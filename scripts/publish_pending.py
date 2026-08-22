#!/usr/bin/env python3
"""Upload article manifests to WordPress, always as pending review.

The authenticated WordPress account should have the Author role.  This script
adds a second safety boundary: it never accepts or sends a publishable status.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_MEDIA_BYTES = 12 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLACEHOLDER_RE = re.compile(r"\{\{media:([a-zA-Z0-9_-]+)\}\}")


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    base_url: str
    username: str
    app_password: str

    @classmethod
    def from_environment(cls) -> "Credentials":
        values = {
            "WP_URL": os.environ.get("WP_URL", "").strip(),
            "WP_USERNAME": os.environ.get("WP_USERNAME", "").strip(),
            "WP_APP_PASSWORD": os.environ.get("WP_APP_PASSWORD", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise BridgeError("Missing required environment values: " + ", ".join(missing))
        return cls(
            base_url=values["WP_URL"].rstrip("/"),
            username=values["WP_USERNAME"],
            app_password=values["WP_APP_PASSWORD"],
        )


class WordPressClient:
    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        token = base64.b64encode(
            f"{credentials.username}:{credentials.app_password}".encode("utf-8")
        ).decode("ascii")
        self._auth_header = f"Basic {token}"

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = {
            "Authorization": self._auth_header,
            "User-Agent": "bric-wordpress-bridge/1.0",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=utf-8"

        targets = [path]
        fallback = rest_route_fallback(path)
        if fallback:
            targets.append(fallback)
        last_error: tuple[int, str, str] | None = None
        for index, target in enumerate(targets):
            url = self.credentials.base_url + target
            request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1200]
                last_error = (exc.code, target, detail)
                # Some LiteSpeed configurations deny the pretty /wp-json path
                # before WordPress runs. Retry the equivalent rest_route URL
                # only for that server-level 403; never mask WordPress errors.
                if not (index == 0 and exc.code == 403 and "<html" in detail.lower() and fallback):
                    break
            except urllib.error.URLError as exc:
                raise BridgeError(f"Cannot reach WordPress for {method} {target}: {exc.reason}") from exc
        assert last_error is not None
        code, target, detail = last_error
        raise BridgeError(f"WordPress HTTP {code} for {method} {target}: {detail}")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", path, payload=payload)

    def upload_media(self, filename: str, mime_type: str, data: bytes) -> dict[str, Any]:
        return self.request(
            "POST",
            "/wp-json/wp/v2/media",
            body=data,
            headers={
                "Content-Type": mime_type,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )


def rest_route_fallback(path: str) -> str | None:
    if not path.startswith("/wp-json/"):
        return None
    parsed = urllib.parse.urlsplit(path)
    route = parsed.path[len("/wp-json") :]
    query = [("rest_route", route), *urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)]
    return "/?" + urllib.parse.urlencode(query)


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise BridgeError("schema_version must be 1")
    for field in ("title", "slug", "content", "category"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise BridgeError(f"{field} must be a non-empty string")
    if not SLUG_RE.fullmatch(manifest["slug"]):
        raise BridgeError("slug must contain lowercase letters, numbers and single hyphens only")
    requested_status = manifest.get("status", "pending")
    if requested_status != "pending":
        raise BridgeError("Only status=pending is allowed")
    media = manifest.get("media", [])
    if not isinstance(media, list):
        raise BridgeError("media must be a list")
    keys: set[str] = set()
    for item in media:
        if not isinstance(item, dict):
            raise BridgeError("each media item must be an object")
        key = item.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", key):
            raise BridgeError("each media item needs a simple key")
        if key in keys:
            raise BridgeError(f"duplicate media key: {key}")
        keys.add(key)
        sources = [name for name in ("path", "url") if item.get(name)]
        if len(sources) != 1:
            raise BridgeError(f"media {key} must define exactly one of path or url")
    featured = manifest.get("featured_media")
    if featured is not None and featured not in keys:
        raise BridgeError("featured_media must reference a media key")
    unknown = set(PLACEHOLDER_RE.findall(manifest["content"])) - keys
    if unknown:
        raise BridgeError("unknown media placeholders: " + ", ".join(sorted(unknown)))


def safe_repo_media_path(repo_root: Path, value: str) -> Path:
    allowed_root = (repo_root / "inbox" / "assets").resolve()
    candidate = (repo_root / value).resolve()
    if candidate != allowed_root and allowed_root not in candidate.parents:
        raise BridgeError("local media paths must stay under inbox/assets")
    if not candidate.is_file():
        raise BridgeError(f"media file does not exist: {value}")
    return candidate


def read_remote_media(url: str) -> tuple[bytes, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise BridgeError("remote media URLs must use https")
    request = urllib.request.Request(url, headers={"User-Agent": "bric-wordpress-bridge/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(MAX_MEDIA_BYTES + 1)
            mime_type = response.headers.get_content_type()
    except urllib.error.URLError as exc:
        raise BridgeError(f"cannot download media URL: {exc.reason}") from exc
    if len(data) > MAX_MEDIA_BYTES:
        raise BridgeError("remote media exceeds 12 MiB")
    return data, mime_type


def load_media(repo_root: Path, item: dict[str, Any]) -> tuple[bytes, str, str]:
    if item.get("path"):
        path = safe_repo_media_path(repo_root, str(item["path"]))
        data = path.read_bytes()
        filename = str(item.get("filename") or path.name)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    else:
        data, response_type = read_remote_media(str(item["url"]))
        default_name = Path(urllib.parse.urlparse(str(item["url"])).path).name or f"{item['key']}.jpg"
        filename = str(item.get("filename") or default_name)
        mime_type = response_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if len(data) > MAX_MEDIA_BYTES:
        raise BridgeError(f"media {item['key']} exceeds 12 MiB")
    if mime_type not in ALLOWED_MEDIA_TYPES:
        raise BridgeError(f"media {item['key']} has unsupported type {mime_type}")
    return data, mime_type, filename


def find_existing_post(client: WordPressClient, slug: str) -> dict[str, Any] | None:
    statuses = "publish,future,draft,pending,private"
    path = "/wp-json/wp/v2/posts?" + urllib.parse.urlencode(
        {"slug": slug, "status": statuses, "context": "edit", "per_page": 1}
    )
    posts = client.get(path)
    return posts[0] if posts else None


def resolve_category(client: WordPressClient, category: str) -> int:
    slug_path = "/wp-json/wp/v2/categories?" + urllib.parse.urlencode(
        {"slug": category, "per_page": 100}
    )
    matches = client.get(slug_path)
    if not matches:
        search_path = "/wp-json/wp/v2/categories?" + urllib.parse.urlencode(
            {"search": category, "per_page": 100}
        )
        matches = [item for item in client.get(search_path) if item.get("name", "").casefold() == category.casefold()]
    if len(matches) != 1:
        raise BridgeError(f"category must match exactly one existing WordPress category: {category}")
    return int(matches[0]["id"])


def publish_manifest(client: WordPressClient, repo_root: Path, manifest_path: Path) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    existing = find_existing_post(client, manifest["slug"])
    if existing:
        return f"SKIP slug={manifest['slug']} existing_id={existing['id']} status={existing['status']}"

    category_id = resolve_category(client, manifest["category"])
    uploaded: dict[str, dict[str, Any]] = {}
    for item in manifest.get("media", []):
        data, mime_type, filename = load_media(repo_root, item)
        media = client.upload_media(filename, mime_type, data)
        details: dict[str, Any] = {}
        if item.get("alt"):
            details["alt_text"] = str(item["alt"])
        if item.get("caption"):
            details["caption"] = str(item["caption"])
        if item.get("title"):
            details["title"] = str(item["title"])
        if details:
            media = client.post(f"/wp-json/wp/v2/media/{media['id']}", details)
        uploaded[item["key"]] = media

    content = PLACEHOLDER_RE.sub(lambda match: uploaded[match.group(1)]["source_url"], manifest["content"])
    post: dict[str, Any] = {
        "title": manifest["title"],
        "slug": manifest["slug"],
        "content": content,
        "excerpt": manifest.get("excerpt", ""),
        "categories": [category_id],
        "status": "pending",
        "comment_status": "closed",
        "ping_status": "closed",
    }
    featured_key = manifest.get("featured_media")
    if featured_key:
        post["featured_media"] = int(uploaded[featured_key]["id"])
    created = client.post("/wp-json/wp/v2/posts", post)
    if created.get("status") != "pending":
        raise BridgeError(f"WordPress returned unexpected status {created.get('status')!r}")
    return f"CREATED slug={manifest['slug']} post_id={created['id']} status=pending"


def manifest_paths(repo_root: Path, values: list[str]) -> list[Path]:
    if values:
        candidates = [(repo_root / value).resolve() for value in values]
    else:
        candidates = sorted((repo_root / "inbox").glob("*.json"))
    inbox_root = (repo_root / "inbox").resolve()
    for candidate in candidates:
        if inbox_root not in candidate.parents or candidate.suffix.lower() != ".json":
            raise BridgeError("manifests must be JSON files directly under inbox")
        if not candidate.is_file():
            raise BridgeError(f"manifest does not exist: {candidate}")
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifests", nargs="*")
    parser.add_argument("--test-connection", action="store_true")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    client = WordPressClient(Credentials.from_environment())
    me = client.get("/wp-json/wp/v2/users/me?context=edit")
    print(f"AUTH_OK user={me.get('slug') or me.get('name')} id={me.get('id')}")
    if args.test_connection:
        return 0
    paths = manifest_paths(repo_root, args.manifests)
    if not paths:
        raise BridgeError("No article manifests found under inbox")
    for path in paths:
        print(publish_manifest(client, repo_root, path))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BridgeError, json.JSONDecodeError) as exc:
        print(f"BRIDGE_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Upload article manifests to WordPress, always as pending review.

The authenticated WordPress account should have the Author role.  This script
adds a second safety boundary: it never accepts or sends a publishable status.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_MEDIA_BYTES = 12 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PLACEHOLDER_RE = re.compile(r"\{\{media:([a-zA-Z0-9_-]+)\}\}")


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    base_url: str
    username: str
    app_password: str

    @classmethod
    def from_environment(cls) -> "Credentials":
        values = {
            "WP_URL": os.environ.get("WP_URL", "").strip(),
            "WP_USERNAME": os.environ.get("WP_USERNAME", "").strip(),
            "WP_APP_PASSWORD": os.environ.get("WP_APP_PASSWORD", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise BridgeError("Missing required environment values: " + ", ".join(missing))
        return cls(
            base_url=values["WP_URL"].rstrip("/"),
            username=values["WP_USERNAME"],
            app_password=values["WP_APP_PASSWORD"],
        )


class WordPressClient:
    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        token = base64.b64encode(
            f"{credentials.username}:{credentials.app_password}".encode("utf-8")
        ).decode("ascii")
        self._auth_header = f"Basic {token}"

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self.credentials.base_url + path
        request_headers = {
            "Authorization": self._auth_header,
            "User-Agent": "bric-wordpress-bridge/1.0",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
            raise BridgeError(f"WordPress HTTP {exc.code} for {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise BridgeError(f"Cannot reach WordPress for {method} {path}: {exc.reason}") from exc

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", path, payload=payload)

    def upload_media(self, filename: str, mime_type: str, data: bytes) -> dict[str, Any]:
        return self.request(
            "POST",
            "/wp-json/wp/v2/media",
            body=data,
            headers={
                "Content-Type": mime_type,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise BridgeError("schema_version must be 1")
    for field in ("title", "slug", "content", "category"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise BridgeError(f"{field} must be a non-empty string")
    if not SLUG_RE.fullmatch(manifest["slug"]):
        raise BridgeError("slug must contain lowercase letters, numbers and single hyphens only")
    requested_status = manifest.get("status", "pending")
    if requested_status != "pending":
        raise BridgeError("Only status=pending is allowed")
    media = manifest.get("media", [])
    if not isinstance(media, list):
        raise BridgeError("media must be a list")
    keys: set[str] = set()
    for item in media:
        if not isinstance(item, dict):
            raise BridgeError("each media item must be an object")
        key = item.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", key):
            raise BridgeError("each media item needs a simple key")
        if key in keys:
            raise BridgeError(f"duplicate media key: {key}")
        keys.add(key)
        sources = [name for name in ("path", "url") if item.get(name)]
        if len(sources) != 1:
            raise BridgeError(f"media {key} must define exactly one of path or url")
    featured = manifest.get("featured_media")
    if featured is not None and featured not in keys:
        raise BridgeError("featured_media must reference a media key")
    unknown = set(PLACEHOLDER_RE.findall(manifest["content"])) - keys
    if unknown:
        raise BridgeError("unknown media placeholders: " + ", ".join(sorted(unknown)))


def safe_repo_media_path(repo_root: Path, value: str) -> Path:
    allowed_root = (repo_root / "inbox" / "assets").resolve()
    candidate = (repo_root / value).resolve()
    if candidate != allowed_root and allowed_root not in candidate.parents:
        raise BridgeError("local media paths must stay under inbox/assets")
    if not candidate.is_file():
        raise BridgeError(f"media file does not exist: {value}")
    return candidate


def read_remote_media(url: str) -> tuple[bytes, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise BridgeError("remote media URLs must use https")
    request = urllib.request.Request(url, headers={"User-Agent": "bric-wordpress-bridge/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(MAX_MEDIA_BYTES + 1)
            mime_type = response.headers.get_content_type()
    except urllib.error.URLError as exc:
        raise BridgeError(f"cannot download media URL: {exc.reason}") from exc
    if len(data) > MAX_MEDIA_BYTES:
        raise BridgeError("remote media exceeds 12 MiB")
    return data, mime_type


def load_media(repo_root: Path, item: dict[str, Any]) -> tuple[bytes, str, str]:
    if item.get("path"):
        path = safe_repo_media_path(repo_root, str(item["path"]))
        data = path.read_bytes()
        filename = str(item.get("filename") or path.name)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    else:
        data, response_type = read_remote_media(str(item["url"]))
        default_name = Path(urllib.parse.urlparse(str(item["url"])).path).name or f"{item['key']}.jpg"
        filename = str(item.get("filename") or default_name)
        mime_type = response_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if len(data) > MAX_MEDIA_BYTES:
        raise BridgeError(f"media {item['key']} exceeds 12 MiB")
    if mime_type not in ALLOWED_MEDIA_TYPES:
        raise BridgeError(f"media {item['key']} has unsupported type {mime_type}")
    return data, mime_type, filename


def find_existing_post(client: WordPressClient, slug: str) -> dict[str, Any] | None:
    statuses = "publish,future,draft,pending,private"
    path = "/wp-json/wp/v2/posts?" + urllib.parse.urlencode(
        {"slug": slug, "status": statuses, "context": "edit", "per_page": 1}
    )
    posts = client.get(path)
    return posts[0] if posts else None


def resolve_category(client: WordPressClient, category: str) -> int:
    slug_path = "/wp-json/wp/v2/categories?" + urllib.parse.urlencode(
        {"slug": category, "per_page": 100}
    )
    matches = client.get(slug_path)
    if not matches:
        search_path = "/wp-json/wp/v2/categories?" + urllib.parse.urlencode(
            {"search": category, "per_page": 100}
        )
        matches = [item for item in client.get(search_path) if item.get("name", "").casefold() == category.casefold()]
    if len(matches) != 1:
        raise BridgeError(f"category must match exactly one existing WordPress category: {category}")
    return int(matches[0]["id"])


def publish_manifest(client: WordPressClient, repo_root: Path, manifest_path: Path) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    existing = find_existing_post(client, manifest["slug"])
    if existing:
        return f"SKIP slug={manifest['slug']} existing_id={existing['id']} status={existing['status']}"

    category_id = resolve_category(client, manifest["category"])
    uploaded: dict[str, dict[str, Any]] = {}
    for item in manifest.get("media", []):
        data, mime_type, filename = load_media(repo_root, item)
        media = client.upload_media(filename, mime_type, data)
        details: dict[str, Any] = {}
        if item.get("alt"):
            details["alt_text"] = str(item["alt"])
        if item.get("caption"):
            details["caption"] = str(item["caption"])
        if item.get("title"):
            details["title"] = str(item["title"])
        if details:
            media = client.post(f"/wp-json/wp/v2/media/{media['id']}", details)
        uploaded[item["key"]] = media

    content = PLACEHOLDER_RE.sub(lambda match: uploaded[match.group(1)]["source_url"], manifest["content"])
    post: dict[str, Any] = {
        "title": manifest["title"],
        "slug": manifest["slug"],
        "content": content,
        "excerpt": manifest.get("excerpt", ""),
        "categories": [category_id],
        "status": "pending",
        "comment_status": "closed",
        "ping_status": "closed",
    }
    featured_key = manifest.get("featured_media")
    if featured_key:
        post["featured_media"] = int(uploaded[featured_key]["id"])
    created = client.post("/wp-json/wp/v2/posts", post)
    if created.get("status") != "pending":
        raise BridgeError(f"WordPress returned unexpected status {created.get('status')!r}")
    return f"CREATED slug={manifest['slug']} post_id={created['id']} status=pending"


def manifest_paths(repo_root: Path, values: list[str]) -> list[Path]:
    if values:
        candidates = [(repo_root / value).resolve() for value in values]
    else:
        candidates = sorted((repo_root / "inbox").glob("*.json"))
    inbox_root = (repo_root / "inbox").resolve()
    for candidate in candidates:
        if inbox_root not in candidate.parents or candidate.suffix.lower() != ".json":
            raise BridgeError("manifests must be JSON files directly under inbox")
        if not candidate.is_file():
            raise BridgeError(f"manifest does not exist: {candidate}")
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifests", nargs="*")
    parser.add_argument("--test-connection", action="store_true")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    client = WordPressClient(Credentials.from_environment())
    me = client.get("/wp-json/wp/v2/users/me?context=edit")
    print(f"AUTH_OK user={me.get('slug') or me.get('name')} id={me.get('id')}")
    if args.test_connection:
        return 0
    paths = manifest_paths(repo_root, args.manifests)
    if not paths:
        raise BridgeError("No article manifests found under inbox")
    for path in paths:
        print(publish_manifest(client, repo_root, path))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BridgeError, json.JSONDecodeError) as exc:
        print(f"BRIDGE_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

