from app.client import PippitClient
from app.store import Store


def test_uploaded_asset_is_reused_for_same_account_and_content(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "pip.db"))
    account = {"id": 1, "cdp_port": 19301, "proxy_url": "socks5://example.com:20001",
               "project_id": "123"}
    uploads = []

    class Browser:
        def __init__(self, _port):
            pass

        def post(self, path, body):
            assert path == "/api/biz/v1/asset/query"
            assert body["project_id"] == 123
            return {"Assets": [{"PippitAssetID": body["pippit_asset_ids"][0],
                                "SourceID": "source-1", "AssetType": 1,
                                "Image": {"metadata": {}}}]}

    monkeypatch.setattr("app.client.CDPBrowser", Browser)
    monkeypatch.setattr("app.client._download", lambda *_: (b"same-content", "image/png", "x.png"))

    def upload(*_args, **_kwargs):
        uploads.append(True)
        return {"pippit_asset_id": "asset-1", "asset_id": "source-1",
                "mime": "image/png", "bytes": 12}

    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    client = PippitClient(account, store=store)
    normalized = {"images": ["https://example.com/x.png"], "videos": [], "audios": []}
    first = client.upload_inputs(normalized)
    second = client.upload_inputs(normalized)
    assert len(uploads) == 1
    assert first["images"] == second["images"]
    store.clear_media_cache(1)
    client.upload_inputs(normalized)
    assert len(uploads) == 2


def test_wrong_cached_video_asset_is_replaced_after_a_fresh_upload(tmp_path, monkeypatch):
    import hashlib

    store = Store(str(tmp_path / "pip.db"))
    account = {"id": 1, "cdp_port": 19301, "proxy_url": "socks5://example.com:20001",
               "project_id": "123"}
    data = b"video-content"
    digest = hashlib.sha256(data).hexdigest()
    store.media_cache_put(1, "videos", digest,
                          {"pippit_asset_id": "wrong-image", "mime": "video/mp4"})
    uploads = []

    class Browser:
        def __init__(self, _port):
            pass

        def post(self, _path, body):
            asset_id = body["pippit_asset_ids"][0]
            asset_type = 2 if asset_id == "correct-video" else 1
            return {"Assets": [{"PippitAssetID": asset_id, "SourceID": "source-1",
                                "AssetType": asset_type, "Name": "clip.mp4"}]}

    monkeypatch.setattr("app.client.CDPBrowser", Browser)
    monkeypatch.setattr("app.client._download", lambda *_: (data, "video/mp4", "clip.mp4"))

    def upload(*_args, **_kwargs):
        uploads.append(True)
        return {"pippit_asset_id": "wrong-image" if len(uploads) == 1 else "correct-video",
                "mime": "video/mp4"}

    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    assets = PippitClient(account, store=store).upload_inputs(
        {"images": [], "videos": ["https://example.com/clip.mp4"], "audios": []})
    assert len(uploads) == 2
    assert assets["videos"][0]["pippit_asset_id"] == "correct-video"
    assert store.media_cache_get(1, "videos", digest)["pippit_asset_id"] == "correct-video"


def test_wrong_image_content_is_not_reused_or_cached(tmp_path, monkeypatch):
    import hashlib

    store = Store(str(tmp_path / "pip.db"))
    account = {"id": 1, "cdp_port": 19301, "proxy_url": "socks5://example.com:20001",
               "project_id": "123"}
    data = b"image-a"
    digest = hashlib.sha256(data).hexdigest()
    store.media_cache_put(1, "images", digest,
                          {"pippit_asset_id": "wrong-image", "mime": "image/png", "bytes": len(data)})
    uploads = []

    class Browser:
        def __init__(self, _port):
            pass

        def post(self, _path, body):
            asset_id = body["pippit_asset_ids"][0]
            return {"Assets": [{"PippitAssetID": asset_id, "SourceID": "source-1",
                                "AssetType": 1, "Name": "image.png",
                                "Image": {"metadata": {"size": len(data) if asset_id == "right-image" else 100}}}]}

    monkeypatch.setattr("app.client.CDPBrowser", Browser)
    monkeypatch.setattr("app.client._download", lambda *_: (data, "image/png", "image.png"))

    def upload(*_args, **_kwargs):
        uploads.append(True)
        return {"pippit_asset_id": "wrong-image" if len(uploads) == 1 else "right-image",
                "mime": "image/png", "bytes": len(data)}

    monkeypatch.setattr("app.client.upload_media_bytes", upload)
    assets = PippitClient(account, store=store).upload_inputs(
        {"images": ["https://example.com/image.png"], "videos": [], "audios": []})
    assert len(uploads) == 2
    assert assets["images"][0]["pippit_asset_id"] == "right-image"
    assert store.media_cache_get(1, "images", digest)["pippit_asset_id"] == "right-image"
