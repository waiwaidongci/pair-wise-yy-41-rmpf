from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .domain import (BATCH_FINALIZED, BATCH_VOID, BATCH_WRITING, ConflictError,
                     NotFoundError, NOTICE_ACTIVE, ValidationError, canonical_ts,
                     ensure_role, normalize_severity, normalize_window, parse_ts,
                     require_int, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_ENTITY, BATCH_ROLES, CONCLUSION_ENTITY,
                    CREATE_ROLES, ENTITY, NOTICE_ENTITY, NOTICE_ROLES,
                    RECORD_ROLES, VIEW_ROLES, VOID_BATCH_ROLES,
                    aggregate_batches, closure_escalation, completion_blockers,
                    escalation_required, notice_covers,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition, warranted_state, window_active)


class Service:
    def __init__(self, repository: Repository, clock: Optional[Any] = None):
        self.repository = repository
        # 可注入时钟，便于按时间窗测试；clock()返回UTC datetime
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(timezone.utc)

    def _now_ts(self) -> str:
        return canonical_ts(self._now())

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ---------------- 桥梁告警 ----------------

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
            raise ValidationError("status必须是open或closed")
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
                   actor: str, role: str,
                   payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        payload = payload or {}
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        bound_id = item.get("bound_notice_id")
        bound_version = item.get("bound_notice_version")
        # 进入限行/封闭前必须绑定交通通告：类型匹配、版本一致、在通告时间窗内
        if target in ("restricted", "closed"):
            notice = self._load_bindable_notice(payload, target)
            bound_id = notice["id"]
            bound_version = notice["version"]
            unbind = False
        else:
            notice = None
            unbind = target in ("normal", "restored")
        updated = self.repository.transition_item(
            item_id, target, expected_version, actor,
            bound_notice_id=bound_id if target in ("restricted", "closed") else None,
            bound_notice_version=(bound_version
                                  if target in ("restricted", "closed") else None),
            unbind=unbind)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "notice_id": bound_id if target in ("restricted", "closed") else None,
            "notice_version": (bound_version
                               if target in ("restricted", "closed") else None),
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        # 人工决策即时固化为一版结论
        latest = self.repository.latest_conclusion(item_id)
        if latest is None or latest["status"] != target:
            self.repository.insert_conclusion(
                item_id, self._now_ts(), target, updated["severity"],
                updated["quantity"], updated["threshold"], 0,
                bound_id if target in ("restricted", "closed") else None,
                bound_version if target in ("restricted", "closed") else None,
                [], f"人工决策：{item['status']}->{target}")
        return self.enrich(updated)

    def _load_bindable_notice(self, payload: Dict[str, Any], target: str) -> Dict[str, Any]:
        notice_id = payload.get("notice_id")
        notice_id = require_int(notice_id, "notice_id", minimum=1)
        notice = self.repository.get_notice(notice_id)
        if not notice_covers(notice, target, self._now()):
            raise ValidationError("交通通告不存在、已作废、类型不匹配或不在有效时间窗内")
        return notice

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def get_live_item(self, item_id: int, role: str) -> Dict[str, Any]:
        """值班台口径：读取时按当前时间窗即时重算，过期结论不会继续生效。"""
        self._view(role)
        self._evaluate(item_id, reason="live_check")
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

    # ---------------- 监测批次 ----------------

    def register_batch(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """登记监测批次（批次头）。同一批次号并发/重试提交：保留首次登记，返回同一份。"""
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        effective_from, effective_to = normalize_window(payload)
        observed = parse_ts(payload.get("observed_at", effective_from), "observed_at")
        severity = normalize_severity(payload.get("severity", "normal"))
        threshold = require_number(
            payload.get("threshold", item["threshold"]),
            "threshold", 0.000001)
        chunks_total = payload.get("chunks_total")
        if chunks_total is not None:
            chunks_total = require_int(chunks_total, "chunks_total", minimum=1, maximum=100000)
        batch = self.repository.register_batch(
            batch_no, item_id, effective_from, effective_to,
            canonical_ts(observed), severity, threshold, chunks_total, actor)
        replayed = batch is None
        if replayed:
            batch = self.repository.get_batch_by_no(batch_no)
            if batch["item_id"] != item_id:
                raise ConflictError("批次号已用于其他桥梁告警")
        else:
            self.repository.append_audit("batch_register", BATCH_ENTITY, batch["id"],
                                         actor, {"batch_no": batch_no,
                                                 "item_id": item_id,
                                                 "chunks_total": chunks_total})
        result = dict(batch)
        result["replayed"] = replayed
        return result

    def put_reading(self, item_id: int, batch_no: str, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        """按批次号续写读数分片。中断重发同一分片：幂等返回首次写入的那份。"""
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self._batch_of_item(item_id, batch_no)
        chunk_index = require_int(payload.get("chunk_index"), "chunk_index", minimum=0)
        quantity = require_number(payload.get("quantity"), "quantity")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 2000)
        reading = self.repository.insert_reading(
            batch["id"], chunk_index, quantity, note, actor)
        replayed = reading is None
        if replayed:
            reading = self.repository.get_reading(batch["id"], chunk_index)
        else:
            self.repository.append_audit(
                "reading_append", BATCH_ENTITY, batch["id"], actor,
                {"batch_no": batch_no, "chunk_index": chunk_index,
                 "quantity": quantity})
        result = dict(reading)
        result["replayed"] = replayed
        result["batch"] = self._batch_progress(batch["id"])
        return result

    def finalize_batch(self, item_id: int, batch_no: str, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """定稿批次并触发合算重算。重复定稿（断线重连）返回首次结果，不重复审计。"""
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self._batch_of_item(item_id, batch_no)
        if batch["status"] == BATCH_WRITING:
            received = len(self.repository.list_readings(batch["id"]))
            if batch["chunks_total"] is not None and received < batch["chunks_total"]:
                raise ConflictError(
                    f"读数分片未收齐：{received}/{batch['chunks_total']}，按批次号续写后再定稿")
            if not self.repository.mark_batch_finalized(batch["id"]):
                raise ConflictError("批次定稿失败")
            batch = self.repository.get_batch(batch["id"])
            self.repository.append_audit(
                "batch_finalize", BATCH_ENTITY, batch["id"], actor,
                {"batch_no": batch_no, "item_id": item_id})
            self._evaluate(item_id, trigger_batch_id=batch["id"],
                           reason="batch_finalize", actor=actor)
        elif batch["status"] == BATCH_VOID:
            raise ConflictError("批次已作废，不能定稿")
        # 已定稿：幂等回读
        return self._batch_result(item_id, batch)

    def void_batch(self, item_id: int, batch_no: str, payload: Dict[str, Any],
                   actor: str, role: str) -> Dict[str, Any]:
        """批次作废：立即失效并强制重算（即使新结论的基准时间更旧也要落账）。"""
        ensure_role(role, VOID_BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self._batch_of_item(item_id, batch_no)
        reason = require_text(payload.get("reason", "批次作废"), "reason")
        self.repository.void_batch(batch["id"], reason)
        self.repository.append_audit(
            "batch_void", BATCH_ENTITY, batch["id"], actor,
            {"batch_no": batch_no, "item_id": item_id, "reason": reason})
        self._evaluate(item_id, trigger_batch_id=batch["id"], force=True,
                       reason="batch_void", actor=actor)
        return self._batch_result(item_id, self.repository.get_batch(batch["id"]))

    def list_batches(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_batches(item_id)

    def _batch_of_item(self, item_id: int, batch_no: str) -> Dict[str, Any]:
        self.repository.get_item(item_id)
        batch = self.repository.get_batch_by_no(require_text(batch_no, "batch_no", 100))
        if batch["item_id"] != item_id:
            raise NotFoundError("监测批次不存在")
        return batch

    def _batch_progress(self, batch_id: int) -> Dict[str, Any]:
        batch = self.repository.get_batch(batch_id)
        return {"id": batch_id, "batch_no": batch["batch_no"],
                "status": batch["status"],
                "chunks_received": batch["chunks_received"],
                "chunks_total": batch["chunks_total"]}

    def _batch_result(self, item_id: int, batch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(batch)
        result["replayed"] = batch["status"] == BATCH_FINALIZED
        result["readings"] = self.repository.list_readings(batch["id"])
        result["conclusion"] = self.repository.latest_conclusion(item_id)
        return result

    # ---------------- 交通通告 ----------------

    def create_notice(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, NOTICE_ROLES)
        actor = require_text(actor, "actor", 100)
        notice_type = payload.get("notice_type")
        if notice_type not in ("restriction", "closure"):
            raise ValidationError("notice_type必须是restriction或closure")
        title = require_text(payload.get("title"), "title", 200)
        detail = require_text(payload.get("detail"), "detail")
        effective_from, effective_to = normalize_window(payload)
        notice = self.repository.create_notice(
            notice_type, title, detail, effective_from, effective_to, actor)
        self.repository.append_audit("notice_create", NOTICE_ENTITY, notice["id"],
                                     actor, {"notice_type": notice_type,
                                             "title": title})
        return notice

    def update_notice(self, notice_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        """通告修改后立即让绑定旧版本的限行/封闭失效重算。"""
        ensure_role(role, NOTICE_ROLES)
        actor = require_text(actor, "actor", 100)
        existing = self.repository.get_notice(notice_id)
        title = require_text(payload.get("title", existing["title"]), "title", 200)
        detail = require_text(payload.get("detail", existing["detail"]), "detail")
        effective_from, effective_to = normalize_window(
            payload,
            effective_from=existing["effective_from"],
            effective_to=existing["effective_to"])
        outcome = self.repository.update_notice(
            notice_id, title, detail, effective_from, effective_to)
        notice = outcome["notice"]
        self.repository.append_audit("notice_update", NOTICE_ENTITY, notice_id, actor, {
            "version": notice["version"],
            "bound_item_ids": outcome["bound_item_ids"],
        })
        for bound_item_id in outcome["bound_item_ids"]:
            self._evaluate(bound_item_id, trigger_notice_id=notice_id, force=True,
                           reason="notice_update", actor=actor)
        return notice

    def void_notice(self, notice_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, NOTICE_ROLES)
        actor = require_text(actor, "actor", 100)
        outcome = self.repository.void_notice(notice_id)
        self.repository.append_audit("notice_void", NOTICE_ENTITY, notice_id, actor, {
            "bound_item_ids": outcome["bound_item_ids"],
        })
        for bound_item_id in outcome["bound_item_ids"]:
            self._evaluate(bound_item_id, trigger_notice_id=notice_id, force=True,
                           reason="notice_void", actor=actor)
        return outcome["notice"]

    def list_notices(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_notices(status)

    def list_conclusions(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_conclusions(item_id)

    # ---------------- 合算与重算 ----------------

    def _evaluate(self, item_id: int, *, force: bool = False,
                  trigger_batch_id: Optional[int] = None,
                  trigger_notice_id: Optional[int] = None,
                  reason: str = "recompute",
                  actor: str = "system") -> Optional[Dict[str, Any]]:
        """同一桥梁在窗批次合算，生成单调版本化结论。

        - 在窗批次取最高告警等级（乱序到达也合算），basis_time为窗内最新观测时间；
        - 旧批次到达若不能产生更新结论，只记审计，绝不盖掉较新的重算结果；
        - force用于通告修改/批次作废等失效事件，即使basis_time更旧也必须重算。
        """
        now = self._now()
        now_ts = canonical_ts(now)
        item = self.repository.get_item(item_id)
        rows = self.repository.active_batches_for(item_id, now_ts)
        agg = aggregate_batches(rows)
        basis = max((r["observed_at"] for r in rows), default=now_ts)
        batch_ids = sorted(int(r["id"]) for r in rows)

        bound = None
        binding_valid = False
        if item.get("bound_notice_id") is not None:
            try:
                bound = self.repository.get_notice(item["bound_notice_id"])
                binding_valid = (
                    bound["status"] == NOTICE_ACTIVE
                    and bound["version"] == item.get("bound_notice_version")
                    and window_active(bound["effective_from"], bound["effective_to"], now))
            except Exception:
                bound = None
                binding_valid = False

        candidate = warranted_state(agg["severity"])
        current = item["status"]
        target = current
        if current == "restored":
            target = "restored"  # 终态不再被监测自动拉起
        elif current in ("restricted", "closed"):
            # 人工限行/封闭只能因绑定失效、或监测不再支撑而自动降级
            if not binding_valid or candidate == "normal":
                target = candidate
            elif candidate == "warning" and current == "restricted":
                target = "warning"
            # closed + 仍有warning/critical支撑：保持封闭
        else:
            target = candidate

        unbind = current in ("restricted", "closed") and target != current

        latest = self.repository.latest_conclusion(item_id)
        effective_notice = bound if (target in ("restricted", "closed")
                                     and binding_valid) else None

        # 绑定失效（通告改版/作废/过窗）属于失效事件，即使basis_time更旧也必须重算
        invalidated = (current in ("restricted", "closed")
                       and (not binding_valid or target != current))
        # 旧批次不能盖掉较新的重算结果：
        # - 基准时间更旧：一律不落新结论（失效事件除外）；
        # - 基准时间相同且合算形态一致：幂等跳过；
        # - 基准时间相同但形态变化（同刻迟到批次改变最高等级）：允许重算落账。
        same_shape = (
            latest is not None
            and latest["status"] == target
            and latest["severity"] == agg["severity"]
            and abs(float(latest["quantity"]) - agg["quantity"]) < 1e-9
            and (latest.get("bound_notice_id")
                 == (effective_notice["id"] if effective_notice else None)))
        stale_basis = latest is not None and basis < latest["basis_time"]
        if not force and not invalidated and (stale_basis or same_shape):
            # 值班台轮询无变化不留痕；晚到批次等真实事件仍记跳过审计
            if reason != "live_check":
                self.repository.append_audit(
                    "recompute_skip", CONCLUSION_ENTITY, item_id, actor, {
                        "reason": reason, "basis_time": basis,
                        "latest_basis_time": latest["basis_time"],
                        "same_shape": bool(same_shape),
                        "trigger_batch_id": trigger_batch_id,
                        "trigger_notice_id": trigger_notice_id})
            return latest

        if target != current:
            self.repository.apply_recompute(
                item_id, target, agg["severity"], agg["quantity"],
                effective_notice["id"] if effective_notice else None,
                effective_notice["version"] if effective_notice else None)
        else:
            # 即便状态不变，也要刷新监测合算字段
            self.repository.apply_recompute(
                item_id, target, agg["severity"], agg["quantity"],
                item.get("bound_notice_id") if binding_valid else None,
                item.get("bound_notice_version") if binding_valid else None)

        conclusion = self.repository.insert_conclusion(
            item_id, basis, target, agg["severity"], agg["quantity"],
            item["threshold"], agg["reading_count"],
            effective_notice["id"] if effective_notice else None,
            effective_notice["version"] if effective_notice else None,
            batch_ids,
            self._recompute_reason(reason, target, current, unbind, binding_valid))
        self.repository.append_audit("recompute", CONCLUSION_ENTITY, item_id, actor, {
            "reason": reason, "from": current, "to": target,
            "basis_time": basis, "batch_ids": batch_ids,
            "severity": agg["severity"], "forced": bool(force),
            "binding_valid": binding_valid,
            "trigger_batch_id": trigger_batch_id,
            "trigger_notice_id": trigger_notice_id,
            "conclusion_id": conclusion["id"]})
        return conclusion

    @staticmethod
    def _recompute_reason(reason: str, target: str, current: str,
                          unbind: bool, binding_valid: bool) -> str:
        if unbind and not binding_valid:
            return f"{reason}: 通告绑定失效，{current}->{target}"
        if target != current:
            return f"{reason}: 监测合算变化，{current}->{target}"
        return f"{reason}: 在窗批次重算，状态保持{target}"

    # ---------------- 汇总 ----------------

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["closure_escalation"] = closure_escalation(item["severity"])
        return result
