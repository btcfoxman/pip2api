"""Credit failures switch accounts only after an explicit upstream rejection."""

from unittest.mock import Mock, patch

from app.browser import BrowserError
from app.catalog import normalize_request
from app.service import Service
from app.store import Store


def _setup(tmp_path, count=2):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    accounts = []
    clients = {}
    for index in range(count):
        account = store.upsert_account({"name": f"account-{index}",
            "cdp_port": 19301 + index, "proxy_url": "socks5://proxy.test:20001"})
        store.update_account(account["id"], {"status": "active", "balance": 100 - index})
        accounts.append(account["id"])
        client = Mock()
        client.account_state.return_value = {"available_balance": 100 - index}
        client.upload_inputs.return_value = {"images": [], "videos": [], "audios": []}
        client.submit.return_value = {"run_id": f"run-{index}", "thread_id": f"thread-{index}"}
        client.poll.return_value = {"status": "succeeded",
            "result_urls": ["https://example.test/result.mp4"]}
        clients[account["id"]] = client
    request = {"model": "sd-2-0-mini", "prompt": "A blue sky", "duration": 4}
    store.create_task("pip_credit_test", request, normalize_request(request))
    return store, service, accounts, clients


def _run(service, clients):
    with patch.object(service.browser_runtime, "acquire", side_effect=lambda account: (account, False)), \
         patch.object(service.browser_runtime, "release"), \
         patch("app.service.PippitClient", side_effect=lambda account, **_: clients[account["id"]]):
        service._run("pip_credit_test")


def test_terminal_credit_failure_switches_to_second_account(tmp_path):
    store, service, accounts, clients = _setup(tmp_path)
    clients[accounts[0]].poll.return_value = {
        "status": "failed", "error": "credit not enough", "error_code": ""}
    _run(service, clients)
    task = store.task("pip_credit_test")
    assert task["status"] == "succeeded"
    assert task["id"] == "pip_credit_test"
    assert task["account_id"] == accounts[1]
    assert task["attempted_account_ids"] == accounts
    clients[accounts[0]].submit.assert_called_once()
    clients[accounts[1]].submit.assert_called_once()
    service.stop()


def test_two_credit_failures_stop_before_third_account(tmp_path):
    store, service, accounts, clients = _setup(tmp_path, count=3)
    for client in clients.values():
        client.poll.return_value = {
            "status": "failed", "error": "credit not enough", "error_code": ""}
    _run(service, clients)
    task = store.task("pip_credit_test")
    assert task["status"] == "failed"
    assert task["error_code"] == "INSUFFICIENT_CREDITS"
    assert len(task["attempted_account_ids"]) == 2
    assert sum(client.submit.call_count for client in clients.values()) == 2
    service.stop()


def test_ambiguous_submit_outcome_does_not_switch(tmp_path):
    store, service, accounts, clients = _setup(tmp_path)
    clients[accounts[0]].submit.side_effect = BrowserError("network closed during submission")
    _run(service, clients)
    task = store.task("pip_credit_test")
    assert task["error_code"] == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"
    assert clients[accounts[0]].submit.call_count == 1
    assert clients[accounts[1]].submit.call_count == 0
    service.stop()


def test_resumed_terminal_credit_failure_requeues_same_task(tmp_path):
    store, service, accounts, clients = _setup(tmp_path)
    store.update_task("pip_credit_test", {"status": "submitted", "account_id": accounts[0],
        "run_id": "run-0", "thread_id": "thread-0",
        "attempted_account_ids": [accounts[0]]})
    clients[accounts[0]].poll.return_value = {
        "status": "failed", "error": "credit not enough", "error_code": ""}
    scheduled = []
    with patch.object(service.browser_runtime, "acquire", side_effect=lambda account: (account, False)), \
         patch.object(service.browser_runtime, "release"), \
         patch("app.service.PippitClient", side_effect=lambda account, **_: clients[account["id"]]), \
         patch.object(service._pool, "submit", side_effect=lambda fn, task_id: scheduled.append(task_id)):
        service._resume_poll("pip_credit_test")
    task = store.task("pip_credit_test")
    assert task["status"] == "queued"
    assert task["run_id"] == ""
    assert task["attempted_account_ids"] == [accounts[0]]
    assert scheduled == ["pip_credit_test"]
    _run(service, clients)
    assert store.task("pip_credit_test")["status"] == "succeeded"
    assert store.task("pip_credit_test")["attempted_account_ids"] == accounts
    service.stop()
