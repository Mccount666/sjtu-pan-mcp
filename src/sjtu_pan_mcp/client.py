"""Two-layer API client for 交大云盘 (pan.sjtu.edu.cn).

Layer 1 (``/user/v1/*``) authenticates with the session ``user_token``
and hands out per-space ``access_token``s. Layer 2 (``/api/v1/*``)
authenticates with an ``access_token`` and serves directory listings and
file downloads.

Every outbound request goes through :func:`security.assert_safe_url`
first, and redirects are followed manually (max 5 hops) so each hop is
validated too.

Response shapes below were verified against the live API on 2026-09-23:

* ``GET  /user/v1/organization``            -> list of org dicts
* ``GET  /user/v1/space/{orgId}``           -> space usage stats
* ``POST /user/v1/space/{orgId}/personal``  -> {libraryId, spaceId,
                                                accessToken, expiresIn}
* ``GET  /api/v1/directory/{lib}/{sid}/{p}``-> {contents: [...], subDirCount,
                                                fileCount, totalNum, ...}
* ``GET  /api/v1/file/{lib}/{sid}/{p}``     -> 302 to a presigned S3 URL
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from . import security
from .security import UnsafeUrlError, assert_safe_url, safe_join, safe_name

BASE_URL = "https://pan.sjtu.edu.cn"
API_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=30.0)
MAX_REDIRECTS = 5
TOKEN_EXPIRY_MARGIN = 60.0  # seconds of slack before treating a token as expired

PERSONAL_SPACE_ID = "personal"  # sentinel; resolved via POST .../personal


class PanError(Exception):
    """An API-level error (non-2xx with a JSON error body)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message} (HTTP {status})")
        self.status = status
        self.code = code
        self.message = message


