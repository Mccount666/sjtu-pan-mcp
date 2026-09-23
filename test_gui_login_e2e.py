"""End-to-end test of gui_login with a simulated successful login.

Launches the real browser, lets gui_login click through to the QR page,
then injects the USER_TOKEN cookie to simulate a scan, and checks that
the polling loop captures it.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sjtu_pan_mcp import login as login_mod

# Simulate the user scanning the QR ~6 seconds in: patch context.cookies
# so that after 6s it reports a USER_TOKEN cookie.
FAKE_TOKEN = "FAKE-USER-TOKEN-1234567890"
start = time.monotonic()
orig_cookies = None


class FakeCookies:
    """Proxy around a real cookie list that injects USER_TOKEN later."""

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, *args, **kwargs):
        real = self.inner(*args, **kwargs)
        if time.monotonic() - start > 6:
            real = list(real) + [
                {"name": "USER_TOKEN", "value": FAKE_TOKEN, "domain": "pan.sjtu.edu.cn", "path": "/"}
            ]
        return real


import playwright.sync_api as psync

_orig_new_context = psync.Browser.new_context


def patched_new_context(self, *a, **kw):
    ctx = _orig_new_context(self, *a, **kw)
    ctx.cookies = FakeCookies(ctx.cookies)
    return ctx


psync.Browser.new_context = patched_new_context

# verify=False: we only test capture, not API validation (token is fake)
result = login_mod.gui_login(timeout=30, verify=False)
print("ok:", result.ok)
print("message:", result.message)
print("token captured:", result.token == FAKE_TOKEN)
assert result.ok and result.token == FAKE_TOKEN, "capture failed"

# Now the verify=True path with the fake token: must fail cleanly
result2 = login_mod.gui_login(timeout=30, verify=True)
print("verify path ok:", result2.ok, "| message:", result2.message[:80])
assert not result2.ok, "fake token should not verify"

# Timeout path: no cookie injected, short timeout
start = time.monotonic()
class NoCookies:
    def __init__(self, inner): self.inner = inner
    def __call__(self, *a, **kw): return self.inner(*a, **kw)
psync.Browser.new_context = lambda self, *a, **kw: (
    lambda ctx: (setattr(ctx, "cookies", NoCookies(ctx.cookies)), ctx)[1]
)(_orig_new_context(self, *a, **kw))
result3 = login_mod.gui_login(timeout=8, verify=False)
print("timeout path ok:", result3.ok, "| message:", result3.message)
assert not result3.ok and "超时" in result3.message
print("GUI LOGIN E2E TEST PASSED")
