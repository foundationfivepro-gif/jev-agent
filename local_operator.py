"""Concrete private-JSON operator for local JEV. No .env or credential discovery.

Host-injected credentials are consumed only in the running server process. This
module never invokes a secret manager, provisions access, or creates a ledger.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import stat
import time
from typing import Literal
import urllib.request

from pydantic import Field, model_validator
from host_contracts import StrictContract, CapabilitySnapshot, Identifier, Number, TaskEnvelope
from live_pilot import Approval, _money
from live_routing import (ProtectedOpenRouterTransport, RoutingAuthorization,
                         LiveRoutingError, payload_fingerprint, _NoRedirects, MODEL)
from unified_mcp import (UnifiedOperator, OperationGrant, CredentialUnavailable,
                         open_existing_ledger)

APPROVAL_ID = 'approved-synthetic-cap-2026-10-01'
PRIOR_CHARGE = '0.046548454'


class OperatorConfigError(ValueError):
    def __init__(self):
        super().__init__('operator_config_unavailable: check private JSON schema, current catalog/prices/consent, existing ledger and approved paths')


class DirectInjectedOpenRouterTransport(ProtectedOpenRouterTransport):
    """Explicit local direct-HTTPS mode; consumes only the host-injected token.

    Not for cloud-issued proxy placeholders. No implicit environment proxy,
    credential lookup, alternate provider, redirect, or retry. Host network policy
    still applies; connection failures are never grounds to change transport.
    """
    def __init__(self, *, credential):
        self._placeholder = credential
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def __call__(self, payload, timeout):
        if payload.get('model') != MODEL:
            raise LiveRoutingError()
        return super().__call__(payload, timeout)


class Prices(StrictContract):
    observed_at: Number
    expires_at: Number
    evidence: Identifier
    actual_models: list[Identifier] = Field(min_length=1,max_length=4)
    context_tokens: int = Field(gt=0,le=32000)
    prompt_per_token: str
    completion_per_token: str
    cache_read_per_token: str
    cache_write_per_token: str
    request_fee: str
    other_fees_upper_usd: str

    @model_validator(mode='after')
    def valid(self):
        for field in ('prompt_per_token','completion_per_token','cache_read_per_token',
                      'cache_write_per_token','request_fee','other_fees_upper_usd'):
            _money(getattr(self,field))
        if (self.expires_at <= self.observed_at or len(set(self.actual_models)) != len(self.actual_models)
                or any(not m.startswith('typesafe/jev-') or m.endswith('latest') for m in self.actual_models)):
            raise ValueError('invalid pricing attestation')
        return self

    def quote(self, payload, now, expiry):
        if not self.observed_at <= now < self.expires_at:
            raise LiveRoutingError()
        # Operator verifies full context/output bound and *all* fees. Sum cache
        # and uncached rates conservatively rather than guessing cache behavior.
        token_rate=sum((_money(getattr(self,k)) for k in ('prompt_per_token','completion_per_token',
                       'cache_read_per_token','cache_write_per_token')), _money('0'))
        ceiling=self.context_tokens*token_rate+_money(self.request_fee)+_money(self.other_fees_upper_usd)
        if ceiling <= 0:
            raise LiveRoutingError()
        return RoutingAuthorization(payload_fingerprint(payload),str(ceiling),
                                    min(expiry,self.expires_at),frozenset(self.actual_models),self.evidence)


class TaskConsent(StrictContract):
    task_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expires_at: Number
    synthetic_attested: Literal[True]


class ToolConsent(StrictContract):
    tool: Literal['jev_gate_command','jev_select_context']
    arguments_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    operation_id: Identifier
    ceiling_usd: str
    expires_at: Number
    synthetic_attested: Literal[True]
    public_metadata_approved: Literal[True]

    @model_validator(mode='after')
    def valid(self):
        if not 0 < _money(self.ceiling_usd) <= _money('5'):
            raise ValueError('invalid ceiling')
        return self


class LocalOperatorConfig(StrictContract):
    schema_version: Literal['1.0'] = '1.0'
    transport: Literal['direct_env','protected_proxy']
    # Explicit proxy config is non-secret. Never copied from an arbitrary task.
    https_proxy: str | None = None
    ledger_path: str
    budget_scope: Literal['canonical_cloud','local_synthetic_smoke']
    approval_evidence: Identifier
    ledger_creation_authorized: bool = False
    ledger_approval_id: Identifier
    ledger_cap_usd: str
    minimum_charged_usd: str
    approval_expires_at: Number
    workspace_roots: list[str] = Field(min_length=1,max_length=16)
    snapshot: CapabilitySnapshot
    catalog_evidence: Identifier
    prices: Prices
    tasks: list[TaskConsent] = Field(max_length=64)
    operations: list[ToolConsent] = Field(max_length=64)

    @model_validator(mode='after')
    def valid(self):
        floor=_money(self.minimum_charged_usd)
        cap=_money(self.ledger_cap_usd)
        if self.budget_scope=='canonical_cloud':
            if (self.ledger_approval_id!=APPROVAL_ID or cap!=_money('5')
                    or floor<_money(PRIOR_CHARGE) or self.ledger_creation_authorized):
                raise ValueError('canonical approval cannot be reset or recreated')
        elif (self.ledger_approval_id==APPROVAL_ID or not 0<cap<=_money('.10') or floor>cap):
            raise ValueError('separate explicit local smoke approval required')
        for value in [self.ledger_path,*self.workspace_roots]:
            path=Path(value)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('absolute operator paths required')
        if (self.transport=='protected_proxy') != (self.https_proxy is not None):
            raise ValueError('explicit transport/proxy mismatch')
        if len(set(self.workspace_roots)) != len(self.workspace_roots):
            raise ValueError('duplicate roots')
        task_ids=[t.task_sha256 for t in self.tasks]
        op_ids=[(o.tool,o.arguments_sha256) for o in self.operations]
        if len(set(task_ids))!=len(task_ids) or len(set(op_ids))!=len(op_ids) or len({o.operation_id for o in self.operations})!=len(op_ids):
            raise ValueError('duplicate consent')
        return self

    def check_current(self, now):
        if (not now < self.approval_expires_at <= now+300
                or not self.snapshot.observed_at <= now < self.snapshot.expires_at <= self.approval_expires_at
                or now-self.snapshot.observed_at>300
                or self.snapshot.evidence_status!='runtime_observed' or not self.snapshot.available
                or not self.prices.observed_at <= now < self.prices.expires_at <= self.approval_expires_at
                or now-self.prices.observed_at>300):
            raise OperatorConfigError()


def read_config(path, *, clock=time.time):
    try:
        path=Path(path)
        if not path.is_absolute():
            raise ValueError()
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'r') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
                raise ValueError()
            raw=stream.read(131073)
        if len(raw)>131072:
            raise ValueError()
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError()
                result[key]=value
            return result
        config=LocalOperatorConfig.model_validate(json.loads(raw,object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
        config.check_current(clock())
        return config
    except Exception:
        raise OperatorConfigError() from None


def normalize_arguments(tool, arguments):
    from pydantic import BaseModel, ConfigDict
    class Gate(BaseModel):
        model_config=ConfigDict(strict=True,extra='forbid')
        command: str
        cwd: str='.'
    class Context(BaseModel):
        model_config=ConfigDict(strict=True,extra='forbid')
        goal: str
        root: str
        globs: list[str] | None=None
        budget_tokens: int=Field(default=40000,ge=1000,le=500000)
    model={'jev_recommend_host_route':TaskEnvelope,'jev_gate_command':Gate,'jev_select_context':Context}[tool]
    return model.model_validate(arguments).model_dump(mode='json')


def build_operator(config_path, *, credential_provider=None, transport_factory=None, clock=time.time):
    """No unfinished callbacks: all grants, catalog and pricing come from private JSON.

    Tests may inject fake provider/transport. Production uses precisely one
    host-injected environment name selected by explicit transport mode.
    """
    initial=read_config(config_path,clock=clock)
    invariant=lambda c:(c.transport,c.https_proxy,c.ledger_path,c.ledger_approval_id,c.ledger_cap_usd,c.budget_scope,
                        tuple(c.workspace_roots),c.snapshot.runtime_id,c.snapshot.session_id)
    def current():
        config=read_config(config_path,clock=clock)
        if invariant(config)!=invariant(initial):
            raise OperatorConfigError()
        return config
    if credential_provider is None:
        env_name='JEV_OPENROUTER_TOKEN' if initial.transport=='direct_env' else 'OPENROUTER_API'
        def credential_provider():
            value=os.environ.get(env_name)
            if not value or '\r' in value or '\n' in value:
                raise CredentialUnavailable()
            return value
    raw_provider=credential_provider
    def checked_credential():
        try:
            value=raw_provider()
            if not isinstance(value,str) or not value or '\r' in value or '\n' in value:
                raise ValueError()
            if initial.transport=='direct_env':
                import re
                if not re.fullmatch(r'sk-or-v1-[A-Za-z0-9_-]{32,4096}',value) or any(
                        marker in value.lower() for marker in ('placeholder','proxy','op://')):
                    raise ValueError()
            return value
        except Exception:
            raise CredentialUnavailable() from None
    try:
        # Only in the running server, never --schema/--check-config. No value is
        # emitted/persisted; revalidate the injection at each actual request.
        checked_credential()
        if transport_factory is not None:
            transport=transport_factory(checked_credential,initial)
        elif initial.transport=='direct_env':
            transport=DirectInjectedOpenRouterTransport(credential=checked_credential)
        else:
            transport=ProtectedOpenRouterTransport(issued_placeholder=checked_credential,https_proxy=initial.https_proxy)
    except Exception:
        raise CredentialUnavailable() from None
    approval=Approval(initial.ledger_approval_id,initial.ledger_cap_usd,frozenset({'TypeSafe'}),initial.approval_expires_at,jev_routing_approved=True)
    ledger=open_existing_ledger(initial.ledger_path,approval,minimum_charged_usd=initial.minimum_charged_usd)
    def task_consent(task):
        config=current()
        digest=payload_fingerprint(task.model_dump(mode='json'))
        consent=next((c for c in config.tasks if c.task_sha256==digest),None)
        if (consent is None or not clock()<task.deadline<=consent.expires_at<=config.approval_expires_at
                or task.data_class!='public' or task.authorized_destinations!=['openrouter.ai']
                or task.context_references or task.budget_usd is None or task.budget_usd<=0):
            raise LiveRoutingError()
        return config,consent
    def authorize_task(task):
        task_consent(task)
        return True
    def quote_route(task,payload):
        config,consent=task_consent(task)
        return config.prices.quote(payload,clock(),min(task.deadline,consent.expires_at))
    def operation_consent(tool,args):
        config=current()
        digest=payload_fingerprint(normalize_arguments(tool,args))
        consent=next((c for c in config.operations if c.tool==tool and c.arguments_sha256==digest),None)
        if consent is None or not clock()<consent.expires_at<=config.approval_expires_at:
            raise LiveRoutingError()
        return config,consent
    def authorize_operation(tool,args):
        config,consent=operation_consent(tool,args)
        return OperationGrant(consent.operation_id,consent.arguments_sha256,consent.ceiling_usd,consent.expires_at)
    def quote_decision(tool,args,payload):
        config,consent=operation_consent(tool,args)
        return config.prices.quote(payload,clock(),consent.expires_at)
    return UnifiedOperator(ledger,transport,initial.snapshot.runtime_id,initial.snapshot.session_id,
        lambda:current().snapshot,authorize_task,quote_route,authorize_operation,quote_decision,
        tuple(Path(p) for p in initial.workspace_roots),clock)


def initialize_smoke_ledger(config_path, *, clock=time.time):
    """Explicit setup only after separate approval; never overwrite/copy a ledger."""
    from live_pilot import PilotLedger
    config=read_config(config_path,clock=clock)
    if (config.budget_scope!='local_synthetic_smoke' or not config.ledger_creation_authorized
            or _money(config.minimum_charged_usd)!=0):
        raise OperatorConfigError()
    fd=os.open(config.ledger_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    os.close(fd)
    approval=Approval(config.ledger_approval_id,config.ledger_cap_usd,frozenset({'TypeSafe'}),
                      config.approval_expires_at,jev_routing_approved=True)
    PilotLedger(Path(config.ledger_path),approval)
    return {'status':'local_smoke_ledger_initialized','approval_id':config.ledger_approval_id,
            'cap_usd':config.ledger_cap_usd,'model_calls':0}
