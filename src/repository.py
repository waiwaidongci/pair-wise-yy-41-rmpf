from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


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
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    bridge_id TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    voided_by TEXT,
                    voided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_batches_bridge
                    ON batches(bridge_id, status, valid_from DESC);
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_no TEXT NOT NULL UNIQUE,
                    bridge_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','modified','void')),
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notice_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
                    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','released')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bridge_conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    batch_id INTEGER REFERENCES batches(id),
                    notice_id INTEGER REFERENCES notices(id),
                    conclusion TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(bridge_id, version)
                );
                CREATE TABLE IF NOT EXISTS write_checkpoints (
                    bridge_id TEXT PRIMARY KEY,
                    last_batch_no TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

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
            raise NotFoundError("项目不存在")
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
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

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

    # ---- batches ----
    def create_batch(self, batch_no: str, bridge_id: str, valid_from: str,
                     valid_to: str, payload: Dict[str, Any], actor: str) -> tuple:
        now = utc_now()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """INSERT INTO batches(batch_no, bridge_id, valid_from, valid_to, status,
                       payload, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (batch_no, bridge_id, valid_from, valid_to, "active", payload_json, actor, now),
                )
            return self.get_batch(batch_no), True
        except sqlite3.IntegrityError:
            return self.get_batch(batch_no), False

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def void_batch(self, batch_no: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE batches SET status='void', voided_by=?, voided_at=? WHERE batch_no=? AND status='active'",
                (actor, now, batch_no),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT status FROM batches WHERE batch_no=?", (batch_no,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("批次不存在")
                raise ConflictError("批次已作废")
        return self.get_batch(batch_no)

    def list_batches(self, bridge_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM batches WHERE bridge_id=?"
        params: tuple = (bridge_id,)
        if status:
            sql += " AND status=?"
            params += (status,)
        sql += " ORDER BY valid_from DESC, valid_to DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def get_latest_active_batch(self, bridge_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM batches WHERE bridge_id=? AND status='active'
                   ORDER BY valid_from DESC, valid_to DESC LIMIT 1""",
                (bridge_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def get_checkpoint(self, bridge_id: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT last_batch_no FROM write_checkpoints WHERE bridge_id=?",
                (bridge_id,),
            ).fetchone()
        return row["last_batch_no"] if row else None

    def update_checkpoint(self, bridge_id: str, batch_no: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO write_checkpoints(bridge_id, last_batch_no, updated_at)
                   VALUES(?,?,?) ON CONFLICT(bridge_id)
                   DO UPDATE SET last_batch_no=?, updated_at=?""",
                (bridge_id, batch_no, now, batch_no, now),
            )

    # ---- notices ----
    def create_notice(self, notice_no: str, bridge_id: str, title: str, content: str,
                      effective_from: str, effective_to: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """INSERT INTO notices(notice_no, bridge_id, title, content, status,
                       effective_from, effective_to, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (notice_no, bridge_id, title, content, "active", effective_from,
                     effective_to, actor, now, now),
                )
        except sqlite3.IntegrityError:
            raise ConflictError("通告编号已存在")
        return self.get_notice(notice_no)

    def get_notice(self, notice_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM notices WHERE notice_no=?", (notice_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("通告不存在")
        return dict(row)

    def modify_notice(self, notice_no: str, title: str, content: str,
                      effective_from: str, effective_to: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE notices SET title=?, content=?, effective_from=?, effective_to=?,
                   status='modified', updated_at=? WHERE notice_no=? AND status!='void'""",
                (title, content, effective_from, effective_to, now, notice_no),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT status FROM notices WHERE notice_no=?", (notice_no,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("通告不存在")
                raise ConflictError("通告已作废，不能修改")
        return self.get_notice(notice_no)

    def void_notice(self, notice_no: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE notices SET status='void', updated_at=? WHERE notice_no=? AND status!='void'",
                (now, notice_no),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT status FROM notices WHERE notice_no=?", (notice_no,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("通告不存在")
                raise ConflictError("通告已作废")
            self.conn.execute(
                """UPDATE notice_bindings SET status='released'
                   WHERE notice_id=(SELECT id FROM notices WHERE notice_no=?)""",
                (notice_no,),
            )
        return self.get_notice(notice_no)

    def list_notices(self, bridge_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM notices WHERE bridge_id=?"
        params: tuple = (bridge_id,)
        if status:
            sql += " AND status=?"
            params += (status,)
        sql += " ORDER BY effective_from DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_active_notice(self, bridge_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM notices WHERE bridge_id=? AND status IN ('active','modified')
                   ORDER BY effective_from DESC LIMIT 1""",
                (bridge_id,),
            ).fetchone()
        return dict(row) if row else None

    # ---- notice bindings ----
    def bind_notice(self, item_id: int, notice_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO notice_bindings(item_id, notice_id, status, created_by, created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET notice_id=?, status='active',
                   created_by=?, created_at=?""",
                (item_id, notice_id, "active", actor, now, notice_id, actor, now),
            )
        binding = self.get_active_binding(item_id)
        if binding is None:
            raise NotFoundError("绑定不存在")
        return binding

    def get_active_binding(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT nb.*, n.notice_no, n.status AS notice_status
                   FROM notice_bindings nb JOIN notices n ON nb.notice_id = n.id
                   WHERE nb.item_id=? AND nb.status='active'""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    # ---- conclusions ----
    def next_conclusion_version(self, bridge_id: str) -> int:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM bridge_conclusions WHERE bridge_id=?",
                (bridge_id,),
            ).fetchone()
        return int(row["next"])

    def store_conclusion(self, bridge_id: str, version: int, batch_id: Optional[int],
                         notice_id: Optional[int], conclusion: Dict[str, Any], actor: str) -> None:
        now = utc_now()
        conclusion_json = json.dumps(conclusion, ensure_ascii=False, sort_keys=True)
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO bridge_conclusions(bridge_id, version, batch_id, notice_id,
                   conclusion, created_at) VALUES(?,?,?,?,?,?)""",
                (bridge_id, version, batch_id, notice_id, conclusion_json, now),
            )

    def get_current_conclusion(self, bridge_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM bridge_conclusions WHERE bridge_id=?
                   ORDER BY version DESC LIMIT 1""",
                (bridge_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["conclusion"] = json.loads(result["conclusion"])
        return result
