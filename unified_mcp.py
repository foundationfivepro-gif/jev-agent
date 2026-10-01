"""Three-tool, operator-bound local server. No credential discovery or dispatch."""
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from pydantic import Field
from typing import Annotated
from core import Answer, Decision, _questions_to_wire, _validate, decide_batched
from context_tier import Chunk, select
from host_contracts import TaskEnvelope
from live_pilot import Approval, PilotLedger, _money
from live_routing import (BoundHostRouting, OpenRouterSelector, ProtectedOpenRouterTransport,
                          RoutingAuthorization, LiveRoutingError, payload_fingerprint)
from permission_gate import gate, triage, extract_commands
from privacy import screen_outbound
from safe_mcp import SafeMCPServer
from workspace_boundary import ApprovedWorkspace, WorkspaceError


MAX_CONTEXT_BYTES = 8 * 1024 * 1024


class CredentialUnavailable(ValueError):
    def __init__(self):
        super().__init__('credential_unavailable: launch with the approved host-injected OpenRouter credential for the selected mode; no credential discovery or provider fallback')


def protected_transport(provider):
    """The separately reviewed provider owns credential provenance and access."""
    try:
        placeholder, proxy = provider()
        if not isinstance(placeholder, str) or not placeholder:
            raise ValueError()
        return ProtectedOpenRouterTransport(issued_placeholder=lambda: placeholder, https_proxy=proxy)
    except Exception:
        raise CredentialUnavailable() from None


