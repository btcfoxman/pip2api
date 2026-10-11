import json

from app.store import Store


def test_concurrent_account_balance_deltas_are_excluded_from_cost_reference(tmp_path):
    store = Store(str(tmp_path / "pip.db"))
    normalized = json.dumps({"duration": 4, "resolution": "480p",
        "images": [], "videos": [], "audios": []})
    with store._connect() as db:
        for task_id, account_id, start, end, cost in (
            ("overlap-1", 1, 10, 20, 50),
            ("overlap-2", 1, 15, 25, 60),
            ("isolated", 2, 10, 20, 12),
        ):
            db.execute("""INSERT INTO tasks
                (id,account_id,model,status,request_json,normalized_json,
                 created_at,updated_at,completed_at,actual_cost)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (task_id, account_id, "sd-2-0-mini-480p", "succeeded", "{}",
                 normalized, start, end, end, cost))
    assert store.has_account_overlap(1, "overlap-1", 10, 20)
    assert not store.has_account_overlap(2, "isolated", 10, 20)
    costs = store.model_costs()
    assert len(costs) == 1
    assert costs[0]["cost"] == 12
    assert costs[0]["last_task_id"] == "isolated"
