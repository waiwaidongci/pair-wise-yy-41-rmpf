from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='桥梁结构监测与限行决策'; ENTITY='桥梁告警'; ID_PREFIX='BM'
SEVERITIES=['normal', 'watch', 'warning', 'critical']; STATES=['normal', 'warning', 'restricted', 'closed', 'restored']; TRANSITIONS={'normal': ['warning'], 'warning': ['restricted'], 'restricted': ['closed'], 'closed': ['restored'], 'restored': []}; TRANSITION_ROLES={'warning': ['sensor_operator'], 'restricted': ['bridge_engineer'], 'closed': ['traffic_authority'], 'restored': ['bridge_engineer']}
CREATE_ROLES=set(['sensor_operator']); RECORD_ROLES=set(['sensor_operator', 'bridge_engineer']); AUDIT_ROLES=set(['bridge_engineer', 'viewer']); VIEW_ROLES=set(['sensor_operator', 'bridge_engineer', 'traffic_authority', 'viewer'])
SEVERITY_WEIGHT={'normal': 1.0, 'watch': 3.0, 'warning': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'normal': 72, 'watch': 24, 'warning': 8, 'critical': 4}; TERMINAL_STATES=set(['restored'])
BATCH_STATUSES=['active','void']; NOTICE_STATUSES=['active','modified','void']; NOTICE_BINDING_STATUSES=['active','released']
BATCH_SUBMIT_ROLES=set(['sensor_operator','bridge_engineer']); BATCH_VOID_ROLES=set(['bridge_engineer']); NOTICE_MANAGE_ROLES=set(['traffic_authority','bridge_engineer']); NOTICE_BIND_ROLES=set(['bridge_engineer','traffic_authority'])
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
def validate_time_window(valid_from,valid_to):
    if not valid_from or not valid_to: raise ValidationError("有效时间窗不能为空")
    if valid_from>=valid_to: raise ValidationError("valid_from必须早于valid_to")
    return valid_from,valid_to
def severity_to_conclusion_status(severity,quantity=0.0,threshold=1.0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    if severity=='critical' or (threshold>0 and quantity>=threshold): return 'closed'
    if severity=='warning': return 'restricted'
    if severity=='watch': return 'warning'
    return 'normal'
def reconcile_batches(batches):
    active=[b for b in batches if b.get('status')=='active']
    if not active: return None
    return max(active,key=lambda b:(b.get('valid_from',''),b.get('valid_to','')))
def derive_conclusion(batch,notice):
    if batch is None:
        return {'status':'normal','batch_no':None,'notice_no':None,'severity':'normal','quantity':0.0,'threshold':1.0}
    payload=batch.get('payload',{})
    severity=payload.get('severity','normal'); quantity=payload.get('quantity',0.0); threshold=payload.get('threshold',1.0)
    status=severity_to_conclusion_status(severity,quantity,threshold)
    notice_no=None
    if notice and notice.get('status') in ('active','modified'): notice_no=notice.get('notice_no')
    return {'status':status,'batch_no':batch.get('batch_no'),'notice_no':notice_no,'severity':severity,'quantity':quantity,'threshold':threshold}
