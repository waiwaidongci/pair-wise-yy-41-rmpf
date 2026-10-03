import sqlite3
import tempfile
import unittest
from pathlib import Path


LEGACY_SCHEMA = """
CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,
  description TEXT NOT NULL,severity TEXT NOT NULL,quantity REAL NOT NULL DEFAULT 0,
  threshold REAL NOT NULL DEFAULT 1,status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
  external_ref TEXT,created_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX ux_items_external_ref ON items(external_ref) WHERE external_ref IS NOT NULL;
CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id INTEGER NOT NULL,
  kind TEXT NOT NULL,detail TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',
  external_ref TEXT,created_by TEXT NOT NULL,created_at TEXT NOT NULL,
  UNIQUE(item_id, external_ref));
CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,
  entity_type TEXT NOT NULL,entity_id INTEGER NOT NULL,actor TEXT NOT NULL,
  detail TEXT NOT NULL,previous_hash TEXT NOT NULL,entry_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL);
INSERT INTO items(title,description,severity,quantity,threshold,status,version,
  created_by,created_at,updated_at)
  VALUES('旧桥','历史数据','warning',5,10,'normal',1,'op','2026-01-01','2026-01-01');
"""


class MigrationTest(unittest.TestCase):
    def test_legacy_schema_migrates_and_runs_new_workflow(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(LEGACY_SCHEMA)
        conn.commit(); conn.close()

        from src.repository import Repository
        from src.service import Service
        repo = Repository(db_path)
        self.addCleanup(repo.close)
        service = Service(repo)

        item = service.get_item(1, "viewer")
        self.assertIsNone(item["bound_notice_id"])
        self.assertEqual(item["title"], "旧桥")
        tables = {row[0] for row in
                  repo.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"monitor_batches", "batch_readings",
                         "traffic_notices", "conclusions"} <= tables)

        service.register_batch(1, {
            "batch_no": "MIG-1", "effective_from": "2026-10-01T00:00:00Z",
            "observed_at": "2026-10-03T10:00:00Z", "severity": "critical",
            "chunks_total": 1,
        }, "op", "sensor_operator")
        service.put_reading(1, "MIG-1", {"chunk_index": 0, "quantity": 99},
                            "op", "sensor_operator")
        service.finalize_batch(1, "MIG-1", {}, "op", "sensor_operator")
        live = service.get_live_item(1, "viewer")
        self.assertEqual(live["severity"], "critical")
        self.assertEqual(live["status"], "warning")
        self.assertTrue(repo.verify_audit_chain())

        # 迁移必须可重复执行（服务重启再次打开库）
        repo.close()
        repo2 = Repository(db_path)
        self.addCleanup(repo2.close)
        self.assertTrue(repo2.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
