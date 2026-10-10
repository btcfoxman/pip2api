"""Download caller media and upload it through an authenticated Pippit browser."""

from __future__ import annotations

import base64
import mimetypes
import tempfile
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
        session = requests.Session()
        session.trust_env = False
        try:
            with session.get(source, timeout=120, stream=True,
                             proxies={"http": proxy, "https": proxy},
                             headers={"User-Agent": "Mozilla/5.0"}) as response:
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
        except requests.RequestException as exc:
            raise MediaError(f"media download failed through account proxy: {type(exc).__name__}") from exc
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
