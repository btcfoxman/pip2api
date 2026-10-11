import pytest
import requests

from app.browser import BrowserError, _remove_stale_profile_locks, start_managed_browser
from app.media import MediaError, _DIRECT_MEDIA_UNTIL, _download


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
        status_code = 200

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


def test_public_caller_media_falls_back_to_direct_after_proxy_tls_failures(monkeypatch):
    routes = []
    _DIRECT_MEDIA_UNTIL.clear()
    monkeypatch.setattr("app.media.time.sleep", lambda _: None)

    class Response:
        headers = {"Content-Type": "image/png"}
        status_code = 200

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
            routes.append(kwargs["proxies"])
            if len(routes) < 3:
                raise requests.exceptions.SSLError("TLS connection closed")
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("app.media.requests.Session", Session)
    data, _, _ = _download("https://media.juxingdonghua.com/input.png", "images",
                           "socks5://host.docker.internal:20001")
    assert data == b"png"
    assert routes == [
        {"http": "socks5h://host.docker.internal:20001",
         "https": "socks5h://host.docker.internal:20001"},
        {"http": "socks5h://host.docker.internal:20001",
         "https": "socks5h://host.docker.internal:20001"},
        {},
    ]
    _download("https://media.juxingdonghua.com/next.png", "images",
              "socks5://host.docker.internal:20001")
    assert routes[-1] == {}
    assert len(routes) == 4
    _DIRECT_MEDIA_UNTIL.clear()


def test_pippit_media_does_not_bypass_account_proxy(monkeypatch):
    routes = []
    monkeypatch.setattr("app.media.time.sleep", lambda _: None)

    class Session:
        def get(self, _url, **kwargs):
            routes.append(kwargs["proxies"])
            raise requests.exceptions.SSLError("TLS connection closed")

        def close(self):
            pass

    monkeypatch.setattr("app.media.requests.Session", Session)
    with pytest.raises(MediaError, match="account proxy: SSLError"):
        _download("https://www.pippit.ai/input.png", "images",
                  "socks5://host.docker.internal:20001")
    assert len(routes) == 2
    assert all(route["https"] == "socks5h://host.docker.internal:20001" for route in routes)


def test_private_media_host_does_not_use_direct_fallback():
    from app.media import _allow_direct_media_fallback

    assert not _allow_direct_media_fallback("127.0.0.1")
    assert not _allow_direct_media_fallback("host.docker.internal")
    assert not _allow_direct_media_fallback("10.1.2.3")
    assert _allow_direct_media_fallback("assets.tokengo.love")
