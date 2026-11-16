"""归位任务：清理中断交班遗留的托管批，并把无主键送入孤儿隔离区。

约定（与主链同源）：
- 托管集合：batches.status = '托管'
- 在途集合：batches.status = '在途' 且归属班次有效
- 孤儿隔离区：batches.status = '孤儿隔离'
- 归位/隔离只按上述状态机落库，不删除任何批次，也不把无主键塞给某个班次冒充在途。

整趟归位写入 + 扫描报告在同一事务内完成；任一批次回写失败则整体回滚，任务失败。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.services.collections import (
    ESCROW,
    INFLIGHT,
    ORPHAN,
    SETTLED,
    count_by_status,
    escrow_count,
    inflight_count,
    orphan_count,
)

HANDOVER_ACTIVE = "进行中"
HANDOVER_RELEASABLE = ("失败", "已取消")

ACTION_REPATRIATE = "归位"
ACTION_QUARANTINE = "隔离"
ACTION_KEEP = "保留"


class WriteFailed(RuntimeError):
    """单批回写失败；携带批次号，便于失败报告定位。"""

    def __init__(self, message: str, batch_id: int | None = None):
        super().__init__(message)
        self.batch_id = batch_id


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_shift_ids(conn) -> set[int]:
    return {r["id"] for r in conn.execute("SELECT id FROM shifts").fetchall()}


def _escrow_rows(conn) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT b.*, h.status AS handover_status, h.from_shift_id AS handover_from_shift_id
            FROM batches b
            LEFT JOIN handovers h ON h.id = b.escrow_handover_id
            WHERE b.status = ?
            ORDER BY b.id
            """,
            (ESCROW,),
        ).fetchall()
    )


def _orphan_rows(conn, valid_shift_ids: set[int]) -> list[dict]:
    """无主键：不在任何班次在途、不在托管、未结清。"""
    out = []
    for b in rows_to_dicts(conn.execute("SELECT * FROM batches ORDER BY id").fetchall()):
        if b["status"] in (ESCROW, SETTLED, ORPHAN):
            continue
        if b["status"] == INFLIGHT and b["owner_shift_id"] in valid_shift_ids:
            continue
        out.append(b)
    return out


def _plan(conn) -> dict:
    """同一套判据同时服务预扫描与执行，保证报告与集合同源。"""
    valid = _valid_shift_ids(conn)
    escrows = _escrow_rows(conn)
    orphans = _orphan_rows(conn, valid)

    items: list[dict] = []
    for b in escrows:
        hid = b["escrow_handover_id"]
        hs = b["handover_status"]
        base = {
            "batch_id": b["id"],
            "biz_key": b["biz_key"],
            "title": b["title"],
            "escrow_handover_id": hid,
            "owner_shift_id": b["owner_shift_id"],
            "prev_status": b["status"],
            "target_shift_id": None,
        }
        if hs is None:
            target = b["owner_shift_id"] if b["owner_shift_id"] in valid else None
            if target is None:
                items.append(
                    {
                        **base,
                        "action": ACTION_QUARANTINE,
                        "reason": "托管批挂靠的交班单不存在，且无有效交班方可回，转入孤儿隔离",
                    }
                )
            else:
                items.append(
                    {
                        **base,
                        "action": ACTION_REPATRIATE,
                        "target_shift_id": target,
                        "reason": f"挂靠交班单#{hid}不存在，整批回交班方在途并解除托管",
                    }
                )
        elif hs == HANDOVER_ACTIVE:
            items.append(
                {
                    **base,
                    "action": ACTION_KEEP,
                    "reason": f"交班单#{hid}仍进行中，不得擅自归位",
                }
            )
        elif hs in HANDOVER_RELEASABLE:
            target = b["handover_from_shift_id"]
            if target not in valid:
                target = b["owner_shift_id"] if b["owner_shift_id"] in valid else None
            if target is None:
                items.append(
                    {
                        **base,
                        "action": ACTION_QUARANTINE,
                        "reason": f"交班单#{hid}已{hs}，但交班方已不存在，转入孤儿隔离",
                    }
                )
            else:
                items.append(
                    {
                        **base,
                        "action": ACTION_REPATRIATE,
                        "target_shift_id": target,
                        "reason": f"挂靠交班单#{hid}已{hs}，整批回交班方在途并解除托管",
                    }
                )
        else:
            items.append(
                {
                    **base,
                    "action": ACTION_KEEP,
                    "reason": f"交班单#{hid}状态为{hs}，不在可归位范围，留待人工核查",
                }
            )

    for b in orphans:
        if b["owner_shift_id"] not in valid:
            reason = "在途批无有效归属班次，不在任何班次在途"
        else:
            reason = f"批次状态「{b['status']}」不在在途/托管/已结清之列"
        items.append(
            {
                "batch_id": b["id"],
                "biz_key": b["biz_key"],
                "title": b["title"],
                "escrow_handover_id": b["escrow_handover_id"],
                "owner_shift_id": b["owner_shift_id"],
                "prev_status": b["status"],
                "target_shift_id": None,
                "action": ACTION_QUARANTINE,
                "reason": reason,
            }
        )

    scanned = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
    return {
        "scanned_count": scanned,
        "escrow_candidates": len(escrows),
        "orphan_candidates": len(orphans),
        "inflight_before": inflight_count(conn),
        "quarantined_before": orphan_count(conn),
        "items": items,
    }


