from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  keeper TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '空闲'
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  biz_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  owner_shift_id INTEGER REFERENCES shifts(id),
  status TEXT NOT NULL DEFAULT '在途',
  note TEXT NOT NULL DEFAULT '',
  escrow_handover_id INTEGER,
  escrow_at TEXT,
  orphan_reason TEXT NOT NULL DEFAULT '',
  orphan_prev_status TEXT,
  orphaned_at TEXT,
  orphan_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS handovers (
  id INTEGER PRIMARY KEY,
  from_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  to_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  status TEXT NOT NULL,
  batch_count INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS handover_items (
  handover_id INTEGER NOT NULL REFERENCES handovers(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  PRIMARY KEY (handover_id, batch_id)
);
CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY,
  handover_id INTEGER REFERENCES handovers(id),
  before_count INTEGER NOT NULL,
  after_from_count INTEGER NOT NULL,
  after_to_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repatriate_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  scanned_count INTEGER NOT NULL DEFAULT 0,
  repatriated_count INTEGER NOT NULL DEFAULT 0,
  quarantined_count INTEGER NOT NULL DEFAULT 0,
  kept_count INTEGER NOT NULL DEFAULT 0,
  failed_batch_id INTEGER,
  message TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repatriate_items (
  run_id INTEGER NOT NULL REFERENCES repatriate_runs(id),
  batch_id INTEGER REFERENCES batches(id),
  biz_key TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, batch_id)
);
"""

# 旧库补列：托管引用、托管时间、孤儿隔离原因与留痕批次
BATCH_COLUMN_MIGRATIONS = [
    ("escrow_handover_id", "INTEGER"),
    ("escrow_at", "TEXT"),
    ("orphan_reason", "TEXT NOT NULL DEFAULT ''"),
    ("orphan_prev_status", "TEXT"),
    ("orphaned_at", "TEXT"),
    ("orphan_run_id", "INTEGER"),
]


def _migrate(conn) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)").fetchall()}
    for name, ddl in BATCH_COLUMN_MIGRATIONS:
        if name not in cols:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {name} {ddl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM shifts").fetchone()["c"]
        if n:
            conn.commit()
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for s in data["shifts"]:
            conn.execute(
                "INSERT INTO shifts(id, name, keeper, status) VALUES (?, ?, ?, ?)",
                (s["id"], s["name"], s["keeper"], s["status"]),
            )
        for b in data["batches"]:
            conn.execute(
                """
                INSERT INTO batches(
                  id, biz_key, title, owner_shift_id, status, note, escrow_handover_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    b["id"],
                    b["biz_key"],
                    b["title"],
                    b["owner_shift_id"],
                    b["status"],
                    b.get("note", ""),
                    b.get("escrow_handover_id"),
                ),
            )
        conn.commit()
