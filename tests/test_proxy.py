import pytest

from app.browser import BrowserError, _remove_stale_profile_locks, start_managed_browser
from app.media import _download


def test_managed_browser_requires_account_proxy(tmp_path):
    with pytest.raises(BrowserError, match="account proxy must be"):
        start_managed_browser({"id": 1, "proxy_url": ""}, chrome="chromium", profiles=tmp_path)


def test_stale_chromium_singleton_links_are_removed(tmp_path):
    try:
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (tmp_path / name).symlink_to("old-container")
    except OSError:
        pytest.skip("this Windows user cannot create symbolic links")
    keep = tmp_path / "Preferences"
    keep.write_text("saved login state", encoding="utf-8")
    _remove_stale_profile_locks(tmp_path)
    assert not any((tmp_path / name).is_symlink() for name in
                   ("SingletonLock", "SingletonSocket", "SingletonCookie"))
    assert keep.read_text(encoding="utf-8") == "saved login state"


def test_media_download_uses_account_proxy_only(monkeypatch):
    observed = {}

    class Response:
        headers = {"Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            pass

        def iter_content(self, *_):
            yield b"png"

    class Session:
        def get(self, _url, **kwargs):
            observed.update(kwargs)
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("app.media.requests.Session", Session)
    data, _, _ = _download("https://example.com/input.png", "images",
                           "socks5://host.docker.internal:20001")
    assert data == b"png"
    assert observed["proxies"] == {
        "http": "socks5h://host.docker.internal:20001",
        "https": "socks5h://host.docker.internal:20001",
    }
