import struct
import zlib

from app.browser import BrowserError, BrowserRejected
from app.media import _unique_image_copy, upload_media_bytes


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