def scan() -> dict:
    with connect() as conn:
        plan = _plan(conn)
    items = plan.pop("items")
    return {
        **plan,
        "repatriate_candidates": sum(1 for i in items if i["action"] == ACTION_REPATRIATE),
        "quarantine_candidates": sum(1 for i in items if i["action"] == ACTION_QUARANTINE),
        "keep_candidates": sum(1 for i in items if i["action"] == ACTION_KEEP),
        "items": items,
    }


def _apply(conn, run_id: int, item: dict, now: str) -> None:
    """回写单批；rowcount 不为 1 即视为回写失败，抛错触发整趟回滚。"""
    bid = item["batch_id"]
    if item["action"] == ACTION_REPATRIATE:
        cur = conn.execute(
            """
            UPDATE batches
            SET status = ?, owner_shift_id = ?, escrow_handover_id = NULL, escrow_at = NULL
            WHERE id = ? AND status = ? AND IFNULL(escrow_handover_id, -1) = ?
            """,
            (
                INFLIGHT,
                item["target_shift_id"],
                bid,
                ESCROW,
                -1 if item["escrow_handover_id"] is None else item["escrow_handover_id"],
            ),
        )
    elif item["action"] == ACTION_QUARANTINE:
        cur = conn.execute(
            """
            UPDATE batches
            SET status = ?, orphan_reason = ?, orphaned_at = ?, orphan_run_id = ?,
                orphan_prev_status = ?, escrow_handover_id = NULL, escrow_at = NULL
            WHERE id = ? AND status = ? AND IFNULL(owner_shift_id, -1) = ?
            """,
            (
                ORPHAN,
                item["reason"],
                now,
                run_id,
                item["prev_status"],
                bid,
                item["prev_status"],
                -1 if item["owner_shift_id"] is None else item["owner_shift_id"],
            ),
        )
    else:
        return
    if cur.rowcount != 1:
        raise WriteFailed(f"批次#{bid}（{item['biz_key']}）回写失败，扫描后状态已变化", bid)


