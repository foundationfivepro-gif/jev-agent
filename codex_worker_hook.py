"""Repo-scoped PreToolUse handler. Approved fixed synthetic worker only.

No hook registration/trust is changed by this module. Valid failures return an
explicit deny; host failures to launch/parse a hook remain outside this boundary.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent


def deny():
    return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny',
        'permissionDecisionReason':'JEV routing unavailable or outside the approved synthetic worker scope.'}}


def handle(event, *, host_factory=None, clock=time.time):
    try:
        from codex_operator import SYNTHETIC_MESSAGE, ACCEPTANCE, build_host
        from live_routing import LiveRoutingError
        host_factory = host_factory or build_host
        if (event.get('hook_event_name')!='PreToolUse' or event.get('tool_name') not in {'Agent','spawn_agent'}
                or Path(event['cwd']).resolve()!=ROOT or not isinstance(event['session_id'],str)
                or not event['session_id'] or len(event['session_id'])>200):
            raise LiveRoutingError()
        args=event['tool_input']
        # One observed argument vocabulary; unknown controls never pass through.
        if (not isinstance(args,dict) or set(args)-{'message','model','reasoning_effort','fork_context','agent_type'}
                or args.get('message')!=SYNTHETIC_MESSAGE or args.get('fork_context',False) is not False
                or args.get('agent_type','default')!='default'):
            raise LiveRoutingError()
        # No inherited parent transcript can leave for an approved synthetic task.
        if any(not isinstance(event[k],str) or not 0 < len(event[k]) <= 256
               for k in ('turn_id','tool_use_id')):
            raise LiveRoutingError()
        token=json.dumps({'session':event['session_id'],'turn':event['turn_id'],'call':event['tool_use_id']},sort_keys=True)
        identity=hashlib.sha256(token.encode()).hexdigest()
        request={'request_id':'hook-'+identity,'purpose':SYNTHETIC_MESSAGE,'acceptance_criteria':ACCEPTANCE,
            'context_references':[],'data_class':'public','authorized_destinations':['openrouter.ai'],
            'budget_usd':.002,'deadline':clock()+25,'task_kind':'narrow','required_tools':[],
            'parent_has_context':False}
        if args.get('model') is not None:
            request.update(explicit_model_id=args['model'],explicit_model_namespace='openai_native')
        if args.get('reasoning_effort') is not None:
            request['explicit_effort']=args['reasoning_effort']
        host=host_factory(session_id=event['session_id'])
        receipt=host.recommend(request)
        if (receipt.status!='recommended' or receipt.routing_evidence_status!='jev_observed'
                or receipt.routing_cost_kind!='billed' or receipt.execution_mode!='delegate'
                or receipt.selected_model_namespace!='openai_native' or clock()>=request['deadline']):
            raise LiveRoutingError()
        # A routing receipt is not native execution authorization. The local host
        # currently exposes no verified billing reservation/tool-restriction contract.
        result=deny()
        result['hookSpecificOutput']['permissionDecisionReason'] = (
            'JEV recommendation validated; native execution budget and tool restrictions are not bound.')
        result['hookSpecificOutput']['additionalContext'] = json.dumps({
            'decision_id':receipt.routing_decision_id,'model':receipt.selected_model_id,
            'reasoning_effort':receipt.selected_effort,'execution_authorized':False})
        return result
    except Exception:
        return deny()


def main():
    try:
        raw=sys.stdin.buffer.read(32769)
        result=handle(json.loads(raw)) if len(raw)<=32768 else deny()
    except Exception:
        result=deny()
    print(json.dumps(result,separators=(',',':')))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