class NeedLoginError(PanError):
    """The user token is missing, invalid or expired."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Space:
    space_id: str
    name: str
    kind: str = ""           # personal / team / group ...
    space_org_id: str = ""   # owning org for team spaces
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space_id": self.space_id,
            "name": self.name,
            "kind": self.kind,
            "space_org_id": self.space_org_id,
        }


@dataclass
class SpaceToken:
    access_token: str
    library_id: str
    space_id: str
    user_id: str = ""
    expires_in: float = 0.0
    obtained_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        if not self.expires_in:
            return False
        return time.time() > self.obtained_at + self.expires_in - TOKEN_EXPIRY_MARGIN


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: str = ""
    user_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "mtime": self.mtime,
        }


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class PanClient:
    def __init__(
        self,
        user_token: str,
        organization_id: Optional[str] = None,
        base_url: str = BASE_URL,
    ) -> None:
        if not user_token:
            raise NeedLoginError(401, "EmptyUserToken", "未配置 user_token，请先登录")
        self.user_token = user_token
        self.base_url = base_url.rstrip("/")
        self._organization_id = str(organization_id) if organization_id else None
        self._library_id: Optional[str] = None
        self._user_id: Optional[str] = None
        self._token_cache: Dict[str, SpaceToken] = {}
        self._http = httpx.Client(
            timeout=API_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PanClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level request -------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Any = API_TIMEOUT,
    ) -> httpx.Response:
        """Perform one request with per-hop URL validation."""
        assert_safe_url(url)
        return self._http.request(
            method,
            url,
            params=params,
            json=json_body,
            follow_redirects=False,
            timeout=timeout,
        )

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Any = API_TIMEOUT,
    ) -> httpx.Response:
        """Send, following redirects manually with validation on each hop.

        Query params and the body belong to the *original* request only —
        re-attaching them to a redirect target would corrupt presigned
        URLs (the file API 302s to a signed S3 URL).
        """
        current = url
        first = True
        resp: Optional[httpx.Response] = None
        for _ in range(MAX_REDIRECTS + 1):
            resp = self._request(
                method,
                current,
                params=params if first else None,
                json_body=json_body if first else None,
                timeout=timeout,
            )
            first = False
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    break
                current = security.resolve_redirect(current, location)
                if resp.status_code == 303:
                    method = "GET"
                    json_body = None
                continue
            return resp
        raise PanError(
            resp.status_code if resp is not None else 0,
            "TooManyRedirects",
            "重定向次数过多",
        )

    def _api(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        user_token: bool = True,
        timeout: Any = API_TIMEOUT,
    ) -> Any:
        """JSON API call. Raises :class:`PanError` on error bodies."""
        url = self.base_url + path
        query: Dict[str, Any] = dict(params or {})
        if user_token:
            query.setdefault("user_token", self.user_token)
        resp = self._send(method, url, params=query, json_body=json_body, timeout=timeout)
        return self._decode(resp)

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            code, message = "HTTPError", resp.text[:300]
            try:
                body = resp.json()
                if isinstance(body, dict):
                    code = str(body.get("code", code))
                    message = str(body.get("message", message))
            except (json.JSONDecodeError, ValueError):
                pass
            err = PanError(resp.status_code, code, message)
            if code in ("InvalidUserToken", "EmptyUserToken", "UserDisabled"):
                raise NeedLoginError(resp.status_code, code, message) from err
            raise err
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return resp.text

    # -- layer 1: user -----------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """Account info. The live endpoint returns a list of org dicts."""
        data = self._api("GET", "/user/v1/organization")
        orgs: List[Dict[str, Any]] = []
        if isinstance(data, list):
            orgs = [o for o in data if isinstance(o, dict)]
        elif isinstance(data, dict):
            raw = data.get("organizations")
            orgs = [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
        if not orgs:
            raise PanError(200, "NoOrganization", "账号没有关联任何组织")
        org = self._select_organization(orgs)
        self._organization_id = str(org.get("id"))
        library_id = org.get("libraryId")
        if library_id:
            self._library_id = str(library_id)
        org_user = org.get("orgUser") if isinstance(org.get("orgUser"), dict) else {}
        user_id = org_user.get("userId") or org_user.get("id")
        if user_id:
            self._user_id = str(user_id)
        return {
            "userId": self._user_id,
            "user_name": org_user.get("nickname"),
            "organization_id": self._organization_id,
            "library_id": self._library_id,
            "organizations": [
                {
                    "organization_id": o.get("id"),
                    "name": o.get("name"),
                    "library_id": o.get("libraryId"),
                }
                for o in orgs
            ],
        }

    def _select_organization(self, orgs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Config pin first, then the most recently signed-in org."""
        from .config import get_pinned_organization_id

        pinned = get_pinned_organization_id()
        if pinned is not None:
            for org in orgs:
                if str(org.get("id")) == str(pinned):
                    return org
        for org in orgs:
            if org.get("isLastSignedIn"):
                return org
        return orgs[0]

    @property
    def organization_id(self) -> str:
        if not self._organization_id:
            self.get_account()
        if not self._organization_id:
            raise PanError(200, "NoOrganization", "账号没有关联任何组织")
        return self._organization_id

    def list_spaces(self) -> List[Space]:
        """Spaces the user can browse. This deployment exposes the
        personal space (the stats endpoint reports ``hasPersonalSpace``);
        team spaces, when present, need their own listing endpoint."""
        stats = self._api("GET", f"/user/v1/space/{self.organization_id}")
        has_personal = True
        if isinstance(stats, dict) and "hasPersonalSpace" in stats:
            has_personal = bool(stats["hasPersonalSpace"])
        spaces: List[Space] = []
        if has_personal:
            spaces.append(
                Space(
                    space_id=PERSONAL_SPACE_ID,
                    name="个人空间",
                    kind="personal",
                    raw=stats if isinstance(stats, dict) else {},
                )
            )
        return spaces

    def get_space_token(self, space_id: str) -> SpaceToken:
        cached = self._token_cache.get(space_id)
        if cached and not cached.expired():
            return cached
        if space_id == PERSONAL_SPACE_ID:
            data = self._api(
                "POST", f"/user/v1/space/{self.organization_id}/personal"
            )
        else:
            data = self._api(
                "POST", f"/user/v1/space/{self.organization_id}/token/{space_id}"
            )
        if not isinstance(data, dict):
            raise PanError(200, "BadResponse", "空间令牌返回异常")
        token = SpaceToken(
            access_token=_first_str(data, ("accessToken", "access_token"), ""),
            library_id=_first_str(
                data, ("libraryId", "library_id"), self._library_id or ""
            ),
            space_id=_first_str(data, ("spaceId", "space_id"), space_id),
            user_id=str(_first_str(data, ("userId", "user_id"), self._user_id or "")),
            expires_in=float(data.get("expiresIn") or data.get("expires_in") or 0),
        )
        if not token.access_token:
            raise PanError(200, "BadResponse", f"空间 {space_id} 未返回 access_token")
        self._token_cache[space_id] = token
        return token

    # -- layer 2: space ----------------------------------------------------

    def list_dir(
        self,
        space: Space,
        dir_path: str = "/",
        *,
        page_size: int = 200,
        max_entries: int = 5000,
    ) -> List[Entry]:
        """List a directory (paginated until exhausted or capped)."""
        token = self.get_space_token(space.space_id)
        dir_path = _norm_remote_path(dir_path)
        entries: List[Entry] = []
        page = 1
        marker: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"access_token": token.access_token}
            if marker:
                params["marker"] = marker
            else:
                params["page"] = page
                params["page_size"] = page_size
            data = self._api(
                "GET",
                f"/api/v1/directory/{token.library_id}/{token.space_id}/{dir_path}",
                params=params,
                user_token=False,
            )
            batch = _normalize_entries(data)
            entries.extend(batch)
            if len(entries) >= max_entries:
                entries = entries[:max_entries]
                break
            marker = _next_marker(data)
            if marker:
                page = 1
                continue
            if len(batch) < page_size:
                break
            page += 1
            if page > 100:  # safety valve against pathological pagination
                break
        return entries

    def search(
        self,
        keyword: str,
        *,
        space: Optional[Space] = None,
        limit: int = 50,
    ) -> Any:
        body: Dict[str, Any] = {"keyword": keyword, "accurate": False}
        if space is not None and space.space_id != PERSONAL_SPACE_ID:
            body["spaceId"] = space.space_id
        return self._api(
            "POST",
            f"/user/v1/directory-search/{self.organization_id}/global-search",
            json_body=body,
        )

    def download_file(
        self,
        space: Space,
        file_path: str,
        dest_dir: str,
        *,
        filename: Optional[str] = None,
        max_bytes: int = 20 * 1024 * 1024 * 1024,
    ) -> Path:
        """Stream one file to ``dest_dir``; returns the local path."""
        token = self.get_space_token(space.space_id)
        remote = _norm_remote_path(file_path)
        # safe_name strips directory separators, NTFS ADS suffixes and
        # reserved device names; confined_target then proves the local
        # path sits inside dest_dir. The write itself goes through
        # security.open_confined, which re-validates before opening.
        name = safe_name(filename or remote.rsplit("/", 1)[-1])
        target = _unique_path(Path(security.confined_target(dest_dir, name)))
        params: Dict[str, Any] = {
            "access_token": token.access_token,
            "content_disposition": "attachment",
        }
        # user_id is only used for traffic accounting; supply it when the
        # owning entry is known, otherwise omit.
        owner = self._lookup_user_id(space, remote)
        if owner:
            params["user_id"] = owner
        current = _with_params(
            f"{self.base_url}/api/v1/file/{token.library_id}/{token.space_id}/{remote}",
            params,
        )
        assert_safe_url(current)
        for _ in range(MAX_REDIRECTS + 1):
            with self._http.stream(
                "GET", current, follow_redirects=False, timeout=DOWNLOAD_TIMEOUT
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise PanError(resp.status_code, "NoLocation", "重定向缺少 Location")
                    current = security.resolve_redirect(current, location)
                    continue
                if resp.status_code >= 400:
                    self._decode(resp)
                ctype = resp.headers.get("content-type", "")
                if "application/json" in ctype:
                    # Some deployments answer with a signed URL instead of bytes.
                    body = resp.read()
                    try:
                        payload = json.loads(body)
                    except (json.JSONDecodeError, ValueError):
                        raise PanError(
                            resp.status_code,
                            "BadDownload",
                            body[:300].decode("utf-8", "replace"),
                        )
                    signed = _find_url(payload)
                    if not signed:
                        raise PanError(resp.status_code, "BadDownload", "下载响应中没有文件 URL")
                    current = signed
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with security.open_confined(dest_dir, target.name, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        written += len(chunk)
                        if written > max_bytes:
                            fh.close()
                            target.unlink(missing_ok=True)
                            raise PanError(200, "TooLarge", f"文件超过大小上限 {max_bytes} 字节")
                        fh.write(chunk)
                return target
        raise PanError(200, "TooManyRedirects", "下载重定向次数过多")

    def _lookup_user_id(self, space: Space, remote_path: str) -> str:
        """Best-effort: find the file's owning userId from its parent
        directory listing (used for traffic accounting only)."""
        try:
            parent = remote_path.rstrip("/").rsplit("/", 1)[0] or "/"
            name = remote_path.rstrip("/").rsplit("/", 1)[-1]
            for entry in self.list_dir(space, parent):
                if entry.name == name:
                    return entry.user_id
        except PanError:
            pass
        return ""

    def walk(
        self,
        space: Space,
        dir_path: str = "/",
        *,
        max_depth: int = 8,
        max_files: int = 2000,
    ) -> Iterable[Tuple[str, Entry]]:
        """Yield ``(parent_path, entry)`` for every file under ``dir_path``."""
        stack: List[Tuple[str, int]] = [(_norm_remote_path(dir_path), 0)]
        seen_files = 0
        while stack:
            current, depth = stack.pop()
            try:
                entries = self.list_dir(space, current)
            except PanError as exc:
                if exc.code in ("NoSuchDirectory", "PathNotFound", "EmptyPath"):
                    continue
                raise
            for entry in entries:
                if entry.is_dir:
                    if depth < max_depth:
                        stack.append((entry.path, depth + 1))
                else:
                    seen_files += 1
                    if seen_files > max_files:
                        return
                    yield current, entry

    # -- helpers -----------------------------------------------------------

    def find_space(self, selector: str) -> Space:
        """Resolve a space by id or (fuzzy) name."""
        spaces = self.list_spaces()
        selector = (selector or "").strip()
        if not selector:
            return self.default_space()
        for space in spaces:
            if space.space_id == selector:
                return space
        lowered = selector.lower()
        for space in spaces:
            if space.name.lower() == lowered:
                return space
        for space in spaces:
            if lowered in space.name.lower():
                return space
        available = ", ".join(f"{s.name}({s.space_id})" for s in spaces) or "无"
        raise PanError(404, "SpaceNotFound", f"找不到空间 {selector!r}，可用: {available}")

    def default_space(self) -> Space:
        spaces = self.list_spaces()
        if not spaces:
            raise PanError(404, "NoSpace", "账号下没有任何空间")
        for space in spaces:
            if space.kind.lower() in ("personal", "person", "private"):
                return space
        return spaces[0]


# --------------------------------------------------------------------------
# Tolerant JSON shape helpers (response schemas are not publicly documented)
# --------------------------------------------------------------------------


def _first_list(data: Any, keys: Iterable[str]) -> List[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def _first_str(data: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _norm_remote_path(path: str) -> str:
    path = (path or "/").strip().replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PanError(400, "InvalidPath", "远程路径不允许包含 '..'")
    cleaned = "/" + "/".join(parts)
    if path.endswith("/") and cleaned != "/":
        cleaned += "/"
    return cleaned


def _next_marker(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in ("next_marker", "nextMarker", "marker"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _entry_path(item: Dict[str, Any]) -> str:
    """The API returns ``path`` as a list of segments."""
    value = item.get("path", item.get("filePath", item.get("dirPath")))
    if isinstance(value, (list, tuple)):
        return "/" + "/".join(str(p) for p in value if str(p))
    if isinstance(value, str) and value:
        return value if value.startswith("/") else "/" + value
    return ""


def _looks_like_dir(item: Dict[str, Any]) -> bool:
    for key in ("is_dir", "isDir", "is_directory", "isDirectory", "directory", "folder"):
        if key in item:
            return bool(item[key])
    for key in ("type", "nodeType", "node_type", "fileType", "file_type"):
        value = str(item.get(key, "")).lower()
        if value in ("dir", "directory", "folder"):
            return True
        if value in ("file", "doc", "video", "audio", "image"):
            return False
    return False


def _normalize_entries(data: Any) -> List[Entry]:
    items = _first_list(
        data,
        ("contents", "nodes", "list", "files", "children", "items", "entries", "data"),
    )
    entries: List[Entry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _first_str(item, ("name", "fileName", "filename", "nodeName"))
        if not name:
            continue
        size_raw = item.get("size", item.get("fileSize", item.get("file_size", 0))) or 0
        try:
            size = int(size_raw)
        except (TypeError, ValueError):
            size = 0
        entries.append(
            Entry(
                name=name,
                path=_entry_path(item) or name,
                is_dir=_looks_like_dir(item),
                size=size,
                mtime=_first_str(
                    item,
                    ("mtime", "modificationTime", "modification_time",
                     "updatedAt", "updated_at", "lastModified"),
                ),
                user_id=_first_str(item, ("userId", "user_id", "ownerId")),
                raw=item,
            )
        )
    return entries


def _find_url(payload: Any) -> Optional[str]:
    if isinstance(payload, str):
        return payload if payload.startswith("http") else None
    if isinstance(payload, dict):
        for key in ("url", "downloadUrl", "download_url", "src", "link"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for value in payload.values():
            found = _find_url(value)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _find_url(value)
            if found:
                return found
    return None


def _with_params(url: str, params: Dict[str, Any]) -> str:
    from urllib.parse import quote, urlencode

    query = urlencode({k: str(v) for k, v in params.items()}, quote_via=quote)
    return f"{url}?{query}" if query else url


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")