def run() -> dict:
    started = _now()
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            plan = _plan(conn)
            items = plan["items"]
            repatriated = sum(1 for i in items if i["action"] == ACTION_REPATRIATE)
            quarantined = sum(1 for i in items if i["action"] == ACTION_QUARANTINE)
            kept = sum(1 for i in items if i["action"] == ACTION_KEEP)
            detail = {
                "escrow_candidates": plan["escrow_candidates"],
                "orphan_candidates": plan["orphan_candidates"],
                "inflight_before": plan["inflight_before"],
                "quarantined_before": plan["quarantined_before"],
                "kept_count": kept,
            }
            cur = conn.execute(
                """
                INSERT INTO repatriate_runs(
                  status, scanned_count, repatriated_count, quarantined_count,
                  kept_count, message, detail_json, started_at, finished_at
                ) VALUES ('已完成', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["scanned_count"],
                    repatriated,
                    quarantined,
                    kept,
                    "",
                    json.dumps(detail, ensure_ascii=False),
                    started,
                    started,
                ),
            )
            run_id = cur.lastrowid
            for item in items:
                _apply(conn, run_id, item, started)
                conn.execute(
                    """
                    INSERT INTO repatriate_items(run_id, batch_id, biz_key, action, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, item["batch_id"], item["biz_key"], item["action"], item["reason"]),
                )

            # 同源自检：报告数字必须与托管集合、在途集合、隔离区一致
            escrow_after = escrow_count(conn)
            inflight_after = inflight_count(conn)
            quarantined_after = orphan_count(conn)
            if escrow_after != kept:
                raise RuntimeError(f"自检失败：托管集合 {escrow_after} ≠ 保留 {kept}")
            if inflight_after != plan["inflight_before"] + repatriated:
                raise RuntimeError(
                    f"自检失败：在途集合 {inflight_after} ≠ "
                    f"{plan['inflight_before']} + {repatriated}"
                )
            if quarantined_after != plan["quarantined_before"] + quarantined:
                raise RuntimeError(
                    f"自检失败：隔离区 {quarantined_after} ≠ "
                    f"{plan['quarantined_before']} + {quarantined}"
                )
            conn.execute(
                "UPDATE repatriate_runs SET message = ? WHERE id = ?",
                (
                    f"扫描 {plan['scanned_count']}，归位 {repatriated}，"
                    f"孤儿隔离 {quarantined}，保留 {kept}",
                    run_id,
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一折算为任务失败（已回滚）
        message = f"归位任务失败，本趟写入已回滚：{exc}"
        _record_failure(started, message, getattr(exc, "batch_id", None))
        raise HTTPException(500, message) from exc

    return get_run(run_id)


def _record_failure(started: str, message: str, batch_id: int | None = None) -> None:
    """失败留痕：归位写入已回滚，单独记一条 0 计数的失败报告供界面查看。"""
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO repatriate_runs(
                  status, scanned_count, repatriated_count, quarantined_count,
                  kept_count, failed_batch_id, message, detail_json, started_at, finished_at
                ) VALUES ('失败', 0, 0, 0, 0, ?, ?, '{}', ?, ?)
                """,
                (batch_id, message, started, _now()),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 - 失败留痕不得掩盖原始错误
        pass


def list_runs() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute("SELECT * FROM repatriate_runs ORDER BY id DESC").fetchall()
        )
    for r in rows:
        r["detail"] = json.loads(r.pop("detail_json"))
    return rows


def get_run(run_id: int) -> dict:
    with connect() as conn:
        r = conn.execute("SELECT * FROM repatriate_runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            raise HTTPException(404, "归位任务报告不存在")
        items = conn.execute(
            """
            SELECT i.*, b.title, b.owner_shift_id, s.name AS owner_shift_name
            FROM repatriate_items i
            LEFT JOIN batches b ON b.id = i.batch_id
            LEFT JOIN shifts s ON s.id = b.owner_shift_id
            WHERE i.run_id = ?
            ORDER BY i.action, i.batch_id
            """,
            (run_id,),
        ).fetchall()
    out = dict(r)
    out["detail"] = json.loads(out.pop("detail_json"))
    out["items"] = rows_to_dicts(items)
    return out


def list_escrow() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name,
                       h.status AS handover_status
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                LEFT JOIN handovers h ON h.id = b.escrow_handover_id
                WHERE b.status = ?
                ORDER BY b.id
                """,
                (ESCROW,),
            ).fetchall()
        )
    return rows


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                WHERE b.status = ?
                ORDER BY b.orphaned_at DESC, b.id
                """,
                (ORPHAN,),
            ).fetchall()
        )
    return rows


def snapshot() -> dict:
    """界面用同源计数：托管集合 / 在途集合 / 孤儿隔离区。"""
    with connect() as conn:
        return {
            "escrow_count": escrow_count(conn),
            "inflight_count": inflight_count(conn),
            "quarantine_count": orphan_count(conn),
            "settled_count": count_by_status(conn, SETTLED),
        }
