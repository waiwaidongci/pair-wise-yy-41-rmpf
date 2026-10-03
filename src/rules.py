from __future__ import annotations
from datetime import datetime
from .domain import SEVERITIES, ConflictError, ValidationError
TITLE='桥梁结构监测与限行决策'; ENTITY='桥梁告警'; BATCH_ENTITY='监测批次'; NOTICE_ENTITY='交通通告'; CONCLUSION_ENTITY='限行结论'; ID_PREFIX='BM'
STATES=['normal', 'warning', 'restricted', 'closed', 'restored']; TRANSITIONS={'normal': ['warning'], 'warning': ['restricted'], 'restricted': ['closed'], 'closed': ['restored'], 'restored': []}; TRANSITION_ROLES={'warning': ['sensor_operator'], 'restricted': ['bridge_engineer'], 'closed': ['traffic_authority'], 'restored': ['bridge_engineer']}
CREATE_ROLES=set(['sensor_operator']); RECORD_ROLES=set(['sensor_operator', 'bridge_engineer']); AUDIT_ROLES=set(['bridge_engineer', 'viewer']); VIEW_ROLES=set(['sensor_operator', 'bridge_engineer', 'traffic_authority', 'viewer'])
BATCH_ROLES=set(['sensor_operator']); NOTICE_ROLES=set(['traffic_authority']); VOID_BATCH_ROLES=set(['sensor_operator', 'bridge_engineer'])
# 进入限行/封闭需要的交通通告类型
NOTICE_REQUIREMENT={'restricted': 'restriction', 'closed': 'closure'}
SEVERITY_WEIGHT={'normal': 1.0, 'watch': 3.0, 'warning': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'normal': 72, 'watch': 24, 'warning': 8, 'critical': 4}; TERMINAL_STATES=set(['restored'])
SEVERITY_ORDER={name: idx for idx, name in enumerate(SEVERITIES)}
_WARRANTED_STATE={'normal': 'normal', 'watch': 'normal', 'warning': 'warning', 'critical': 'warning'}
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def window_active(effective_from: str, effective_to, now: datetime) -> bool:
    """当前时刻是否落在有效时间窗内；effective_to为空表示长期有效。"""
    start=datetime.fromisoformat(effective_from)
    if now < start:
        return False
    if effective_to is not None and now >= datetime.fromisoformat(effective_to):
        return False
    return True
def max_severity(values):
    result='normal'
    for value in values:
        if SEVERITY_ORDER[value] > SEVERITY_ORDER[result]:
            result=value
    return result
def aggregate_batches(rows):
    """同一桥梁的在窗批次合算：取最高告警等级，驱动量取最大偏差。"""
    if not rows:
        return {'severity': 'normal', 'quantity': 0.0, 'reading_count': 0}
    return {
        'severity': max_severity(r['severity'] for r in rows),
        'quantity': max(float(r['quantity']) for r in rows),
        'reading_count': sum(int(r['reading_count']) for r in rows),
    }
def warranted_state(severity: str) -> str:
    """监测合算结果所能支持的最高告警状态；限行/封闭必须另有交通通告与人工决策。"""
    return _WARRANTED_STATE[severity]
def closure_escalation(severity: str) -> bool:
    """critical等级要求封闭升级。"""
    return severity == 'critical'
def notice_covers(notice: dict, target: str, now: datetime) -> bool:
    """通告是否支持目标状态：版本须与绑定时一致，类型匹配，且仍在通告时间窗内。"""
    if notice is None or notice.get('status') != 'active':
        return False
    required=NOTICE_REQUIREMENT.get(target)
    if required is None or notice.get('notice_type') != required:
        return False
    return window_active(notice['effective_from'], notice['effective_to'], now)
