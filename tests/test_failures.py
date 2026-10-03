import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"failure item","description":"failure scenarios","severity":'warning',"quantity":5,"threshold":10,"external_ref":"FAIL-1"},"creator",'sensor_operator')
        self.restriction=self.service.create_notice({"notice_type":"restriction","title":"限行","detail":"限载","effective_from":"2000-01-01T00:00:00Z"},"ta",'traffic_authority')
        self.closure=self.service.create_notice({"notice_type":"closure","title":"封闭","detail":"封闭","effective_from":"2000-01-01T00:00:00Z"},"ta",'traffic_authority')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0],{"notice_id":self.restriction["id"]})
        payload={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1"}
        self.service.add_record(self.item["id"],payload,"recorder",'sensor_operator')
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],payload,"recorder",'sensor_operator')
        current=self.service.get_item(self.item["id"],"viewer")
        notices={"restricted":self.restriction["id"],"closed":self.closure["id"]}
        for target in STATES[1:-1]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0],({"notice_id":notices[target]} if target in notices else {}))
        with self.assertRaises(ConflictError): self.service.transition(current["id"],STATES[-1],current["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
    def test_restricted_requires_matching_active_notice(self):
        current=self.service.transition(self.item["id"],"warning",1,"op",'sensor_operator')
        with self.assertRaises(ValidationError):
            self.service.transition(current["id"],"restricted",current["version"],"eng",'bridge_engineer',{})
        # 封闭不能用限行通告
        with self.assertRaises(ValidationError):
            self.service.transition(current["id"],"restricted",current["version"],"eng",'bridge_engineer',{"notice_id":self.closure["id"]})
if __name__=="__main__": unittest.main()
