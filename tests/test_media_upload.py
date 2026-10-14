import struct
import io
import os
import zlib

from PIL import Image

from app.browser import BrowserError, BrowserRejected
from app.media import _unique_image_copy, _unique_mp4_copy, upload_media_bytes


def _chunk(name: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + name + body
            + struct.pack(">I", zlib.crc32(name + body) & 0xffffffff))


def _png() -> bytes:
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
            + _chunk(b"caBX", b"original provenance")
            + _chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\x40\x50\x60"))
            + _chunk(b"IEND", b""))


def test_duplicate_png_retry_keeps_pixels_and_existing_metadata():
    original = _png()
    observed = []

    class Browser:
        def upload_file(self, path):
            observed.append(path.read_bytes())
            if len(observed) == 1:
                raise BrowserRejected("Pippit ret=1: Something went wrong. Try again.")
            return {"PippitAssetID": "asset-test", "SourceID": "source-test"}

    result = upload_media_bytes(original, "image/png", "input.png", "images", browser=Browser())
    assert observed[0] == original
    assert observed[1] != original
    assert b"caBX" in observed[1]
    assert _chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\x40\x50\x60")) in observed[1]
    assert b"tEXt" in observed[1]
    assert result["pippit_asset_id"] == "asset-test"
    assert result["bytes"] == len(observed[1])


def test_unrecognized_image_is_not_rewritten():
    assert _unique_image_copy(b"unknown", "image/png") is None


def test_duplicate_mp4_retry_preserves_existing_boxes_and_samples():
    original = (struct.pack(">I4s", 12, b"ftyp") + b"isom"
                + struct.pack(">I4s", 12, b"mdat") + b"data")
    observed = []

    class Browser:
        def upload_file(self, path):
            observed.append(path.read_bytes())
            if len(observed) == 1:
                raise BrowserRejected("Pippit ret=1: Something went wrong. Try again.")
            return {"PippitAssetID": "video-test"}

    result = upload_media_bytes(original, "video/mp4", "input.mp4", "videos", browser=Browser())
    assert observed[0] == original
    assert observed[1].startswith(original)
    assert observed[1][len(original) + 4:len(original) + 8] == b"free"
    assert result["bytes"] == len(observed[1])
    assert result["pippit_asset_id"] == "video-test"
    assert _unique_mp4_copy(b"not an mp4", "video/mp4") is None


def test_incomplete_asset_registration_retries_once_with_a_fresh_input_path():
    seen = []

    class Browser:
        def upload_file(self, path):
            seen.append((path.name, path.read_bytes()))
            if len(seen) == 1:
                raise BrowserError("Pippit asset creation did not complete (prepare=1, chunks=4)")
            return {"PippitAssetID": "asset-after-retry"}

    original = _png()
    result = upload_media_bytes(original, "image/png", "input.png", "images", browser=Browser())
    assert result["pippit_asset_id"] == "asset-after-retry"
    assert [name for name, _ in seen] == ["images.png", "images-retry.png"]
    assert seen[0][1] == seen[1][1] == original


def test_incomplete_asset_registration_never_loops_forever():
    calls = 0

    class Browser:
        def upload_file(self, path):
            nonlocal calls
            calls += 1
            raise BrowserError("Pippit asset creation did not complete")

    try:
        upload_media_bytes(_png(), "image/png", "input.png", "images", browser=Browser())
    except Exception as exc:
        assert "asset creation did not complete" in str(exc)
    else:
        raise AssertionError("expected the bounded upload failure")
    assert calls == 2


def test_large_png_is_uploadable_without_waiting_on_browser_file_processing():
    pixels = os.urandom(2600 * 2600 * 3)
    source = io.BytesIO()
    Image.frombytes("RGB", (2600, 2600), pixels).save(source, format="PNG", compress_level=0)
    assert len(source.getvalue()) > 19 * 1024 * 1024
    seen = []

    class Browser:
        def upload_file(self, path):
            with Image.open(path) as image:
                image.verify()
            seen.append((path.suffix, path.stat().st_size))
            return {"PippitAssetID": "large-image-asset"}

    result = upload_media_bytes(source.getvalue(), "image/png", "large.png", "images", browser=Browser())
    assert result["pippit_asset_id"] == "large-image-asset"
    assert seen[0][1] <= 14 * 1024 * 1024
    assert seen[0][0] == ".jpg"
    assert result["mime"] == "image/jpeg"
