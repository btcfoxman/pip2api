"""A missing account pool rejects submissions instead of filling the queue."""

from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from app.catalog import normalize_request
from app.service import AccountPoolUnavailable, Service, _estimate_cost
from app.store import Store


REQUEST = {"model": "sd-2-0-mini", "prompt": "A blue sky", "duration": 4}


def _fast_costs():
    return [{"model": "sd-2-0-fast", "resolution": "720p", "duration": duration,
             "image_count": image_count, "video_count": 0, "audio_count": 0,
             "cost": duration * 8}
            for duration, image_count in ((4, 3), (6, 2), (15, 4))]


def test_no_enabled_accounts_rejects_before_creating_a_task(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    account = store.upsert_account({"name": "disabled", "cdp_port": 19301,
        "proxy_url": "socks5://proxy.test:20001"})
    store.update_account(account["id"], {"enabled": False, "status": "active"})

    with pytest.raises(AccountPoolUnavailable, match="no available Pippit account"):
        service.create(REQUEST)

    assert store.active_count() == 0
    assert store.tasks() == []
    service.stop()


def test_insufficient_observed_balance_rejects_before_queueing(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    account = store.upsert_account({"name": "low-balance", "cdp_port": 19301,
        "proxy_url": "socks5://proxy.test:20001"})
    store.update_account(account["id"], {"status": "active", "balance": 2})
    cost = {"model": "sd-2-0-mini", "duration": 4, "resolution": "720p",
            "image_count": 0, "video_count": 0, "audio_count": 0, "cost": 5}

    with patch.object(store, "model_costs", return_value=[cost]), \
         pytest.raises(AccountPoolUnavailable):
        service.create(REQUEST)

    assert store.tasks() == []
    service.stop()


def test_stable_observed_rate_selects_funded_account_for_unseen_image_count(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    low = store.upsert_account({"name": "low", "cdp_port": 19301,
        "proxy_url": "socks5://proxy.test:20001", "priority": 2})
    high = store.upsert_account({"name": "funded", "cdp_port": 19302,
        "proxy_url": "socks5://proxy.test:20001", "priority": 0})
    store.update_account(low["id"], {"status": "active", "balance": 23})
    store.update_account(high["id"], {"status": "active", "balance": 500})
    request = {"model": "sd-2-0-fast", "prompt": "A blue sky", "duration": 15}
    normalized = normalize_request(request)
    normalized["images"] = ["https://example.test/reference.png"] * 6

    with patch("app.service.normalize_request", return_value=normalized), \
         patch.object(store, "model_costs", return_value=_fast_costs()), \
         patch.object(service._pool, "submit"):
        task = service.create(request)

    assert store.task(task["id"])["estimated_cost"] == 120
    assert service._reserve_account(120)["id"] == high["id"]
    service._release_account(high["id"])
    service.stop()


def test_old_queued_task_reestimates_before_account_selection(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    low = store.upsert_account({"name": "low", "cdp_port": 19301,
        "proxy_url": "socks5://proxy.test:20001", "priority": 2})
    high = store.upsert_account({"name": "funded", "cdp_port": 19302,
        "proxy_url": "socks5://proxy.test:20001", "priority": 0})
    store.update_account(low["id"], {"status": "active", "balance": 23})
    store.update_account(high["id"], {"status": "active", "balance": 500})
    request = {"model": "sd-2-0-fast", "prompt": "A blue sky", "duration": 15}
    normalized = normalize_request(request)
    normalized["images"] = ["https://example.test/reference.png"] * 6
    store.create_task("old-queued", request, normalized)
    client = Mock()
    client.account_state.return_value = {"available_balance": 500}
    client.upload_inputs.return_value = {"images": [], "videos": [], "audios": []}
    client.submit.return_value = {"run_id": "run", "thread_id": "thread"}
    client.poll.return_value = {"status": "succeeded", "result_urls": ["https://example.test/result.mp4"]}

    with patch.object(store, "model_costs", return_value=_fast_costs()), \
         patch.object(service.browser_runtime, "acquire", side_effect=lambda account: (account, False)), \
         patch.object(service.browser_runtime, "release"), \
         patch("app.service.PippitClient", return_value=client):
        service._run("old-queued")

    task = store.task("old-queued")
    assert task["status"] == "succeeded"
    assert task["estimated_cost"] == 120
    assert task["account_id"] == high["id"]
    assert task["attempted_account_ids"] == [high["id"]]
    service.stop()


def test_unstable_rates_do_not_guess_an_unseen_cost():
    normalized = normalize_request({"model": "sd-2-0-fast", "prompt": "Sky", "duration": 15})
    normalized["images"] = ["https://example.test/reference.png"] * 6
    costs = _fast_costs()
    costs[1]["cost"] = 100
    assert _estimate_cost(normalized, costs) == 0


def test_busy_eligible_account_can_still_queue(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    account = store.upsert_account({"name": "busy", "cdp_port": 19301,
        "proxy_url": "socks5://proxy.test:20001"})
    store.update_account(account["id"], {"status": "active", "balance": 100})
    service._active[account["id"]] = 1

    with patch.object(service._pool, "submit") as submit:
        task = service.create(REQUEST)

    assert task["status"] == "queued"
    submit.assert_called_once()
    service.stop()


def test_old_queued_task_fails_immediately_when_pool_disappears(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    store.create_task("old-task", REQUEST, normalize_request(REQUEST))
    service = Service(store)

    service._run("old-task")

    task = store.task("old-task")
    assert task["status"] == "failed"
    assert task["error_code"] == "ACCOUNT_POOL_UNAVAILABLE"
    service.stop()


def test_http_rejection_explicitly_says_upstream_submission_not_accepted(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("PIP_API_KEY", "test-api-key")
    monkeypatch.setenv("PIP_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("PIP_DATA_DIR", str(tmp_path))
    main = importlib.import_module("app.main")

    with pytest.raises(HTTPException) as caught:
        main._create(REQUEST)

    assert caught.value.status_code == 503
    assert caught.value.headers["X-PIPAPI-Submit-Outcome"] == "not-accepted"
    assert main.store.tasks() == []
    main.service.stop()
