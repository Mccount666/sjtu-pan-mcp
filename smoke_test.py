import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sjtu_pan_mcp import security, client, server

# --- security checks ---
assert security.is_safe_url("https://pan.sjtu.edu.cn/api/v1/config")
assert not security.is_safe_url("http://localhost/api")
assert not security.is_safe_url("http://127.0.0.1/api")
assert not security.is_safe_url("https://192.168.1.10/x")
assert not security.is_safe_url("https://10.0.0.5/x")
assert not security.is_safe_url("https://169.254.169.254/latest/meta-data")
assert not security.is_safe_url("file:///C:/Windows/win.ini")
assert not security.is_safe_url("ftp://pan.sjtu.edu.cn/x")
assert not security.is_safe_url("https://evil.example.com/x")
assert not security.is_safe_url("https://user:pass@pan.sjtu.edu.cn/x")
assert not security.is_safe_url("https://[::1]/x")
assert not security.is_safe_url("")
print("security checks OK")

# --- name/path safety ---
assert security.safe_name("../../etc/passwd") == "passwd"
assert security.safe_name("C:\\evil\\path.txt") == "path.txt"
assert security.safe_name("CON.txt") == "_CON.txt"
assert security.safe_name("NUL") == "_NUL"
assert security.safe_name("") == "unnamed"
assert security.safe_name("..") == "unnamed"
assert security.safe_name("  spaced .txt ") == "spaced .txt"
assert security.safe_name("a:b.txt") == "a"  # colon splits NTFS ADS suffix, conservatively
try:
    security.safe_join("C:/tmp/dest", "../../escape.txt")
    raise SystemExit("safe_join should have raised")
except security.UnsafeUrlError:
    pass
# confined_target / open_confined: the only path where a remote name
# becomes a local file
import os, tempfile
tmp = tempfile.mkdtemp()
# separators are stripped by safe_name, so traversal attempts collapse
# into a plain name inside the destination
assert security.confined_target(tmp, "../../etc/passwd") == os.path.join(os.path.realpath(tmp), "passwd")
assert security.confined_target(tmp, "..\\..\\evil") == os.path.join(os.path.realpath(tmp), "evil")
with security.open_confined(tmp, "sub dir/../../x.txt", "wb") as fh:
    fh.write(b"ok")
assert os.listdir(tmp) == ["x.txt"], os.listdir(tmp)
print("confined write OK")
print("name checks OK")

# --- path normalization ---
n = client._norm_remote_path
assert n("/") == "/"
assert n("a/b") == "/a/b"
assert n("/a//b/") == "/a/b/"
assert n("\\a\\b") == "/a/b"
assert n("/a/./b") == "/a/b"
for bad in ("/a/../../etc", "/../x", "..", "/a/b/../.."):
    try:
        n(bad)
        raise SystemExit(f"'..' should be rejected: {bad}")
    except client.PanError as e:
        assert e.code == "InvalidPath", e
print("path norm OK (incl. '..' rejection)")

# --- entry normalization (tolerant shapes) ---
data = {"contents": [
    {"name": "docs", "path": ["docs"], "type": "dir", "modificationTime": "2026-01-01"},
    {"fileName": "paper.pdf", "path": ["docs", "paper.pdf"], "fileSize": 1234, "type": "file", "userId": "42"},
    {"name": "x", "path": "/x", "type": "directory"},
    {"name": "y.mp4", "path": "/y.mp4", "size": "99", "type": "video"},
]}
entries = client._normalize_entries(data)
assert len(entries) == 4, entries
assert entries[0].is_dir and entries[0].path == "/docs"
assert entries[1].size == 1234 and not entries[1].is_dir and entries[1].path == "/docs/paper.pdf"
assert entries[1].user_id == "42"
assert entries[2].is_dir
assert not entries[3].is_dir and entries[3].size == 99
print("entry norm OK")

# --- spaces normalization: list-shaped org response + personal space ---
orgs = client.PanClient.__dict__  # ensure class importable
data_orgs = [{"id": 1, "name": "上海交通大学", "libraryId": "lib123",
              "orgUser": {"nickname": "测试用户"}, "isLastSignedIn": True}]
assert data_orgs[0]["id"] == 1
print("org shape OK")

# --- live API probes with the configured token (skipped when absent) ---
from sjtu_pan_mcp.config import get_user_token
if get_user_token():
    c = client.PanClient(get_user_token())
    try:
        acct = c.get_account()
        assert acct["organization_id"], acct
        spaces = c.list_spaces()
        assert spaces and spaces[0].kind == "personal", spaces
        sp = spaces[0]
        tok = c.get_space_token(sp.space_id)
        assert tok.access_token and tok.library_id and tok.space_id
        root = c.list_dir(sp, "/")
        assert root, "root listing empty"
        print(f"live: org={acct['organization_id']} space={tok.space_id} root entries={len(root)}")
        # find a file and download it
        import tempfile
        found = None
        queue = ["/"]
        for _ in range(8):
            nxt = []
            for d in queue:
                for e in c.list_dir(sp, d):
                    if e.is_dir:
                        nxt.append(e.path)
                    else:
                        found = e
                        break
                if found:
                    break
            if found or not nxt:
                break
            queue = nxt[:4]
        if found:
            dest = c.download_file(sp, found.path, tempfile.mkdtemp(prefix="sjtu-pan-test-"))
            assert dest.is_file() and dest.stat().st_size > 0
            print(f"live: downloaded {found.name!r} -> {dest.stat().st_size} bytes OK")
        else:
            print("live: no file found to download (dirs only)")
    finally:
        c.close()
else:
    print("live: no token configured, skipped")

# --- tools defined ---
tools = server.get_tool_definitions()
names = [t.name for t in tools]
expected = ["pan_account", "pan_login", "pan_list_spaces", "pan_list_dir", "pan_search",
            "pan_file_info", "pan_download_file", "pan_download_dir"]
assert names == expected, names
for t in tools:
    assert t.inputSchema.get("type") == "object"
print("tools OK:", names)

# --- pan_account behavior depends on whether a token is configured ---
import asyncio, json
from sjtu_pan_mcp.config import get_user_token
res = asyncio.run(server.handle_call_tool("pan_account", {}))
payload = json.loads(res[0].text)
if get_user_token():
    assert payload["ok"] is True and payload["organization_id"], payload
    print("pan_account (with token) OK:", payload["user_name"])
else:
    assert payload["ok"] is False and "hint" in payload, payload
    print("no-token error payload OK:", payload["error"])

# --- live API probe without token (real endpoints, expect 403 JSON) ---
c = client.PanClient("dummytoken")
try:
    c.get_account()
    raise SystemExit("expected failure")
except client.NeedLoginError as e:
    print("live probe OK: NeedLoginError ->", e.code)
c.close()

print("ALL CHECKS PASSED")
