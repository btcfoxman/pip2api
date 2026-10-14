"""Only pre-submit tasks can be resumed without duplicating upstream runs."""

from unittest.mock import patch

from app.service import Service
from app.store import Store


def test_restart_requeues_uploading_task_but_not_ambiguous_submission(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    normalized = {"model": "sd-2-0-mini", "upstream_model": "seedance_2_0_mini",
                  "duration": 4, "resolution": "480p", "aspect_ratio": "16:9",
                  "images": [], "videos": [], "audios": []}
    store.create_task("pre-upload", {}, normalized)
    store.update_task("pre-upload", {"status": "preparing", "progress": 12, "account_id": 2})
    store.create_task("mid-submit", {}, normalized)
    store.update_task("mid-submit", {"status": "preparing", "progress": 30, "account_id": 2})
    service = Service(store)
    submitted = []
    with patch.object(service, "_maintain_accounts"), patch.object(
        service._pool, "submit", side_effect=lambda fn, task_id: submitted.append(task_id)
    ):
        service.start()
    service.stop()

    assert store.task("pre-upload")["status"] == "queued"
    assert store.task("pre-upload")["account_id"] is None
    assert submitted == ["pre-upload"]
    assert store.task("mid-submit")["status"] == "failed"
    assert store.task("mid-submit")["error_code"] == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"
