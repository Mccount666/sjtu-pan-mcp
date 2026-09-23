"""Speak raw MCP over stdio to verify the server handshake and tool listing."""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CHILD = [sys.executable, str(PROJECT_ROOT / "smoke_mcp_child.py")]


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc):
    line = proc.stdout.readline()
    return json.loads(line)


proc = subprocess.Popen(
    CHILD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8",
)
try:
    send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0.1"},
        },
    })
    init = recv(proc)
    assert init["result"]["serverInfo"]["name"] == "sjtu-pan-mcp", init
    print("initialize OK:", init["result"]["serverInfo"])

    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = recv(proc)
    names = [t["name"] for t in tools["result"]["tools"]]
    assert len(names) == 8, names
    print("tools/list OK:", names)

    send(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "pan_list_spaces", "arguments": {}},
    })
    call = recv(proc)
    payload = json.loads(call["result"]["content"][0]["text"])
    has_token = bool(os.environ.get("SJTU_PAN_USER_TOKEN")) or os.path.exists(
        os.path.expanduser("~/.sjtu-pan-mcp/config.json")
    )
    if has_token:
        assert payload["ok"] is True, payload
        print("tools/call (with token) OK: spaces =", payload.get("count"))
    else:
        assert payload["ok"] is False, payload  # no token configured yet
        print("tools/call (no token) OK:", payload["error"])

    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "pan_nope", "arguments": {}}})
    bad = recv(proc)
    assert bad["result"]["isError"] is True, bad
    print("unknown tool OK")
    print("MCP PROTOCOL TEST PASSED")
finally:
    proc.kill()
    err = proc.stderr.read()
    if err.strip():
        print("--- child stderr ---")
        print(err[:2000])
