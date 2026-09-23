"""User-token resolution.

The pan SPA stores the session token in a plain (non-HttpOnly) cookie
named ``USER_TOKEN`` after a jAccount login, so there are two ways to
supply it:

  1. Manual: paste the cookie value into the config file (always works).
  2. Semi-automatic: :func:`extract_token_from_browsers` reads the cookie
     straight out of a local Chromium-based browser's cookie database
     (Chrome / Edge / Tabbit / other Electron apps). Newer Chrome builds
     encrypt cookies with app-bound encryption which this cannot read;
     the failure is reported, not swallowed.

Both paths end in the same place: the config file written by
:func:`save_token`.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

COOKIE_NAME = "USER_TOKEN"
COOKIE_HOST_MARKER = "pan.sjtu.edu.cn"

# Chromium layouts, relative to a "user data dir":
#   modern:  User Data/Default/Network/Cookies
#   older:  User Data/Default/Cookies
_COOKIE_RELPATHS = (
    ("Default", "Network", "Cookies"),
    ("Default", "Cookies"),
)


@dataclass
class ExtractionResult:
    token: Optional[str] = None
    source: Optional[str] = None
    attempts: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.token)


def resolve_user_token() -> Optional[str]:
    """Env var first, then the config file."""
    from .config import get_user_token

    return get_user_token()


def save_token(token: str) -> Path:
    from .config import save_config

    return save_config({"user_token": token})


# --------------------------------------------------------------------------
# Browser cookie extraction (best effort, Windows only)
# --------------------------------------------------------------------------


def _candidate_user_data_dirs() -> List[Path]:
    """Places a Chromium 'User Data' directory might live."""
    roots: List[Path] = []
    for var in ("LOCALAPPDATA", "APPDATA", "ROAMING"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base))
    dirs: List[Path] = []
    for root in roots:
        # Direct children that look like a User Data dir.
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                ud = child / "User Data"
                if ud.is_dir():
                    dirs.append(ud)
        except OSError:
            continue
        # Electron apps: <root>/<AppName>/Cookies (no User Data level).
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                for rel in _COOKIE_RELPATHS:
                    if (child.joinpath(*rel)).is_file():
                        dirs.append(child)
        except OSError:
            continue
    # De-duplicate, keep order.
    seen = set()
    unique: List[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt a DPAPI blob (Local State's encrypted_key)."""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wt.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _aes_gcm_decrypt(key: bytes, value: bytes) -> Optional[str]:
    """Decrypt a v10/v11 Chromium cookie value."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    for prefix in (b"v10", b"v11"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    else:
        return None
    if len(value) < 3 + 12 + 16:
        return None
    nonce, ciphertext = value[:12], value[12:]
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception:
        return None
    return plain.decode("utf-8", "replace")


def _read_local_state_key(user_data_dir: Path) -> Optional[bytes]:
    state_file = user_data_dir / "Local State"
    if not state_file.is_file():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        encrypted_key = base64.b64decode(state["os_crypt"]["encrypted_key"])
    except (OSError, KeyError, ValueError):
        return None
    if not encrypted_key.startswith(b"DPAPI"):
        return None
    try:
        return _dpapi_unprotect(encrypted_key[len(b"DPAPI") :])
    except OSError:
        return None


def _query_cookie(db_copy: Path, key: Optional[bytes]) -> List[Tuple[str, str]]:
    """Return (host_key, value) pairs for the target cookie."""
    uri = f"file:{db_copy.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.execute(
            "SELECT host_key, name, encrypted_value FROM cookies "
            "WHERE name = ?",
            (COOKIE_NAME,),
        )
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: List[Tuple[str, str]] = []
    for host_key, name, enc in rows:
        if isinstance(enc, str):  # already plaintext (rare)
            out.append((host_key, enc))
            continue
        raw = bytes(enc)
        if raw.startswith((b"v10", b"v11")):
            if key is None:
                continue
            value = _aes_gcm_decrypt(key, raw)
            if value:
                out.append((host_key, value))
        else:
            # Legacy DPAPI-encrypted cookie value.
            try:
                value = _dpapi_unprotect(raw).decode("utf-8", "replace")
                out.append((host_key, value))
            except OSError:
                continue
    return out


def extract_token_from_browsers() -> ExtractionResult:
    """Scan local Chromium profiles for the ``USER_TOKEN`` cookie."""
    result = ExtractionResult()
    for ud in _candidate_user_data_dirs():
        db: Optional[Path] = None
        for rel in _COOKIE_RELPATHS:
            candidate = ud.joinpath(*rel)
            if candidate.is_file():
                db = candidate
                break
        if db is None:
            continue
        label = str(ud)
        # Copy the DB aside: browsers lock it while running.
        tmp_dir = tempfile.mkdtemp(prefix="sjtu-pan-cookie-")
        try:
            copy = Path(tmp_dir) / "Cookies"
            shutil.copyfile(db, copy)
            for suffix in ("-wal", "-shm"):
                side = db.with_name(db.name + suffix)
                if side.is_file():
                    shutil.copyfile(side, copy.with_name(copy.name + suffix))
            key = _read_local_state_key(ud)
            pairs = _query_cookie(copy, key)
        except OSError as exc:
            result.attempts.append(f"{label}: 无法读取 cookie 库 ({exc})")
            continue
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if not pairs:
            result.attempts.append(f"{label}: 未找到 {COOKIE_NAME} cookie")
            continue
        # Prefer an exact host match.
        pairs.sort(key=lambda p: 0 if COOKIE_HOST_MARKER in p[0] else 1)
        host, value = pairs[0]
        if value:
            result.token = value
            result.source = f"{label} (host: {host})"
            return result
        result.attempts.append(f"{label}: cookie 解密失败")
    if not result.attempts:
        result.attempts.append("未在本地发现 Chromium 系浏览器的 User Data 目录")
    return result
