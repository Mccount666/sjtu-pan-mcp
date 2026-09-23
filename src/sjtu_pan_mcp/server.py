"""MCP server wiring for 交大云盘 (pan.sjtu.edu.cn).

Tools:
  pan_login         弹出浏览器窗口登录（jAccount 扫码/账密），自动保存登录态
  pan_account       账户信息与登录态自检
  pan_list_spaces   空间列表（个人空间/团队空间）
  pan_list_dir      列出目录内容
  pan_search        全局搜索文件
  pan_file_info     单个文件/目录的元信息
  pan_download_file 下载单个文件到本地
  pan_download_dir  递归下载整个目录到本地

Run modes (see :func:`main`):
  (default)          stdio MCP server
  login              从本地浏览器提取 USER_TOKEN 并写入配置
  status             查看当前配置与登录态
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import mcp.types as types
from mcp.server import Server

from . import __version__, security
from .auth import extract_token_from_browsers, resolve_user_token, save_token
from .client import Entry, NeedLoginError, PanClient, PanError, Space
from .login import gui_login

logger = logging.getLogger("sjtu-pan-mcp")

MAX_DIR_ENTRIES = 500
MAX_DOWNLOAD_FILES = 500

_client: Optional[PanClient] = None
_client_token: Optional[str] = None


def get_client() -> PanClient:
    """A shared client per user token (cached across tool calls)."""
    global _client, _client_token
    token = resolve_user_token()
    if not token:
        raise NeedLoginError(
            401,
            "EmptyUserToken",
            "尚未登录。请在浏览器登录 pan.sjtu.edu.cn 后，"
            "运行 `sjtu-pan-mcp login` 或把 USER_TOKEN 写入 "
            "~/.sjtu-pan-mcp/config.json",
        )
    if _client is None or _client_token != token:
        if _client is not None:
            _client.close()
        _client = PanClient(token)
        _client_token = token
    return _client


def _error_payload(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, PanError):
        hint = ""
        if isinstance(exc, NeedLoginError):
            hint = "登录态无效或已过期，请重新登录 pan.sjtu.edu.cn 后更新 token"
        return {"ok": False, "error": exc.code, "message": exc.message, "hint": hint}
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


def _entry_dict(entry: Entry) -> Dict[str, Any]:
    return entry.to_dict()


def _space_dict(space: Space) -> Dict[str, Any]:
    return space.to_dict()


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def tool_account() -> Dict[str, Any]:
    client = get_client()
    account = client.get_account()
    spaces = client.list_spaces()
    return {
        "ok": True,
        "user_id": account.get("userId"),
        "user_name": account.get("user_name"),
        "organization_id": account.get("organization_id"),
        "organizations": account.get("organizations") or [],
        "spaces": [_space_dict(s) for s in spaces],
    }


def tool_login(timeout: float = 240.0) -> Dict[str, Any]:
    """Pop up a browser window for jAccount login, then capture the token."""
    result = gui_login(timeout=timeout)
    if not result.ok or not result.token:
        return {
            "ok": False,
            "error": "LoginFailed",
            "message": result.message,
            "hint": "窗口弹出后请用交我办 App 扫码（或账号密码）完成登录",
        }
    path = save_token(result.token)
    payload: Dict[str, Any] = {
        "ok": True,
        "message": result.message,
        "config_file": str(path),
        "account": result.account,
    }
    if result.attempts:
        payload["verify_warning"] = result.attempts[-1]
    return payload


def tool_list_spaces() -> Dict[str, Any]:
    client = get_client()
    spaces = client.list_spaces()
    return {
        "ok": True,
        "count": len(spaces),
        "spaces": [_space_dict(s) for s in spaces],
    }


def tool_list_dir(space_selector: str, path: str) -> Dict[str, Any]:
    client = get_client()
    space = client.find_space(space_selector) if space_selector else client.default_space()
    entries = client.list_dir(space, path)
    truncated = len(entries) > MAX_DIR_ENTRIES
    return {
        "ok": True,
        "space": _space_dict(space),
        "path": path or "/",
        "count": len(entries),
        "truncated": truncated,
        "entries": [_entry_dict(e) for e in entries[:MAX_DIR_ENTRIES]],
    }


def tool_search(keyword: str, space_selector: str = "", limit: int = 50) -> Dict[str, Any]:
    client = get_client()
    space = client.find_space(space_selector) if space_selector else None
    data = client.search(keyword, space=space, limit=limit)
    return {"ok": True, "keyword": keyword, "result": data}


def tool_file_info(space_selector: str, path: str) -> Dict[str, Any]:
    client = get_client()
    space = client.find_space(space_selector) if space_selector else client.default_space()
    from .client import _norm_remote_path

    remote = _norm_remote_path(path)
    parent, _, name = remote.rpartition("/")
    entries = client.list_dir(space, parent or "/")
    for entry in entries:
        if entry.name == name or entry.path.rstrip("/") == remote.rstrip("/"):
            return {"ok": True, "space": _space_dict(space), "entry": _entry_dict(entry)}
    raise PanError(404, "NotFound", f"路径不存在: {remote}")


def tool_download_file(
    space_selector: str,
    remote_path: str,
    local_dir: str = "",
) -> Dict[str, Any]:
    client = get_client()
    space = client.find_space(space_selector) if space_selector else client.default_space()
    dest = local_dir or str(_default_download_dir())
    target = client.download_file(space, remote_path, dest)
    return {
        "ok": True,
        "space": _space_dict(space),
        "remote_path": remote_path,
        "local_path": str(target),
        "size": target.stat().st_size,
    }


def tool_download_dir(
    space_selector: str,
    remote_dir: str,
    local_dir: str = "",
    max_files: int = 100,
) -> Dict[str, Any]:
    client = get_client()
    space = client.find_space(space_selector) if space_selector else client.default_space()
    dest = local_dir or str(_default_download_dir())
    max_files = max(1, min(int(max_files), MAX_DOWNLOAD_FILES))

    from .client import _norm_remote_path

    root = _norm_remote_path(remote_dir)
    root_name = root.rstrip("/").rsplit("/", 1)[-1] or "root"
    base = _safe_mkdir(dest, root_name)

    downloaded: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for parent, entry in client.walk(space, root, max_files=max_files):
        if len(downloaded) >= max_files:
            break
        # Preserve the folder structure below the downloaded root.
        sub_rel = parent.rstrip("/")
        if sub_rel.startswith(root.rstrip("/")):
            sub_rel = sub_rel[len(root.rstrip("/")) :].lstrip("/")
        else:
            sub_rel = ""
        target_dir = (
            Path(security.safe_join(str(base), *sub_rel.split("/")))
            if sub_rel
            else base
        )
        try:
            target = client.download_file(space, entry.path, str(target_dir))
            downloaded.append(
                {"remote_path": entry.path, "local_path": str(target), "size": target.stat().st_size}
            )
        except (PanError, OSError) as exc:
            failures.append({"remote_path": entry.path, "error": str(exc)})
    return {
        "ok": True,
        "space": _space_dict(space),
        "remote_dir": root,
        "local_dir": str(base),
        "downloaded": downloaded,
        "failed": failures,
        "total": len(downloaded),
    }


def _default_download_dir():
    from .config import get_default_download_dir

    return get_default_download_dir()


def _safe_mkdir(dest: str, name: str):
    from .security import confined_target

    target = Path(confined_target(dest, name))
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------------
# MCP plumbing (mirrors the geo-mcp pattern: one dispatcher per request type)
# --------------------------------------------------------------------------


def get_tool_definitions() -> List[types.Tool]:
    return [
        types.Tool(
            name="pan_account",
            description="交大云盘账户信息与登录态自检（用户ID、组织、空间列表）",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="pan_login",
            description=(
                "弹出浏览器窗口登录交大云盘（jAccount 扫码/账号密码），"
                "登录成功后自动保存登录态。调用后会阻塞等待用户完成登录"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "等待登录的超时秒数（默认 240）",
                        "default": 240,
                    }
                },
            },
        ),
        types.Tool(
            name="pan_list_spaces",
            description="列出交大云盘的全部空间（个人空间/团队空间），含 space_id",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="pan_list_dir",
            description="列出交大云盘某个目录下的文件和子目录",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径，如 / 或 /论文/2026",
                        "default": "/",
                    },
                    "space": {
                        "type": "string",
                        "description": "空间名或 space_id，省略则用个人空间",
                        "default": "",
                    },
                },
            },
        ),
        types.Tool(
            name="pan_search",
            description="在交大云盘全局搜索文件（按关键词）",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "space": {
                        "type": "string",
                        "description": "限定空间（名或 space_id），可省略",
                        "default": "",
                    },
                },
                "required": ["keyword"],
            },
        ),
        types.Tool(
            name="pan_file_info",
            description="查看交大云盘单个文件/目录的元信息（大小、修改时间等）",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录路径"},
                    "space": {
                        "type": "string",
                        "description": "空间名或 space_id，省略则用个人空间",
                        "default": "",
                    },
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="pan_download_file",
            description="把交大云盘上的单个文件下载到本地目录",
            inputSchema={
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "云盘文件路径，如 /论文/2026/paper.pdf",
                    },
                    "local_dir": {
                        "type": "string",
                        "description": "本地目标目录，省略则用默认下载目录",
                        "default": "",
                    },
                    "space": {
                        "type": "string",
                        "description": "空间名或 space_id，省略则用个人空间",
                        "default": "",
                    },
                },
                "required": ["remote_path"],
            },
        ),
        types.Tool(
            name="pan_download_dir",
            description="递归下载交大云盘整个目录到本地（保留子目录结构）",
            inputSchema={
                "type": "object",
                "properties": {
                    "remote_dir": {"type": "string", "description": "云盘目录路径，如 /论文"},
                    "local_dir": {
                        "type": "string",
                        "description": "本地目标目录，省略则用默认下载目录",
                        "default": "",
                    },
                    "space": {
                        "type": "string",
                        "description": "空间名或 space_id，省略则用个人空间",
                        "default": "",
                    },
                    "max_files": {
                        "type": "integer",
                        "description": "最多下载多少个文件（默认100，上限500）",
                        "default": 100,
                    },
                },
                "required": ["remote_dir"],
            },
        ),
    ]


_TOOL_NAMES = frozenset(
    {
        "pan_account",
        "pan_login",
        "pan_list_spaces",
        "pan_list_dir",
        "pan_search",
        "pan_file_info",
        "pan_download_file",
        "pan_download_dir",
    }
)


async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    arguments = arguments or {}
    if name not in _TOOL_NAMES:
        # Unknown tools raise so the SDK marks the result as an error.
        raise ValueError(f"Unknown tool: {name}")
    try:
        if name == "pan_account":
            result = tool_account()
        elif name == "pan_login":
            result = tool_login(float(arguments.get("timeout", 240)))
        elif name == "pan_list_spaces":
            result = tool_list_spaces()
        elif name == "pan_list_dir":
            result = tool_list_dir(
                arguments.get("space", ""), arguments.get("path", "/")
            )
        elif name == "pan_search":
            result = tool_search(
                arguments.get("keyword", ""),
                arguments.get("space", ""),
                arguments.get("limit", 50),
            )
        elif name == "pan_file_info":
            result = tool_file_info(
                arguments.get("space", ""), arguments.get("path", "")
            )
        elif name == "pan_download_file":
            result = tool_download_file(
                arguments.get("space", ""),
                arguments.get("remote_path", ""),
                arguments.get("local_dir", ""),
            )
        elif name == "pan_download_dir":
            result = tool_download_dir(
                arguments.get("space", ""),
                arguments.get("remote_dir", ""),
                arguments.get("local_dir", ""),
                arguments.get("max_files", 100),
            )
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:  # surface every failure as structured JSON
        logger.exception("tool %s failed", name)
        result = _error_payload(exc)
    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

server = Server("sjtu-pan-mcp")


@server.list_tools()
async def _list_tools() -> List[types.Tool]:
    return get_tool_definitions()


@server.call_tool()
async def _call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    return await handle_call_tool(name, arguments)


# --------------------------------------------------------------------------
# CLI: login / status / serve
# --------------------------------------------------------------------------


def _cmd_login(from_browser: bool, timeout: float) -> int:
    if from_browser:
        print("正在从本地浏览器提取 USER_TOKEN cookie …")
        result = extract_token_from_browsers()
        if not result.ok:
            print("自动提取失败：")
            for attempt in result.attempts:
                print(f"  - {attempt}")
            print()
            print("可以改用弹窗登录: sjtu-pan-mcp login")
            return 1
        path = save_token(result.token)
        print(f"已保存 token（来源: {result.source}）")
        print(f"配置文件: {path}")
        return 0

    print("正在打开浏览器窗口，请完成登录（交我办扫码或账号密码）…")
    result = gui_login(timeout=timeout)
    if not result.ok or not result.token:
        print(f"登录失败: {result.message}")
        print("提示: 窗口弹出后请用交我办 App 扫码，或改用 sjtu-pan-mcp login --from-browser")
        return 1
    path = save_token(result.token)
    print(f"{result.message}，已保存到 {path}")
    if result.account:
        print(f"  userId: {result.account.get('user_id')}")
        for space in result.account.get("spaces", []):
            print(f"  空间: {space['name']} ({space['space_id']}) [{space['kind']}]")
    return 0


def _cmd_status() -> int:
    from .config import config_path, get_default_download_dir

    token = resolve_user_token()
    print(f"配置文件: {config_path()}")
    print(f"默认下载目录: {get_default_download_dir()}")
    if not token:
        print("登录态: 未配置（运行 `sjtu-pan-mcp login`）")
        return 1
    print(f"登录态: 已配置 user_token（{len(token)} 字符，{token[:6]}…）")
    try:
        client = get_client()
        account = tool_account()
        print(f"账户: userId={account['user_id']} 组织={account['organization_id']}")
        for space in account["spaces"]:
            print(f"  空间: {space['name']} ({space['space_id']}) [{space['kind']}]")
    except Exception as exc:
        print(f"登录态校验失败: {exc}")
        return 1
    return 0


def _cmd_debug() -> int:
    """Dump the raw responses of the key user-layer endpoints."""
    from .client import PanClient

    token = resolve_user_token()
    if not token:
        print("未配置登录态")
        return 1
    client = PanClient(token)
    try:
        for path in (
            "/user/v1/organization",
            f"/user/v1/space/{client.organization_id}",
        ):
            url = client.base_url + path
            resp = client._send("GET", url, params={"user_token": token})
            print(f"== GET {path} -> HTTP {resp.status_code}")
            print("   content-type:", resp.headers.get("content-type"))
            print("   body[:600]:", resp.text[:600].replace("\n", " "))
            print()
    except Exception as exc:
        print("debug 失败:", exc)
        return 1
    finally:
        client.close()
    return 0


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="sjtu-pan-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sjtu-pan-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")
    login_p = sub.add_parser("login", help="登录交大云盘（默认弹窗，可用 --from-browser 读本地浏览器 cookie）")
    login_p.add_argument(
        "--from-browser",
        action="store_true",
        help="不弹窗，改为扫描本地 Chromium 系浏览器的 cookie 库",
    )
    login_p.add_argument(
        "--timeout",
        type=float,
        default=240.0,
        help="弹窗登录的等待秒数（默认 240）",
    )
    sub.add_parser("status", help="查看配置与登录态")
    sub.add_parser("debug", help="打印关键接口的原始响应（排查用）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if args.command == "login":
        sys.exit(_cmd_login(args.from_browser, args.timeout))
    if args.command == "status":
        sys.exit(_cmd_status())
    if args.command == "debug":
        sys.exit(_cmd_debug())

    from mcp.server.stdio import stdio_server

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
