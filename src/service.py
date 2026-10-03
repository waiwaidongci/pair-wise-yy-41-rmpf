from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, ValidationError,
                     ensure_role, normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_SUBMIT_ROLES, BATCH_VOID_ROLES,
                    CREATE_ROLES, ENTITY, NOTICE_BIND_ROLES, NOTICE_MANAGE_ROLES,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    derive_conclusion, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    severity_to_conclusion_status, validate_time_window,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        if target in ("restricted", "closed"):
            binding = self.repository.get_active_binding(item_id)
            if binding is None:
                raise ConflictError("进入限行或封闭前必须绑定交通通告")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- batches ----
    def submit_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        bridge_id = require_text(payload.get("bridge_id"), "bridge_id", 100)
        valid_from = require_text(payload.get("valid_from"), "valid_from", 100)
        valid_to = require_text(payload.get("valid_to"), "valid_to", 100)
        validate_time_window(valid_from, valid_to)
        raw_payload = payload.get("payload", {})
        if not isinstance(raw_payload, dict):
            raise ValidationError("payload必须是JSON对象")
        severity = normalize_severity(raw_payload.get("severity", "normal"))
        quantity = require_number(raw_payload.get("quantity", 0), "quantity")
        threshold = require_number(raw_payload.get("threshold", 1), "threshold", 0.000001)
        batch_payload = {"severity": severity, "quantity": quantity, "threshold": threshold}
        batch, created = self.repository.create_batch(
            batch_no, bridge_id, valid_from, valid_to, batch_payload, actor)
        if created:
            latest = self.repository.get_latest_active_batch(bridge_id)
            if latest and latest["batch_no"] == batch["batch_no"]:
                self._recalculate(bridge_id, "batch_submit", actor)
            self.repository.update_checkpoint(bridge_id, batch_no)
            self.repository.append_audit("batch_submit", "bridge", bridge_id, actor, {
                "batch_no": batch_no, "bridge_id": bridge_id,
                "valid_from": valid_from, "valid_to": valid_to,
            })
        return batch

    def get_batch(self, batch_no: str, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_batch(batch_no)

    def void_batch(self, batch_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_VOID_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.void_batch(batch_no, actor)
        bridge_id = batch["bridge_id"]
        self._recalculate(bridge_id, "batch_void", actor)
        self.repository.append_audit("batch_void", "bridge", bridge_id, actor, {
            "batch_no": batch_no,
        })
        return batch

    def list_batches(self, bridge_id: str, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_batches(bridge_id, status)

    def get_bridge_conclusion(self, bridge_id: str, role: str) -> Dict[str, Any]:
        self._view(role)
        conclusion = self.repository.get_current_conclusion(bridge_id)
        if conclusion is None:
            return {"bridge_id": bridge_id, "version": 0, "conclusion": None}
        return conclusion

    def get_write_checkpoint(self, bridge_id: str, role: str) -> Dict[str, Any]:
        self._view(role)
        checkpoint = self.repository.get_checkpoint(bridge_id)
        return {"bridge_id": bridge_id, "last_batch_no": checkpoint}

    # ---- notices ----
    def create_notice(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, NOTICE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        notice_no = require_text(payload.get("notice_no"), "notice_no", 100)
        bridge_id = require_text(payload.get("bridge_id"), "bridge_id", 100)
        title = require_text(payload.get("title"), "title", 200)
        content = require_text(payload.get("content"), "content")
        effective_from = require_text(payload.get("effective_from"), "effective_from", 100)
        effective_to = payload.get("effective_to")
        if effective_to is not None:
            effective_to = require_text(effective_to, "effective_to", 100)
        notice = self.repository.create_notice(
            notice_no, bridge_id, title, content, effective_from, effective_to, actor)
        self._recalculate(bridge_id, "notice_create", actor)
        self.repository.append_audit("notice_create", "notice", notice["id"], actor, {
            "notice_no": notice_no, "bridge_id": bridge_id,
        })
        return notice

    def modify_notice(self, notice_no: str, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, NOTICE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        content = require_text(payload.get("content"), "content")
        effective_from = require_text(payload.get("effective_from"), "effective_from", 100)
        effective_to = payload.get("effective_to")
        if effective_to is not None:
            effective_to = require_text(effective_to, "effective_to", 100)
        notice = self.repository.modify_notice(
            notice_no, title, content, effective_from, effective_to, actor)
        bridge_id = notice["bridge_id"]
        self._recalculate(bridge_id, "notice_modify", actor)
        self.repository.append_audit("notice_modify", "notice", notice["id"], actor, {
            "notice_no": notice_no,
        })
        return notice

    def void_notice(self, notice_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, NOTICE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        notice = self.repository.void_notice(notice_no, actor)
        bridge_id = notice["bridge_id"]
        self._recalculate(bridge_id, "notice_void", actor)
        self.repository.append_audit("notice_void", "notice", notice["id"], actor, {
            "notice_no": notice_no,
        })
        return notice

    def list_notices(self, bridge_id: str, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_notices(bridge_id, status)

    # ---- notice bindings ----
    def bind_notice(self, item_id: int, notice_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, NOTICE_BIND_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        notice = self.repository.get_notice(notice_no)
        if notice["status"] not in ("active", "modified"):
            raise ConflictError("通告已失效，不能绑定")
        binding = self.repository.bind_notice(item_id, notice["id"], actor)
        self.repository.append_audit("notice_bind", ENTITY, item_id, actor, {
            "notice_no": notice_no, "item_id": item_id,
        })
        return binding

    def _recalculate(self, bridge_id: str, trigger: str, actor: str) -> None:
        latest = self.repository.get_latest_active_batch(bridge_id)
        notice = self.repository.get_active_notice(bridge_id)
        conclusion = derive_conclusion(latest, notice)
        version = self.repository.next_conclusion_version(bridge_id)
        batch_id = latest["id"] if latest else None
        notice_id = notice["id"] if notice else None
        self.repository.store_conclusion(
            bridge_id, version, batch_id, notice_id, conclusion, actor)
        self.repository.append_audit("recalculate", "bridge", bridge_id, actor, {
            "version": version, "trigger": trigger,
            "batch_no": latest["batch_no"] if latest else None,
            "notice_no": notice["notice_no"] if notice else None,
        })

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
