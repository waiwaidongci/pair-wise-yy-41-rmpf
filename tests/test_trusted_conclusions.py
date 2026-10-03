import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import ConflictError, ValidationError
from src.repository import Repository
from src.service import Service


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def set(self, dt):
        self.dt = dt


class TrustedConclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
        self.clock = Clock(self.base)
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo, clock=self.clock)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _bridge(self, ref="B-1"):
        return self.service.create_item({
            "title": "跨河桥", "description": "主梁挠度监测",
            "severity": "normal", "quantity": 0, "threshold": 10,
            "external_ref": ref,
        }, "op1", "sensor_operator")

    def _register(self, item, batch_no, start, end, observed, severity,
                  chunks_total=None, actor="op1", role="sensor_operator"):
        return self.service.register_batch(item["id"], {
            "batch_no": batch_no,
            "effective_from": iso(start), "effective_to": iso(end),
            "observed_at": iso(observed), "severity": severity,
            "chunks_total": chunks_total,
        }, actor, role)

    def _readings(self, item, batch_no, values):
        for idx, value in enumerate(values):
            self.service.put_reading(item["id"], batch_no,
                                     {"chunk_index": idx, "quantity": value},
                                     "op1", "sensor_operator")

    def _finalize(self, item, batch_no):
        return self.service.finalize_batch(
            item["id"], batch_no, {}, "op1", "sensor_operator")

    def _notice(self, ntype="restriction", start=None, end=None):
        return self.service.create_notice({
            "notice_type": ntype, "title": "通告", "detail": "措施",
            "effective_from": iso(start or (self.base - timedelta(days=1))),
            "effective_to": iso(end) if end else None,
        }, "ta1", "traffic_authority")

    # 1. 两名操作员同时提交同一批次：保留首次登记，重试仍返回那份结果
    def test_concurrent_register_same_batch_keeps_first(self):
        item = self._bridge()
        results = []

        def register(actor):
            results.append(self.service.register_batch(item["id"], {
                "batch_no": "BATCH-DUP",
                "effective_from": iso(self.base - timedelta(hours=1)),
                "effective_to": iso(self.base + timedelta(hours=1)),
                "severity": "warning",
            }, actor, "sensor_operator"))

        t1 = threading.Thread(target=register, args=("op-a",))
        t2 = threading.Thread(target=register, args=("op-b",))
        t1.start(); t2.start(); t1.join(); t2.join()
        ids = {r["id"] for r in results}
        self.assertEqual(len(ids), 1, "同一批次号只能有一份登记")
        actors = {r["registered_by"] for r in results}
        self.assertEqual(len(actors), 1, "保留的是首次登记的操作员")
        replay_flags = {bool(r["replayed"]) for r in results}
        self.assertEqual(replay_flags, {True, False})
        # 断线重连再送一次：仍是同一份
        again = self.service.register_batch(item["id"], {
            "batch_no": "BATCH-DUP",
            "effective_from": iso(self.base - timedelta(hours=1)),
            "effective_to": iso(self.base + timedelta(hours=1)),
            "severity": "warning",
        }, "op-a", "sensor_operator")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["id"], next(iter(ids)))

    # 2. 乱序到达：旧批次不能盖掉较新的重算结果
    def test_out_of_order_batches_stale_basis_never_overwrites(self):
        item = self._bridge()
        # 较新批次（观测时间11:00，严重warning）先到
        self._register(item, "B-NEW",
                       self.base - timedelta(hours=3), self.base + timedelta(hours=3),
                       self.base - timedelta(hours=1), "warning")
        self._readings(item, "B-NEW", [12.0])
        self._finalize(item, "B-NEW")
        live = self.service.get_live_item(item["id"], "viewer")
        self.assertEqual(live["status"], "warning")
        self.assertEqual(live["severity"], "warning")

        # 旧批次晚到（观测时间08:00，normal），它的basis_time更旧
        self._register(item, "B-OLD",
                       self.base - timedelta(hours=6), self.base + timedelta(hours=6),
                       self.base - timedelta(hours=4), "normal")
        self._readings(item, "B-OLD", [1.0])
        self._finalize(item, "B-OLD")
        item2 = self.service.get_item(item["id"], "viewer")
        self.assertEqual(item2["status"], "warning", "旧批次不得把较新结论降级")
        conclusions = self.service.list_conclusions(item["id"], "viewer")
        latest = conclusions[-1]
        self.assertEqual(latest["status"], "warning")
        self.assertEqual(latest["basis_time"], iso(self.base - timedelta(hours=1)))
        # 旧批次触发的重算被跳过，审计可追溯
        audit = self.repo.list_audit(item["id"])
        self.assertTrue(any(e["action"] == "recompute_skip" for e in audit))

    # 3. 在有效时间窗内合算：多批次重叠取最高等级
    def test_windowed_aggregation_takes_highest_severity(self):
        item = self._bridge()
        # 先到normal（10:00），再到watch（11:00，窗口重叠）
        self._register(item, "B-LOW",
                       self.base - timedelta(hours=4), self.base + timedelta(hours=4),
                       self.base - timedelta(hours=2), "normal")
        self._readings(item, "B-LOW", [1.0])
        self._finalize(item, "B-LOW")
        self.assertEqual(self.service.get_item(item["id"], "viewer")["status"], "normal")

        self._register(item, "B-WATCH",
                       self.base - timedelta(hours=4), self.base + timedelta(hours=4),
                       self.base - timedelta(hours=1), "watch")
        self._readings(item, "B-WATCH", [2.0])
        self._finalize(item, "B-WATCH")
        # watch的warranted仍是normal，但后续critical批次必须把告警拉起
        self._register(item, "B-CRIT",
                       self.base - timedelta(hours=4), self.base + timedelta(hours=4),
                       self.base, "critical")
        self._readings(item, "B-CRIT", [99.0])
        self._finalize(item, "B-CRIT")
        live = self.service.get_live_item(item["id"], "viewer")
        self.assertEqual(live["status"], "warning")
        self.assertEqual(live["severity"], "critical")
        latest = self.service.list_conclusions(item["id"], "viewer")[-1]
        self.assertEqual(len(set(latest["batch_ids"])), 3)
        self.assertEqual(latest["basis_time"], iso(self.base))
        self.assertTrue(live["closure_escalation"])

    # 4. 已过窗的批次不参与合算
    def test_expired_window_batch_is_excluded(self):
        item = self._bridge()
        self._register(item, "B-PAST",
                       self.base - timedelta(hours=10), self.base - timedelta(hours=8),
                       self.base - timedelta(hours=9), "critical")
        self._readings(item, "B-PAST", [99.0])
        self._finalize(item, "B-PAST")
        live = self.service.get_live_item(item["id"], "viewer")
        self.assertEqual(live["status"], "normal")
        self.assertEqual(live["severity"], "normal")

    # 4b. 同一观测时刻的迟到批次若抬高窗内最高等级，仍要重算落账
    def test_same_basis_late_batch_raising_severity_recomputes(self):
        item = self._bridge()
        self._register(item, "B-T1",
                       self.base - timedelta(hours=3), self.base + timedelta(hours=3),
                       self.base, "normal")
        self._readings(item, "B-T1", [1.0])
        self._finalize(item, "B-T1")
        first = self.service.list_conclusions(item["id"], "viewer")[-1]
        self.assertEqual(first["severity"], "normal")
        # 同刻、同窗，晚到的critical批次
        self._register(item, "B-T2",
                       self.base - timedelta(hours=3), self.base + timedelta(hours=3),
                       self.base, "critical")
        self._readings(item, "B-T2", [88.0])
        self._finalize(item, "B-T2")
        after = self.service.get_item(item["id"], "viewer")
        self.assertEqual(after["severity"], "critical")
        self.assertEqual(after["status"], "warning")
        latest = self.service.list_conclusions(item["id"], "viewer")[-1]
        self.assertEqual(latest["basis_time"], first["basis_time"])
        self.assertEqual(set(latest["batch_ids"]), {1, 2})

    # 5. 进入限行必须绑定有效且类型匹配的通告
    def test_restricted_transition_binds_notice(self):
        item = self._bridge()
        # 先有warning监测
        self._register(item, "B-W",
                       self.base - timedelta(hours=2), self.base + timedelta(hours=2),
                       self.base, "warning")
        self._readings(item, "B-W", [11.0])
        self._finalize(item, "B-W")
        cur = self.service.get_item(item["id"], "viewer")
        self.assertEqual(cur["status"], "warning")
        with self.assertRaises(ValidationError):
            self.service.transition(cur["id"], "restricted", cur["version"],
                                    "eng", "bridge_engineer", {})
        notice = self._notice("restriction")
        cur = self.service.transition(cur["id"], "restricted", cur["version"],
                                      "eng", "bridge_engineer",
                                      {"notice_id": notice["id"]})
        self.assertEqual(cur["status"], "restricted")
        self.assertEqual(cur["bound_notice_id"], notice["id"])
        self.assertEqual(cur["bound_notice_version"], 1)
        latest = self.service.list_conclusions(item["id"], "viewer")[-1]
        self.assertEqual(latest["bound_notice_id"], notice["id"])

    # 6. 通告修改后立即失效重算并降级
    def test_notice_update_invalidates_and_recomputes(self):
        item = self._bridge()
        notice = self._notice("restriction")
        # 先有在窗warning批次支撑告警
        self._register(item, "B-N6",
                       self.base - timedelta(hours=2), self.base + timedelta(hours=2),
                       self.base, "warning")
        self._readings(item, "B-N6", [11.0])
        self._finalize(item, "B-N6")
        cur = self.service.get_item(item["id"], "viewer")
        self.assertEqual(cur["status"], "warning")
        cur = self.service.transition(cur["id"], "restricted", cur["version"],
                                      "eng", "bridge_engineer",
                                      {"notice_id": notice["id"]})
        self.assertEqual(cur["status"], "restricted")
        # 通告改版：版本+1，旧绑定立即失效；监测只支撑warning -> 自动降级
        updated = self.service.update_notice(notice["id"], {
            "title": "通告-修订", "detail": "调整限载",
        }, "ta1", "traffic_authority")
        self.assertEqual(updated["version"], 2)
        after = self.service.get_item(item["id"], "viewer")
        self.assertEqual(after["status"], "warning")
        self.assertIsNone(after["bound_notice_id"])
        latest = self.service.list_conclusions(item["id"], "viewer")[-1]
        self.assertIsNone(latest["bound_notice_id"])
        self.assertIn("通告绑定失效", latest["reason"])

    # 7. 通告时间窗到期：值班台读取时旧限行结论不再生效
    def test_notice_window_expiry_recomputed_on_live_read(self):
        item = self._bridge()
        notice = self._notice(
            "restriction",
            start=self.base - timedelta(days=2),
            end=self.base + timedelta(hours=1))
        self._register(item, "B-N7",
                       self.base - timedelta(hours=3), self.base + timedelta(hours=5),
                       self.base, "warning")
        self._readings(item, "B-N7", [11.0])
        self._finalize(item, "B-N7")
        cur = self.service.get_item(item["id"], "viewer")
        self.assertEqual(cur["status"], "warning")
        cur = self.service.transition(cur["id"], "restricted", cur["version"],
                                      "eng", "bridge_engineer",
                                      {"notice_id": notice["id"]})
        self.assertEqual(cur["status"], "restricted")
        # 时间推进到通告失效之后
        self.clock.set(self.base + timedelta(hours=2))
        live = self.service.get_live_item(item["id"], "viewer")
        self.assertEqual(live["status"], "warning")
        self.assertIsNone(live["bound_notice_id"])

    # 8. 批次作废后立即重算，即使新结论基准时间更旧
    def test_void_batch_forces_recompute_on_older_basis(self):
        item = self._bridge()
        # 新批次critical（11:00）
        self._register(item, "B-CRIT",
                       self.base - timedelta(hours=3), self.base + timedelta(hours=3),
                       self.base - timedelta(hours=1), "critical")
        self._readings(item, "B-CRIT", [50.0])
        self._finalize(item, "B-CRIT")
        self.assertEqual(self.service.get_item(item["id"], "viewer")["status"], "warning")
        # 旧批次normal（08:00）
        self._register(item, "B-OLD2",
                       self.base - timedelta(hours=6), self.base + timedelta(hours=6),
                       self.base - timedelta(hours=4), "normal")
        self._readings(item, "B-OLD2", [1.0])
        self._finalize(item, "B-OLD2")
        self.assertEqual(self.service.get_item(item["id"], "viewer")["status"], "warning")
        # 作废critical批次：强制重算，旧基准也允许落账，降级normal
        self.service.void_batch(item["id"], "B-CRIT",
                                {"reason": "误报数据作废"}, "eng", "bridge_engineer")
        after = self.service.get_item(item["id"], "viewer")
        self.assertEqual(after["status"], "normal")
        self.assertEqual(after["severity"], "normal")
        latest = self.service.list_conclusions(item["id"], "viewer")[-1]
        self.assertEqual(latest["basis_time"], iso(self.base - timedelta(hours=4)))

    # 9. 写入中断按批次号续写：缺分片不能定稿，补齐后成功，重发幂等
    def test_resume_write_by_batch_no_is_idempotent(self):
        item = self._bridge()
        reg = self._register(item, "B-RESUME",
                             self.base - timedelta(hours=1),
                             self.base + timedelta(hours=1),
                             self.base, "warning", chunks_total=3)
        self.assertEqual(reg["chunks_total"], 3)
        r0 = self.service.put_reading(item["id"], "B-RESUME",
                                      {"chunk_index": 0, "quantity": 1.0},
                                      "op1", "sensor_operator")
        self.service.put_reading(item["id"], "B-RESUME",
                                 {"chunk_index": 1, "quantity": 2.0},
                                 "op1", "sensor_operator")
        self.assertFalse(r0["replayed"])
        # 模拟中断：分片1重发，返回首次写入那份
        r1_retry = self.service.put_reading(item["id"], "B-RESUME",
                                            {"chunk_index": 1, "quantity": 9.9},
                                            "op1", "sensor_operator")
        self.assertTrue(r1_retry["replayed"])
        self.assertEqual(r1_retry["quantity"], 2.0)
        progress = r1_retry["batch"]
        self.assertEqual((progress["chunks_received"], progress["chunks_total"]), (2, 3))
        # 未收齐不能定稿
        with self.assertRaises(ConflictError):
            self._finalize(item, "B-RESUME")
        # 按批次号续写最后一片
        r2 = self.service.put_reading(item["id"], "B-RESUME",
                                      {"chunk_index": 2, "quantity": 3.0},
                                      "op1", "sensor_operator")
        self.assertFalse(r2["replayed"])
        result = self._finalize(item, "B-RESUME")
        self.assertEqual(result["status"], "finalized")
        readings = result["readings"]
        self.assertEqual([r["chunk_index"] for r in readings], [0, 1, 2])
        # 定稿重试：幂等返回同一份结论
        again = self.service.finalize_batch(
            item["id"], "B-RESUME", {}, "op1", "sensor_operator")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["id"], result["id"])
        self.assertEqual(len(again["readings"]), 3)

    # 10. 已定稿批次不能再追加读数
    def test_cannot_append_reading_after_finalize(self):
        item = self._bridge()
        self._register(item, "B-DONE",
                       self.base - timedelta(hours=1), self.base + timedelta(hours=1),
                       self.base, "normal")
        self._readings(item, "B-DONE", [1.0])
        self._finalize(item, "B-DONE")
        with self.assertRaises(ConflictError):
            self.service.put_reading(item["id"], "B-DONE",
                                     {"chunk_index": 5, "quantity": 1.0},
                                     "op1", "sensor_operator")

    # 11. 非授权角色不能登记批次或发布通告
    def test_role_guards(self):
        item = self._bridge()
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self._register(item, "B-X", self.base, self.base + timedelta(hours=1),
                           self.base, "normal", actor="viewer-x", role="viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_notice({
                "notice_type": "restriction", "title": "x", "detail": "y",
            }, "op1", "sensor_operator")

    # 12. 审计链在所有重算后仍然完整
    def test_audit_chain_intact(self):
        item = self._bridge()
        notice = self._notice("restriction")
        self._register(item, "B-A",
                       self.base - timedelta(hours=2), self.base + timedelta(hours=2),
                       self.base, "warning")
        self._readings(item, "B-A", [11.0])
        self._finalize(item, "B-A")
        cur = self.service.get_item(item["id"], "viewer")
        cur = self.service.transition(cur["id"], "restricted", cur["version"],
                                      "eng", "bridge_engineer",
                                      {"notice_id": notice["id"]})
        self.service.update_notice(notice["id"], {"detail": "v2"},
                                   "ta1", "traffic_authority")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
