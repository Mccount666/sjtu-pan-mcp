"""Configuration: paths, token storage, defaults.

Resolution order for the user token (first hit wins):
  1. ``SJTU_PAN_USER_TOKEN`` environment variable
  2. ``user_token`` field in the JSON config file (default
     ``~/.sjtu-pan-mcp/config.json``; override with
     ``SJTU_PAN_CONFIG``)

The config file may also carry:
  ``default_download_dir``  where files land when a tool call omits a
                            destination (default ``~/Downloads/sjtu-pan``)
  ``organization_id``       pin the active organization instead of
                            auto-selecting the first one
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

APP_DIR_NAME = ".sjtu-pan-mcp"
CONFIG_FILE_NAME = "config.json"

DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "sjtu-pan"


def app_dir() -> Path:
    """Directory holding the config file (created on write, not here)."""
    override = os.environ.get("SJTU_PAN_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / APP_DIR_NAME


def config_path() -> Path:
    override = os.environ.get("SJTU_PAN_CONFIG")
    if override:
        return Path(override).expanduser()
    return app_dir() / CONFIG_FILE_NAME


def load_config() -> Dict[str, Any]:
    """Return the config dict, or an empty dict when absent/unreadable."""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: Dict[str, Any]) -> Path:
    """Merge ``data`` into the config file and return its path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_config()
    merged.update(data)
    path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def get_user_token() -> Optional[str]:
    """Resolve the user token from env, then the config file."""
    token = os.environ.get("SJTU_PAN_USER_TOKEN", "").strip()
    if token:
        return token
    token = str(load_config().get("user_token", "")).strip()
    return token or None


def get_default_download_dir() -> Path:
    value = load_config().get("default_download_dir")
    if value:
        return Path(str(value)).expanduser()
    return DEFAULT_DOWNLOAD_DIR


def get_pinned_organization_id() -> Optional[int]:
    value = load_config().get("organization_id")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
