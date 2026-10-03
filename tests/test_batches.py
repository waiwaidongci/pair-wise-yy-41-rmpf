import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class BatchConclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit(self, batch_no, bridge_id, valid_from, valid_to,
                severity="warning", quantity=12, threshold=6, actor="sensor"):
        return self.service.submit_batch({
            "batch_no": batch_no, "bridge_id": bridge_id,
            "valid_from": valid_from, "valid_to": valid_to,
            "payload": {"severity": severity, "quantity": quantity, "threshold": threshold},
        }, actor, "sensor_operator")

    def _create_notice(self, notice_no, bridge_id="BR-1"):
        return self.service.create_notice({
            "notice_no": notice_no, "bridge_id": bridge_id,
            "title": "traffic notice", "content": "content",
            "effective_from": "2026-01-01T00:00:00Z",
        }, "authority", "traffic_authority")

    def test_idempotent_submission_first_registration_wins(self):
        b1 = self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        b2 = self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
                          actor="other_operator")
        self.assertEqual(b1["id"], b2["id"])
        self.assertEqual(b1["created_at"], b2["created_at"])
        batches = self.service.list_batches("BR-1", "viewer")
        self.assertEqual(len(batches), 1)

    def test_out_of_order_reconciliation_by_time_window(self):
        self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        self._submit("B-2", "BR-1", "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z")
        c2 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c2["conclusion"]["batch_no"], "B-2")
        self.assertEqual(c2["version"], 2)
        self._submit("B-3", "BR-1", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z")
        c3 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c3["conclusion"]["batch_no"], "B-2")
        self.assertEqual(c3["version"], 2)

    def test_old_batch_does_not_overwrite_newer_recalculation(self):
        self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        self._create_notice("NT-1")
        c1 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c1["conclusion"]["batch_no"], "B-1")
        self.assertEqual(c1["conclusion"]["notice_no"], "NT-1")
        self.assertEqual(c1["version"], 2)
        self.service.modify_notice("NT-1", {
            "title": "updated notice", "content": "updated content",
            "effective_from": "2026-01-01T00:00:00Z",
        }, "authority", "traffic_authority")
        c2 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c2["version"], 3)
        self.assertEqual(c2["conclusion"]["batch_no"], "B-1")
        self._submit("B-2", "BR-1", "2025-12-01T00:00:00Z", "2025-12-02T00:00:00Z")
        c3 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c3["version"], 3)
        self.assertEqual(c3["conclusion"]["batch_no"], "B-1")

    def test_notice_binding_guard_before_restricted(self):
        item = self.service.create_item({
            "title": "guard item", "description": "desc", "severity": "warning",
            "quantity": 12, "threshold": 6,
        }, "creator", "sensor_operator")
        with self.assertRaises(ConflictError):
            self.service.transition(item["id"], "restricted", item["version"],
                                    "reviewer", "bridge_engineer")
        self._create_notice("NT-1")
        self.service.bind_notice(item["id"], "NT-1", "engineer", "bridge_engineer")
        current = item
        for target in ["warning", "restricted"]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "restricted")

    def test_notice_modification_triggers_recalculation(self):
        self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        self._create_notice("NT-1")
        c1 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c1["version"], 2)
        self.service.modify_notice("NT-1", {
            "title": "modified", "content": "modified content",
            "effective_from": "2026-01-01T00:00:00Z",
        }, "authority", "traffic_authority")
        c2 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c2["version"], 3)
        self.assertEqual(c2["conclusion"]["notice_no"], "NT-1")

    def test_batch_void_triggers_recalculation(self):
        self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        self._submit("B-2", "BR-1", "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z")
        c1 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c1["conclusion"]["batch_no"], "B-2")
        self.service.void_batch("B-2", "engineer", "bridge_engineer")
        c2 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c2["conclusion"]["batch_no"], "B-1")

    def test_write_checkpoint_resume_by_batch_number(self):
        self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        cp1 = self.service.get_write_checkpoint("BR-1", "viewer")
        self.assertEqual(cp1["last_batch_no"], "B-1")
        self._submit("B-2", "BR-1", "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z")
        cp2 = self.service.get_write_checkpoint("BR-1", "viewer")
        self.assertEqual(cp2["last_batch_no"], "B-2")
        self._submit("B-3", "BR-1", "2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z")
        cp3 = self.service.get_write_checkpoint("BR-1", "viewer")
        self.assertEqual(cp3["last_batch_no"], "B-3")

    def test_conclusion_status_derived_from_severity(self):
        self._submit("B-1", "BR-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
                     severity="critical", quantity=30, threshold=10)
        c = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c["conclusion"]["status"], "closed")
        self._submit("B-2", "BR-1", "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z",
                     severity="normal", quantity=1, threshold=10)
        c2 = self.service.get_bridge_conclusion("BR-1", "viewer")
        self.assertEqual(c2["conclusion"]["status"], "normal")


if __name__ == "__main__":
    unittest.main()
