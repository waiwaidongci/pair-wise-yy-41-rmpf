import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.client import RemoteDisconnected
from pathlib import Path

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service
from http.server import ThreadingHTTPServer


def post(url, payload, actor="op", role="sensor_operator", method="POST"):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "X-Actor": actor, "X-Role": role})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Repository(str(Path(cls.tmp.name) / "http.db"))
        cls.service = Service(cls.repo)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         make_handler(cls.service, "static"))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.repo.close()
        cls.tmp.cleanup()

    def test_parallel_same_batch_submission_single_registration(self):
        _, item = post(f"http://127.0.0.1:{self.port}/api/items", {
            "title": "并发桥", "description": "d", "severity": "normal",
            "threshold": 10, "external_ref": "HTTP-1",
        })
        item_id = item["id"]
        payload = {"batch_no": "HTTP-BATCH",
                   "effective_from": "2026-10-01T00:00:00Z",
                   "observed_at": "2026-10-03T10:00:00Z",
                   "severity": "warning", "chunks_total": 1}
        results = []
        errors = []

        def submit(actor):
            try:
                status, body = post(
                    f"http://127.0.0.1:{self.port}/api/items/{item_id}/batches",
                    payload, actor=actor)
                results.append((status, body))
            except (RemoteDisconnected, OSError) as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(f"op{i}",))
                   for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        ids = {body["id"] for _, body in results}
        self.assertEqual(len(ids), 1)
        actors = {body["registered_by"] for _, body in results}
        self.assertEqual(len(actors), 1)
        first_count = sum(1 for _, body in results if not body["replayed"])
        self.assertEqual(first_count, 1)

        # 并发续写不同分片 + 同一分片重发
        read_results = []

        def put_reading(chunk_index):
            status, body = post(
                f"http://127.0.0.1:{self.port}/api/items/{item_id}/batches/HTTP-BATCH/readings",
                {"chunk_index": chunk_index, "quantity": float(chunk_index + 1)},
                method="PUT")
            read_results.append(body)

        jobs = [0, 0, 0, 0]  # 4个线程同时写分片0，只允许一份首次写入
        rthreads = [threading.Thread(target=put_reading, args=(c,)) for c in jobs]
        for t in rthreads: t.start()
        for t in rthreads: t.join()
        self.assertEqual(len({r["id"] for r in read_results}), 1)
        self.assertEqual(sum(1 for r in read_results if not r["replayed"]), 1)


if __name__ == "__main__":
    unittest.main()
