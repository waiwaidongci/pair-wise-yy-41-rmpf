from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Tuple
from urllib.parse import urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        # /api/items/{id}/... 解析：返回(item_id, 剩余段)
        @staticmethod
        def _item_tail(path: str):
            parts = path.strip("/").split("/")
            if len(parts) < 3 or parts[0] != "api" or parts[1] != "items":
                return None
            try:
                item_id = int(parts[2])
            except ValueError:
                return None
            return item_id, parts[3:]

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    _, role = self._identity()
                    self._json(200, {"items": service.list_items(role)})
                elif path == "/api/notices":
                    _, role = self._identity()
                    self._json(200, {"notices": service.list_notices(role)})
                elif path == "/api/audit":
                    _, role = self._identity()
                    self._json(200, {"events": service.audit(role)})
                else:
                    parsed = self._item_tail(path)
                    if parsed is None:
                        self._json(404, {"error": "not_found"})
                        return
                    item_id, tail = parsed
                    _, role = self._identity()
                    if len(tail) == 0:
                        self._json(200, service.get_item(item_id, role))
                    elif tail == ["live"]:
                        self._json(200, service.get_live_item(item_id, role))
                    elif tail == ["records"]:
                        self._json(200, {"records": service.list_records(item_id, role)})
                    elif tail == ["batches"]:
                        self._json(200, {"batches": service.list_batches(item_id, role)})
                    elif tail == ["conclusions"]:
                        self._json(200, {"conclusions": service.list_conclusions(item_id, role)})
                    else:
                        self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path == "/api/notices":
                    self._json(201, service.create_notice(body, actor, role))
                else:
                    parsed = self._item_tail(path)
                    if parsed is None:
                        self._json(404, {"error": "not_found"})
                        return
                    item_id, tail = parsed
                    if tail == ["records"]:
                        self._json(201, service.add_record(item_id, body, actor, role))
                    elif tail == ["transition"]:
                        target = body.get("target")
                        expected = body.get("expected_version")
                        self._json(200, service.transition(
                            item_id, target, expected, actor, role, body))
                    elif tail == ["batches"]:
                        self._json(201, service.register_batch(
                            item_id, body, actor, role))
                    elif len(tail) == 3 and tail[0] == "batches" and tail[2] == "readings":
                        # POST 等价于 PUT，供不支持PUT的重试客户端使用
                        self._json(200, service.put_reading(
                            item_id, tail[1], body, actor, role))
                    elif len(tail) == 3 and tail[0] == "batches" and tail[2] == "finalize":
                        self._json(200, service.finalize_batch(
                            item_id, tail[1], body, actor, role))
                    elif len(tail) == 3 and tail[0] == "batches" and tail[2] == "void":
                        self._json(200, service.void_batch(
                            item_id, tail[1], body, actor, role))
                    else:
                        self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_PUT(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                parsed = self._item_tail(path)
                if parsed is None and path.startswith("/api/notices/"):
                    notice_id = int(path.rsplit("/", 1)[-1])
                    self._json(200, service.update_notice(notice_id, body, actor, role))
                elif parsed is not None:
                    item_id, tail = parsed
                    if len(tail) == 3 and tail[0] == "batches" and tail[2] == "readings":
                        self._json(200, service.put_reading(
                            item_id, tail[1], body, actor, role))
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_DELETE(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                if path.startswith("/api/notices/"):
                    notice_id = int(path.rsplit("/", 1)[-1])
                    self._json(200, service.void_notice(notice_id, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
