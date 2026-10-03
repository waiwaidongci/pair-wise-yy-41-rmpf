from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['normal', 'watch', 'warning', 'critical']; STATES=['normal', 'warning', 'restricted', 'closed', 'restored']; ROLES=['sensor_operator', 'bridge_engineer', 'traffic_authority', 'viewer']
BATCH_STATUSES=['writing', 'finalized', 'void']; BATCH_WRITING='writing'; BATCH_FINALIZED='finalized'; BATCH_VOID='void'
NOTICE_TYPES=['restriction', 'closure']; NOTICE_ACTIVE='active'; NOTICE_VOID='void'
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def require_int(value,field,minimum=None,maximum=None):
    if isinstance(value,bool) or not isinstance(value,int):
        raise ValidationError(f"{field}必须是整数")
    if minimum is not None and value<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    if maximum is not None and value>maximum: raise ValidationError(f"{field}不能大于{maximum}")
    return value
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def parse_ts(value: Any, field: str = "时间") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}必须是ISO8601时间字符串")
    try:
        dt=datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field}必须是ISO8601时间字符串") from exc
    if dt.tzinfo is None:
        dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
def canonical_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
def normalize_window(payload: Dict[str, Any], *, effective_from: Optional[str]=None,
                     effective_to: Optional[str]=None) -> Tuple[str, Optional[str]]:
    start_raw=payload.get("effective_from", effective_from)
    start=parse_ts(start_raw, "effective_from") if start_raw is not None else datetime.now(timezone.utc)
    end_raw=payload.get("effective_to", effective_to)
    end=parse_ts(end_raw, "effective_to") if end_raw is not None else None
    if end is not None and end<=start:
        raise ValidationError("effective_to必须晚于effective_from")
    return canonical_ts(start), (canonical_ts(end) if end else None)
