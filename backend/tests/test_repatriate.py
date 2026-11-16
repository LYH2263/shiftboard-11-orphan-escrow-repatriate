"""归位任务与孤儿隔离的回归测试（独立库，避免污染冒烟测试的库）。"""

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import repatriate as rep_svc  # noqa: E402

TEST_DB = ROOT / "backend" / "data_test_repatriate" / "app.db"
if TEST_DB.exists():
    TEST_DB.unlink()


@pytest.fixture(scope="module")
def client():
    db.DATA_DIR = TEST_DB.parent
    db.DB_PATH = TEST_DB
    with TestClient(app) as c:
        yield c


def _conn():
    db.DATA_DIR = TEST_DB.parent
    db.DB_PATH = TEST_DB
    return db.connect()


def _batch(client, biz_key):
    rows = client.get("/api/batches").json()
    return next(b for b in rows if b["biz_key"] == biz_key)


def test_scan_reports_sources_of_truth(client):
    scan = client.get("/api/repatriate/scan").json()
    assert scan["scanned_count"] >= 7
    assert scan["repatriate_candidates"] >= 1  # 托管批 6，挂靠单不存在
    assert scan["quarantine_candidates"] >= 1  # 孤儿键 7
    actions = {(i["biz_key"], i["action"]) for i in scan["items"]}
    assert ("B20261112-07", "归位") in actions
    assert ("B20261111-04", "隔离") in actions
    # 扫描是只读的
    assert _batch(client, "B20261112-07")["status"] == "托管"
    assert _batch(client, "B20261111-04")["status"] == "在途"


def test_run_repatriates_and_quarantines(client):
    before = client.get("/api/repatriate/snapshot").json()
    total_batches = len(client.get("/api/batches").json())
    run = client.post("/api/repatriate/run").json()
    assert run["status"] == "已完成"
    assert run["scanned_count"] == total_batches
    assert run["repatriated_count"] >= 1
    assert run["quarantined_count"] >= 1

    rep = _batch(client, "B20261112-07")
    assert rep["status"] == "在途"
    assert rep["owner_shift_id"] == 1  # 回交班方，而不是接班方
    assert rep["escrow_handover_id"] is None

    orphan = _batch(client, "B20261111-04")
    assert orphan["status"] == "孤儿隔离"
    assert orphan["orphan_reason"]
    assert orphan["orphan_prev_status"] == "在途"

    quarantine = client.get("/api/repatriate/quarantine").json()
    assert any(b["biz_key"] == "B20261111-04" for b in quarantine)
    assert client.get("/api/repatriate/escrow").json() == []

    after = client.get("/api/repatriate/snapshot").json()
    assert after["escrow_count"] == 0
    assert after["inflight_count"] == before["inflight_count"] + run["repatriated_count"]
    assert after["quarantine_count"] == before["quarantine_count"] + run["quarantined_count"]
    detail = client.get(f"/api/repatriate/runs/{run['id']}").json()
    assert detail["items"]
    assert {i["action"] for i in detail["items"]} <= {"归位", "隔离", "保留"}


def test_inconsistent_completion_reverts_to_from_shift(client):
    started = client.post(
        "/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 3}
    )
    assert started.status_code == 200, started.text
    hid = started.json()["id"]
    # 人为把一个托管批踢出托管，制造「清单有批但没托管住」
    with _conn() as conn:
        row = conn.execute(
            "SELECT batch_id FROM handover_items WHERE handover_id = ? LIMIT 1", (hid,)
        ).fetchone()
        conn.execute(
            "UPDATE batches SET status = '在途', escrow_handover_id = NULL WHERE id = ?",
            (row["batch_id"],),
        )
        conn.commit()

    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "失败"
    # 已过户的批必须整批退回交班方，不能留在接班方形成双轨
    to_batches = client.get("/api/batches", params={"owner_shift_id": 3}).json()
    assert not any(b["status"] == "在途" for b in to_batches)


def test_active_handover_escrow_is_not_repatriated(client):
    started = client.post(
        "/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 2}
    )
    assert started.status_code == 200, started.text
    hid = started.json()["id"]
    # 开单即托管
    escrow = client.get("/api/repatriate/escrow").json()
    assert escrow
    assert all(b["escrow_handover_id"] == hid for b in escrow)

    run = client.post("/api/repatriate/run").json()
    assert run["status"] == "已完成"
    assert run["repatriated_count"] == 0
    assert run["kept_count"] == len(escrow)
    # 进行中单的托管批不得被清理回在途
    after = client.get("/api/repatriate/escrow").json()
    assert len(after) == len(escrow)
    assert _batch(client, "B20261112-07")["status"] == "托管"

    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "已完成"
    assert client.get("/api/repatriate/escrow").json() == []
    to_batches = client.get("/api/batches", params={"owner_shift_id": 2}).json()
    assert sum(1 for b in to_batches if b["status"] == "在途") >= len(escrow)


def test_cancel_releases_escrow(client):
    started = client.post(
        "/api/handovers/start", json={"from_shift_id": 2, "to_shift_id": 3}
    )
    assert started.status_code == 200, started.text
    hid = started.json()["id"]
    assert client.get("/api/repatriate/escrow").json()

    cancelled = client.post(f"/api/handovers/{hid}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "已取消"
    assert client.get("/api/repatriate/escrow").json() == []

    run = client.post("/api/repatriate/run").json()
    assert run["status"] == "已完成"
    # 取消时已整批回交班方在途，归位任务无残留可归位
    assert run["repatriated_count"] == 0
    assert run["kept_count"] == 0
    inflight = client.get("/api/batches", params={"owner_shift_id": 2}).json()
    assert any(b["status"] == "在途" for b in inflight)
    # 回的是交班方（2），不是接班方（3）；托管集合已清空
    assert client.get("/api/repatriate/escrow").json() == []


def test_failed_write_rolls_back_whole_run(client, monkeypatch):
    # 造一个托管批，然后让回写必然失败，验证整趟回滚
    with _conn() as conn:
        conn.execute(
            "UPDATE batches SET status = '托管', escrow_handover_id = 99, escrow_at = 'x', "
            "owner_shift_id = 3 WHERE biz_key = 'B20261111-04'"
        )
        conn.commit()

    real_apply = rep_svc._apply

    def boom(conn, run_id, item, now):
        if item["action"] == "归位":
            raise rep_svc.WriteFailed("模拟回写失败", item["batch_id"])
        return real_apply(conn, run_id, item, now)

    monkeypatch.setattr(rep_svc, "_apply", boom)

    snap_before = client.get("/api/repatriate/snapshot").json()
    r = client.post("/api/repatriate/run")
    assert r.status_code == 500
    assert "已回滚" in r.json()["detail"]
    snap_after = client.get("/api/repatriate/snapshot").json()
    assert snap_after == snap_before
    assert _batch(client, "B20261111-04")["status"] == "托管"

    runs = client.get("/api/repatriate/runs").json()
    assert runs[0]["status"] == "失败"
    assert runs[0]["repatriated_count"] == 0
    assert runs[0]["failed_batch_id"] is not None

    # 恢复回写后重跑，确认隔离批仍可被正常处理（失败没有留下半截写入）
    monkeypatch.setattr(rep_svc, "_apply", real_apply)
    rerun = client.post("/api/repatriate/run").json()
    assert rerun["repatriated_count"] == 1
    assert _batch(client, "B20261111-04")["status"] == "在途"
