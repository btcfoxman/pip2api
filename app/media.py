"""Download caller media and upload it through an authenticated Pippit browser."""

from __future__ import annotations

import base64
import ipaddress
import mimetypes
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from .browser import BrowserError, CDPBrowser


MAX_MEDIA_BYTES = 200 * 1024 * 1024
MIME_EXTENSIONS = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "image/jpeg": ".jpg",
                   "image/png": ".png", "video/mp4": ".mp4", "video/quicktime": ".mov"}


class MediaError(Exception):
    pass


def _allow_direct_media_fallback(host: str | None) -> bool:
    """Keep browser/upstream and local URLs on the account proxy."""
    if not host or host == "pippit.ai" or host.endswith(".pippit.ai"):
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def _download(source: str, kind: str, proxy_url: str = "") -> tuple[bytes, str, str]:
    if source.startswith("data:"):
        header, separator, payload = source.partition(",")
        if not separator or ";base64" not in header:
            raise MediaError("media data URL must be base64 encoded")
        mime = header[5:].split(";", 1)[0].lower()
        try:
            data = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise MediaError("media data URL contains invalid base64") from exc
        filename = kind + MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
    elif source.startswith(("https://", "http://")):
        parsed = urlsplit(source)
        if not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("media URL is invalid")
        if not proxy_url:
            raise MediaError("account proxy is required for media download")
        # Requests' socks5h form resolves the destination through the same
        # account proxy that Chromium uses for Pippit traffic.
        proxy = proxy_url.replace("socks5://", "socks5h://", 1)
        account_route = {"http": proxy, "https": proxy}
        routes = [("account proxy", account_route), ("account proxy", account_route)]
        # Caller-owned public media can use a direct route if the account proxy
        # repeatedly fails; Pippit and local hosts never use that route.
        if _allow_direct_media_fallback(parsed.hostname):
            routes.append(("direct media fallback", {}))
        for index, (route_name, route_proxy) in enumerate(routes):
            session = requests.Session()
            session.trust_env = False
            try:
                with session.get(source, timeout=(20, 120), stream=True,
                                 proxies=route_proxy,
                                 allow_redirects=bool(route_proxy),
                                 headers={"User-Agent": "Mozilla/5.0"}) as response:
                    if not route_proxy and 300 <= response.status_code < 400:
                        raise MediaError("direct media fallback redirected; account proxy is required")
                    response.raise_for_status()
                    chunks = []
                    total = 0
                    for chunk in response.iter_content(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_MEDIA_BYTES:
                            raise MediaError("reference media exceeds 200 MB")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    mime = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
                    filename = Path(parsed.path).name or kind + MIME_EXTENSIONS.get(mime, ".bin")
                    break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                if index == len(routes) - 1:
                    raise MediaError(f"media download failed via {route_name}: {type(exc).__name__}") from exc
                time.sleep(0.5 * (index + 1))
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status not in {429, 500, 502, 503, 504} or index == len(routes) - 1:
                    raise MediaError(f"media download failed via {route_name}: HTTP {status}") from exc
                time.sleep(0.5 * (index + 1))
            except requests.RequestException as exc:
                raise MediaError(f"media download failed via {route_name}: {type(exc).__name__}") from exc
            finally:
                session.close()
    else:
        raise MediaError("media must be an HTTPS URL, HTTP URL, data URL, or Pippit asset ID")
    if not data:
        raise MediaError("reference media is empty")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("reference media exceeds 200 MB")
    if not mime.startswith({"images": "image/", "videos": "video/", "audios": "audio/"}[kind]):
        guessed = mimetypes.guess_type(filename)[0] or ""
        if not guessed.startswith({"images": "image/", "videos": "video/", "audios": "audio/"}[kind]):
            raise MediaError(f"{kind} reference has the wrong content type")
        mime = guessed
    return data, mime, filename


def upload_media(source: str, kind: str, *, browser: CDPBrowser,
                 proxy_url: str = "") -> dict[str, Any]:
    if source.startswith("pippit:"):
        asset_id = source.removeprefix("pippit:").strip("/")
        if not asset_id:
            raise MediaError("Pippit asset ID is empty")
        return {"pippit_asset_id": asset_id}
    data, mime, filename = _download(source, kind, proxy_url)
    suffix = Path(filename).suffix.lower() or MIME_EXTENSIONS.get(mime, ".bin")
    try:
        with tempfile.TemporaryDirectory(prefix="pip2api-media-") as root:
            path = Path(root) / (kind + suffix)
            path.write_bytes(data)
            result = browser.upload_file(path)
    except BrowserError as exc:
        raise MediaError(f"Pippit web upload failed: {exc}") from exc
    asset_id = result.get("PippitAssetID") or result.get("pippit_asset_id")
    if not asset_id:
        raise MediaError("Pippit web upload returned no PippitAssetID")
    return {"pippit_asset_id": asset_id,
            "asset_id": result.get("SourceID") or result.get("asset_id") or "",
            "mime": mime, "bytes": len(data)}