def open_existing_ledger(path, approval, *, minimum_charged_usd):
    """No new database, reset, migration, transfer, or credential lookup."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise LiveRoutingError()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT approval, cap FROM pilot WHERE id=1').fetchone()
        if row is None or row[0] != approval.approval_id:
            raise LiveRoutingError()
        charged = db.execute('SELECT COALESCE(SUM(charged),0) FROM operations').fetchone()[0]
        if Decimal(charged) / Decimal(10**9) < _money(minimum_charged_usd):
            raise LiveRoutingError()
    return PilotLedger(path, approval)


@dataclass(frozen=True)
class OperationGrant:
    operation_id: str
    arguments_sha256: str
    ceiling_usd: str
    expires_at: float

    def validate(self, arguments, now):
        from host_contracts import Identifier
        from pydantic import TypeAdapter
        TypeAdapter(Identifier).validate_python(self.operation_id, strict=True)
        if (self.arguments_sha256 != payload_fingerprint(arguments)
                or not now < self.expires_at <= now + 300):
            raise LiveRoutingError()
        ceiling = _money(self.ceiling_usd)
        if ceiling <= 0:
            raise LiveRoutingError()
        return ceiling


class AccountedDecisions:
    """Reserve an entire gate/context operation, including every batch/refinement.

    One durable operation ID cannot repeat uncertain or failed requests. Every
    subcall needs a complete host-verified quote within the remaining reservation.
    Unknown cost holds the entire reservation; no retries or alternate providers.
    """
    def __init__(self, *, ledger, transport, quote, clock=time.time, authorize_operation=None):
        self.ledger, self.transport, self.quote, self.clock = ledger, transport, quote, clock
        self.authorize_operation = authorize_operation

    def run(self, tool, arguments, grant, invoke, *, content_fingerprint=None):
        screen_outbound(arguments)
        if type(grant) is not OperationGrant:
            raise LiveRoutingError()
        cap = grant.validate(arguments, self.clock())
        fingerprint = payload_fingerprint({'tool':tool, 'arguments':arguments, 'content':content_fingerprint})
        key = 'unified:' + grant.operation_id
        claim = self.ledger.claim(key, fingerprint, cap)
        if claim['status'] != 'claimed':
            if (claim['status'] == 'completed' and _money(claim['cost_usd']) <= cap
                    and self.clock() < claim.get('expires_at', 0)):
                return claim['result']
            raise LiveRoutingError()
        total = Decimal(0)
        uncertain = False
        receipts = []
        def decide(state, questions):
            nonlocal total, uncertain
            if (self.clock() >= grant.expires_at or (self.authorize_operation is not None
                    and self.authorize_operation(tool, arguments) != grant)):
                raise LiveRoutingError()
            wire = _questions_to_wire(questions)
            payload = {'model':'jev-latest', 'state':state, 'questions':wire}
            screen_outbound(payload)
            quote = self.quote(tool, arguments, payload)
            if type(quote) is not RoutingAuthorization:
                raise LiveRoutingError()
            ceiling = quote.validate(payload, self.clock())
            if ceiling <= 0 or total + ceiling > cap:
                raise LiveRoutingError()
            timeout = min(30., grant.expires_at-self.clock(), quote.expires_at-self.clock())
            if timeout <= 0:
                raise LiveRoutingError()
            uncertain = True
            body = self.transport(payload, timeout)
            billed = _money(str(body['usage']['cost']))
            total += billed
            uncertain = False
            if (billed > ceiling or total > cap or body.get('provider') != 'TypeSafe'
                    or body.get('model') not in quote.actual_models
                    or self.clock() >= min(grant.expires_at, quote.expires_at)):
                raise LiveRoutingError()
            from host_contracts import RoutingUsage, Identifier
            from pydantic import TypeAdapter
            usage = RoutingUsage(input_tokens=body['usage']['input_tokens'], output_tokens=body['usage']['output_tokens'])
            identifier = TypeAdapter(Identifier).validate_python(body['id'], strict=True)
            screen_outbound(body)
            _validate(wire, body)
            if set(body['answers']) != set(wire):
                raise LiveRoutingError()
            answers = {}
            for qid, answer in body['answers'].items():
                kind = answer['type']
                value = answer[{'noul':'noul','choice':'choice','score':'score'}[kind]]
                answers[qid] = Answer(kind, value, answer.get('confidence'), answer.get('probabilities'))
            receipts.append({'decision_id':identifier, 'model':body['model'], 'cost_usd':str(billed),
                             'usage':usage.model_dump(), 'payload_sha256':payload_fingerprint(payload),
                             'pricing_evidence':quote.pricing_evidence})
            return Decision(answers, body['model'], 'openrouter', usage.input_tokens, usage.output_tokens)
        try:
            result = invoke(decide)
            if (self.clock() >= grant.expires_at or (self.authorize_operation is not None
                    and self.authorize_operation(tool, arguments) != grant)):
                raise LiveRoutingError()
            screen_outbound(result)
            # Persist only screened tool output, never raw requests/source/credentials.
            record = self.ledger.finish(key, {'status':'completed','cost_usd':str(total),
                'result':result,'decisions':receipts,'expires_at':grant.expires_at}, billed=str(total))
            if record['status'] != 'completed':
                raise LiveRoutingError()
            return result
        except Exception:
            self.ledger.finish(key, {'status':'unavailable','decisions':receipts},
                billed=None if uncertain else str(total), halt='unified_decision_unavailable')
            raise LiveRoutingError() from None


@dataclass
class UnifiedOperator:
    """Trusted Python binding, never supplied by MCP tool arguments."""
    ledger: PilotLedger
    transport: object
    runtime_id: str
    session_id: str
    discover: object
    authorize_task: object
    quote_route: object
    authorize_operation: object
    quote_decision: object
    workspace_roots: tuple[Path, ...]
    clock: object = time.time


def create_server(operator: UnifiedOperator):
    if type(operator) is not UnifiedOperator:
        raise LiveRoutingError()
    host = BoundHostRouting(runtime_id=operator.runtime_id, session_id=operator.session_id,
        discover=operator.discover, authorize=operator.authorize_task, clock=operator.clock,
        selector=OpenRouterSelector(transport=operator.transport, ledger=operator.ledger,
                                    authorize=operator.quote_route, clock=operator.clock).binding())
    decisions = AccountedDecisions(ledger=operator.ledger, transport=operator.transport,
                                  quote=operator.quote_decision, clock=operator.clock,
                                  authorize_operation=operator.authorize_operation)
    server = SafeMCPServer('jev', instructions='Recommend, gate and select context only; never execute. A recommendation is not permission.')

    @server.tool()
    def jev_recommend_host_route(task: TaskEnvelope) -> dict:
        """Recommend with trusted host discovery and a billed JEV receipt. Never dispatch."""
        return host.recommend(task).model_dump(mode='json')

    @server.tool()
    def jev_gate_command(command: str, cwd: str = '.') -> dict:
        """Return command policy/advice only; never run a command or grant permission."""
        arguments = {'command':command,'cwd':cwd}
        screen_outbound(arguments)
        grant = operator.authorize_operation('jev_gate_command', arguments)
        if type(grant) is not OperationGrant:
            raise LiveRoutingError()
        grant.validate(arguments, operator.clock())
        kind, reason = triage(command)
        if kind != 'external':
            return {'decision':'block' if kind == 'block' else 'allow','reason':reason,
                    'binaries':extract_commands(command),'source':'policy'}
        def invoke(decide):
            result = gate(command, cwd, decide_fn=decide, trace_fn=lambda *a,**k:None,
                          operator_allow_fn=lambda *a,**k:None)
            return {'decision':result['final'],'reason':result.get('reason',''),
                    'binaries':result.get('binaries',[]),'source':'model',
                    'block_probability':(result.get('probabilities') or {}).get('block'),
                    'impact':result.get('impact')}
        return decisions.run('jev_gate_command', arguments, grant, invoke)

    @server.tool()
    def jev_select_context(goal: str, root: str, globs: list[str] | None = None,
                           budget_tokens: Annotated[int, Field(ge=1000,le=500000)] = 40000) -> dict:
        """Read approved local files; send only screened paths/symbols and goal for ranking."""
        arguments = {'goal':goal,'root':root,'globs':globs,'budget_tokens':budget_tokens}
        screen_outbound(arguments)
        grant = operator.authorize_operation('jev_select_context', arguments)
        if type(grant) is not OperationGrant:
            raise LiveRoutingError()
        grant.validate(arguments, operator.clock())
        # Exact operator roots keep the boundary small; nested roots need approval.
        if Path(root) not in operator.workspace_roots:
            raise WorkspaceError('approved root required')
        workspace = ApprovedWorkspace(root, max_bytes=400000)
        paths = workspace.gather(globs, suffixes={'.py','.ts','.tsx','.js','.jsx','.go','.rs','.swift','.md','.json','.yaml','.yml','.toml'},
                                 skip_dirs={'.git','node_modules','.venv','venv','__pycache__','dist','build'})
        chunks = [Chunk('goal',goal,kind='request')]
        names = {}
        total_bytes = 0
        for index,path in enumerate(paths):
            relative = str(path.relative_to(workspace.root))
            try:
                source = workspace.read_text(relative)
            except WorkspaceError:
                continue
            total_bytes += len(source.encode('utf-8'))
            if total_bytes > MAX_CONTEXT_BYTES:
                raise WorkspaceError('scan limit exceeded')
            cid = 'f'+str(index)
            names[cid] = relative
            chunks.append(Chunk(cid,source,path=relative))
        if not names:
            raise LiveRoutingError()
        content_hash = payload_fingerprint([{'id':c.id,'path':c.path,'text':c.text} for c in chunks])
        def invoke(decide):
            packed = select(goal,chunks,budget=budget_tokens,strict_refinement=True,
                decide_batch_fn=lambda state,questions:decide_batched(state,questions,decide_fn=decide),
                trace_fn=lambda *a,**k:None)
            return {'include':[names[c.id] for c in packed.included if c.id in names],
                'index':packed.indexed,'exclude_count':len(packed.excluded),
                'scores':{names[k]:round(v,3) for k,v in packed.scores.items() if k in names},
                'tokens_before':packed.tokens_in,'tokens_after':packed.tokens_out,
                'saved_pct':100-round(100*packed.tokens_out/max(1,packed.tokens_in)), 'canary':packed.canary}
        return decisions.run('jev_select_context',arguments,grant,invoke,content_fingerprint=content_hash)
    return server
