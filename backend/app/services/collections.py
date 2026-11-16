"""批次集合的同源定义：托管集合 / 在途集合 / 孤儿隔离区。

所有报告、总览、归位任务都必须经由这里取数，避免同一名词在不同页面口径不一。
"""

from __future__ import annotations

INFLIGHT = "在途"
ESCROW = "托管"
SETTLED = "已结清"
ORPHAN = "孤儿隔离"


def count_by_status(conn, status: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE status = ?", (status,)
    ).fetchone()["c"]


def escrow_count(conn) -> int:
    return count_by_status(conn, ESCROW)


def orphan_count(conn) -> int:
    return count_by_status(conn, ORPHAN)


def inflight_count(conn) -> int:
    """在途集合：状态为在途且归属班次有效；无有效归属的在途批属于无主键。"""
    return conn.execute(
        """
        SELECT COUNT(*) AS c FROM batches b
        JOIN shifts s ON s.id = b.owner_shift_id
        WHERE b.status = ?
        """,
        (INFLIGHT,),
    ).fetchone()["c"]
