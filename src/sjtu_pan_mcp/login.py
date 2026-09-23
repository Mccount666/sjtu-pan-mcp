"""Interactive login: pop up a real browser window and wait for the user
to authenticate (jAccount / QR scan), then capture the session cookie.

This is the primary login path because it works identically everywhere
(ZCode, WorkBuddy, plain CLI) and does not depend on reading the user's
own browser cookie store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

LOGIN_URL = "https://pan.sjtu.edu.cn/"
COOKIE_NAME = "USER_TOKEN"
COOKIE_HOST = "https://pan.sjtu.edu.cn"


@dataclass
class GuiLoginResult:
    ok: bool = False
    token: Optional[str] = None
    message: str = ""
    account: Optional[dict] = None
    attempts: List[str] = field(default_factory=list)


def _click_jaccount_entry(page) -> None:
    """Best effort: click the "jAccount登录" button so the user lands
    directly on the QR page. Failure is harmless — the user clicks it."""
    try:
        button = page.locator("button.linear-gradient-btn")
        if button.count():
            button.first.click(timeout=5000)
    except Exception:
        pass


def gui_login(
    timeout: float = 240.0,
    headless: bool = False,
    verify: bool = True,
) -> GuiLoginResult:
    """Open a visible Chromium window on the pan login page and wait for
    the ``USER_TOKEN`` cookie to appear.

    ``timeout`` is the total wait in seconds. ``verify`` calls the account
    API with the captured token before returning, so a stale/expired token
    is caught here rather than at first use.
    """
    result = GuiLoginResult()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result.message = (
            "未安装 playwright。请执行: "
            "pip install \"playwright>=1.60\" 然后 playwright install chromium"
        )
        return result

    token: Optional[str] = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--start-maximized"],
            )
            try:
                context = browser.new_context(no_viewport=True)
                page = context.new_page()
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                _click_jaccount_entry(page)
                deadline = time.monotonic() + max(5.0, timeout)
                while time.monotonic() < deadline:
                    if page.is_closed():
                        result.message = "登录窗口已被关闭，未完成登录"
                        return result
                    try:
                        cookies = context.cookies(COOKIE_HOST)
                    except Exception:
                        cookies = []
                    for cookie in cookies:
                        if cookie.get("name") == COOKIE_NAME and cookie.get("value"):
                            token = cookie["value"]
                            break
                    if token:
                        break
                    time.sleep(1.0)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:  # browser launch failures, etc.
        result.message = f"浏览器启动失败: {exc}"
        result.attempts.append(str(exc))
        return result

    if not token:
        result.message = (
            f"等待登录超时（{int(timeout)} 秒）。请在窗口中完成扫码/登录后重试"
        )
        return result

    result.token = token
    result.ok = True  # the cookie came from a real login; keep it regardless
    if verify:
        from .client import PanClient

        client = PanClient(token)
        try:
            account = client.get_account()
            spaces = client.list_spaces()
            result.account = {
                "user_id": account.get("userId"),
                "organization_id": client.organization_id,
                "spaces": [
                    {
                        "name": s.name,
                        "space_id": s.space_id,
                        "kind": s.kind,
                    }
                    for s in spaces
                ],
            }
            result.message = "登录成功"
        except Exception as exc:
            # Token is real; verification is best-effort. Keep it and warn.
            result.message = f"已捕获登录态（校验未通过: {exc}）"
            result.attempts.append(str(exc))
        finally:
            client.close()
    else:
        result.message = "已捕获登录态"
    return result
