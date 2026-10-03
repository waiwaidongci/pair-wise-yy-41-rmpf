from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import (BATCH_FINALIZED, BATCH_VOID, BATCH_WRITING, ConflictError,
                     NOTICE_ACTIVE, NotFoundError)
from .rules import STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        batch_statuses = ",".join("'" + s + "'" for s in
                                  (BATCH_WRITING, BATCH_FINALIZED, BATCH_VOID))
        # 旧库先补列，避免后续索引/脚本依赖缺列而失败
        self._migrate_columns()
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    bound_notice_id INTEGER,
                    bound_notice_version INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS monitor_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    observed_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    threshold REAL NOT NULL DEFAULT 1,
                    chunks_total INTEGER,
                    chunks_received INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '{BATCH_WRITING}'
                        CHECK(status IN ({batch_statuses})),
                    registered_by TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    finalized_at TEXT,
                    voided_at TEXT,
                    void_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES monitor_batches(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    quantity REAL NOT NULL,
                    note TEXT,
                    received_by TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE(batch_id, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS traffic_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_type TEXT NOT NULL CHECK(notice_type IN ('restriction','closure')),
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    status TEXT NOT NULL DEFAULT '{NOTICE_ACTIVE}'
                        CHECK(status IN ('{NOTICE_ACTIVE}','void')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    voided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    basis_time TEXT NOT NULL,
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    threshold REAL NOT NULL,
                    reading_count INTEGER NOT NULL DEFAULT 0,
                    bound_notice_id INTEGER,
                    bound_notice_version INTEGER,
                    batch_ids TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_conclusions_item
                    ON conclusions(item_id, id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    def _migrate_columns(self) -> None:
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone()
        if existing is None:
            return  # 全新库：建表脚本自带新列
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "bound_notice_id" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN bound_notice_id INTEGER")
        if "bound_notice_version" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN bound_notice_version INTEGER")
        self.conn.commit()

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("桥梁告警不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str, bound_notice_id: Optional[int] = None,
                        bound_notice_version: Optional[int] = None,
                        unbind: bool = False) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if unbind:
                cur = self.conn.execute(
                    """UPDATE items SET status=?, version=version+1, updated_at=?,
                           bound_notice_id=NULL, bound_notice_version=NULL
                       WHERE id=? AND version=?""",
                    (target, now, item_id, expected_version),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE items SET status=?, version=version+1, updated_at=?,
                           bound_notice_id=COALESCE(?, bound_notice_id),
                           bound_notice_version=COALESCE(?, bound_notice_version)
                       WHERE id=? AND version=?""",
                    (target, now, bound_notice_id, bound_notice_version,
                     item_id, expected_version),
                )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("桥梁告警不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def apply_recompute(self, item_id: int, status: str, severity: str,
                        quantity: float, bound_notice_id: Optional[int],
                        bound_notice_version: Optional[int]) -> None:
        """重算落账：监测字段更新，失效的通告绑定清除，版本递增。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, severity=?, quantity=?, updated_at=?,
                       version=version+1, bound_notice_id=?, bound_notice_version=?
                   WHERE id=?""",
                (status, severity, quantity, now, bound_notice_id,
                 bound_notice_version, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("桥梁告警不存在")

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---------------- 监测批次 ----------------

    def register_batch(self, batch_no: str, item_id: int, effective_from: str,
                       effective_to: Optional[str], observed_at: str, severity: str,
                       threshold: float, chunks_total: Optional[int],
                       actor: str) -> Dict[str, Any]:
        """首次登记；批次号冲突时返回None，由调用方回读（断线重连仍返回同一份）。"""
        now = utc_now()
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT id FROM monitor_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            if existing is not None:
                return None
            try:
                cur = self.conn.execute(
                    """INSERT INTO monitor_batches(batch_no, item_id, effective_from,
                       effective_to, observed_at, severity, threshold, chunks_total,
                       status, registered_by, registered_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_no, item_id, effective_from, effective_to, observed_at,
                     severity, threshold, chunks_total, BATCH_WRITING, actor, now),
                )
            except sqlite3.IntegrityError:
                return None
            batch_id = int(cur.lastrowid)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM monitor_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("监测批次不存在")
        return dict(row)

    def get_batch_by_no(self, batch_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM monitor_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("监测批次不存在")
        return dict(row)

    def list_batches(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if item_id is not None:
            sql = "SELECT * FROM monitor_batches WHERE item_id=? ORDER BY id"
            params = (item_id,)
        else:
            sql = "SELECT * FROM monitor_batches ORDER BY id"
            params = ()
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def insert_reading(self, batch_id: int, chunk_index: int, quantity: float,
                       note: Optional[str], actor: str) -> Optional[Dict[str, Any]]:
        """幂等追加分片读数；同一分片重发返回None，由调用方回读旧值。"""
        now = utc_now()
        with self._lock, self.conn:
            batch = self.conn.execute(
                "SELECT status, chunks_total FROM monitor_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("监测批次不存在")
            if batch["status"] != BATCH_WRITING:
                raise ConflictError("批次已定稿或已作废，不能追加读数")
            total = batch["chunks_total"]
            if total is not None and not (0 <= chunk_index < total):
                raise ConflictError(f"分片序号必须在0..{total - 1}之间")
            try:
                cur = self.conn.execute(
                    """INSERT INTO batch_readings(batch_id, chunk_index, quantity, note,
                       received_by, received_at) VALUES(?,?,?,?,?,?)""",
                    (batch_id, chunk_index, quantity, note, actor, now),
                )
            except sqlite3.IntegrityError:
                return None
            reading_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE monitor_batches SET chunks_received = "
                "(SELECT COUNT(*) FROM batch_readings WHERE batch_id=?) WHERE id=?",
                (batch_id, batch_id),
            )
            row = self.conn.execute(
                "SELECT * FROM batch_readings WHERE id=?", (reading_id,)
            ).fetchone()
            return dict(row)

    def get_reading(self, batch_id: int, chunk_index: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batch_readings WHERE batch_id=? AND chunk_index=?",
                (batch_id, chunk_index),
            ).fetchone()
        if row is None:
            raise NotFoundError("该分片读数不存在")
        return dict(row)

    def list_readings(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_readings WHERE batch_id=? ORDER BY chunk_index",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_batch_finalized(self, batch_id: int) -> bool:
        """定稿批次：必须处于writing状态且收齐全部声明分片；重复定稿返回False。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"""UPDATE monitor_batches SET status=?, finalized_at=?
                    WHERE id=? AND status='{BATCH_WRITING}'
                      AND (chunks_total IS NULL
                           OR chunks_received >= chunks_total)""",
                (BATCH_FINALIZED, now, batch_id),
            )
            return cur.rowcount > 0

    def void_batch(self, batch_id: int, reason: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE monitor_batches SET status=?, voided_at=?, void_reason=?
                   WHERE id=? AND status!=?""",
                (BATCH_VOID, now, reason, batch_id, BATCH_VOID),
            )
            if cur.rowcount == 0:
                if self.conn.execute(
                    "SELECT 1 FROM monitor_batches WHERE id=?", (batch_id,)
                ).fetchone() is None:
                    raise NotFoundError("监测批次不存在")
                raise ConflictError("批次已经作废")

    def active_batches_for(self, item_id: int, now_ts: str) -> List[Dict[str, Any]]:
        """同一桥梁当前在有效时间窗内、已定稿、且观测时间不晚于当前时刻的批次。"""
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT b.id, b.severity, b.threshold, b.observed_at,
                           (SELECT COALESCE(SUM(r.quantity),0)
                              FROM batch_readings r WHERE r.batch_id=b.id) AS quantity,
                           (SELECT COUNT(*) FROM batch_readings r WHERE r.batch_id=b.id)
                               AS reading_count
                      FROM monitor_batches b
                     WHERE b.item_id=? AND b.status='{BATCH_FINALIZED}'
                       AND b.effective_from<=?
                       AND (b.effective_to IS NULL OR b.effective_to>?)
                       AND b.observed_at<=?
                     ORDER BY b.observed_at, b.id""",
                (item_id, now_ts, now_ts, now_ts),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------- 交通通告 ----------------

    def create_notice(self, notice_type: str, title: str, detail: str,
                      effective_from: str, effective_to: Optional[str],
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO traffic_notices(notice_type, title, detail, effective_from,
                   effective_to, status, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,1,?,?,?)""",
                (notice_type, title, detail, effective_from, effective_to,
                 NOTICE_ACTIVE, actor, now, now),
            )
            notice_id = int(cur.lastrowid)
        return self.get_notice(notice_id)

    def get_notice(self, notice_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM traffic_notices WHERE id=?", (notice_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("交通通告不存在")
        return dict(row)

    def list_notices(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM traffic_notices"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_notice(self, notice_id: int, title: str, detail: str,
                      effective_from: str, effective_to: Optional[str]) -> Dict[str, Any]:
        """通告修改：版本递增，绑定该通告（任意旧版本）的限行/封闭告警都要重算。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE traffic_notices SET title=?, detail=?, effective_from=?,
                       effective_to=?, version=version+1, updated_at=?
                   WHERE id=? AND status=?""",
                (title, detail, effective_from, effective_to, now,
                 notice_id, NOTICE_ACTIVE),
            )
            if cur.rowcount == 0:
                if self.conn.execute(
                    "SELECT 1 FROM traffic_notices WHERE id=?", (notice_id,)
                ).fetchone() is None:
                    raise NotFoundError("交通通告不存在")
                raise ConflictError("通告已作废，不能修改")
            bound = self.conn.execute(
                """SELECT id FROM items
                   WHERE bound_notice_id=? AND status IN ('restricted','closed')""",
                (notice_id,),
            ).fetchall()
        return {"notice": self.get_notice(notice_id),
                "bound_item_ids": [int(r["id"]) for r in bound]}

    def void_notice(self, notice_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE traffic_notices SET status='void', voided_at=?, updated_at=?
                   WHERE id=? AND status=?""",
                (now, now, notice_id, NOTICE_ACTIVE),
            )
            if cur.rowcount == 0:
                if self.conn.execute(
                    "SELECT 1 FROM traffic_notices WHERE id=?", (notice_id,)
                ).fetchone() is None:
                    raise NotFoundError("交通通告不存在")
                raise ConflictError("通告已经作废")
            bound = self.conn.execute(
                """SELECT id FROM items
                   WHERE bound_notice_id=? AND status IN ('restricted','closed')""",
                (notice_id,),
            ).fetchall()
        return {"notice": self.get_notice(notice_id),
                "bound_item_ids": [int(r["id"]) for r in bound]}

    # ---------------- 结论 ----------------

    def insert_conclusion(self, item_id: int, basis_time: str, status: str,
                          severity: str, quantity: float, threshold: float,
                          reading_count: int, bound_notice_id: Optional[int],
                          bound_notice_version: Optional[int], batch_ids: List[int],
                          reason: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO conclusions(item_id, basis_time, status, severity, quantity,
                   threshold, reading_count, bound_notice_id, bound_notice_version,
                   batch_ids, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, basis_time, status, severity, quantity, threshold,
                 reading_count, bound_notice_id, bound_notice_version,
                 json.dumps(batch_ids), reason, now),
            )
            conclusion_id = int(cur.lastrowid)
        return self.get_conclusion(conclusion_id)

    def get_conclusion(self, conclusion_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM conclusions WHERE id=?", (conclusion_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("结论不存在")
        result = dict(row)
        result["batch_ids"] = json.loads(result["batch_ids"])
        return result

    def latest_conclusion(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM conclusions WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["batch_ids"] = json.loads(result["batch_ids"])
        return result

    def list_conclusions(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM conclusions WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["batch_ids"] = json.loads(item["batch_ids"])
            result.append(item)
        return result

    # ---------------- 审计 ----------------

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
