"""Real OpenRouter adapters for the separately authorized synthetic LivePilot.

These are low-level trusted dependencies, not public MCP tools or an auth server.
LivePilot owns JEV selection, consent, canonical-ledger reservations and replay
protection. Never expose send() directly or inject these into mock WritingService.
No default binding, environment reader, paid startup, or automatic activation.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from decimal import Decimal
from urllib.parse import urlsplit

from live_pilot import ALIASES, ENDPOINT, Approval, PilotError, Reply, Target, Usage, _money
from live_routing import _ForcedProxy, _NoRedirects
from privacy import screen_outbound

_MODEL = re.compile(r'^anthropic/claude-(?:sonnet|opus)-[A-Za-z0-9.-]+$')


def _proxy(proxy):
    parsed = urlsplit(proxy)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise PilotError('invalid_catalog_or_privacy')
    return proxy


def _opener(proxy, url):
    return urllib.request.build_opener(_ForcedProxy({'https': _proxy(proxy)}, allowed_urls={url}), _NoRedirects())


class OpenRouterWritingTransport:
    """Single HTTPS POST, no retries, exact model/provider and bounded response.

    Caller must reserve routing-plus-generation cost in the canonical ledger
    before entering this transport, as LivePilot.run_one does. Errors retain an
    uncertain-charge reservation; HTTP failures never imply a free retry.
    """
    evidence_mode = 'live'

    def __init__(self, *, issued_placeholder, https_proxy, allowed_models, allowed_providers):
        self._placeholder = issued_placeholder
        self._models = frozenset(allowed_models)
        self._providers = frozenset(allowed_providers)
        if not self._models or any(not _MODEL.fullmatch(m) for m in self._models) or not self._providers:
            raise PilotError('invalid_catalog_or_privacy')
        self._opener = _opener(https_proxy, ENDPOINT)

    def send(self, *, endpoint, payload, timeout_seconds, operation_id):
        try:
            if endpoint != ENDPOINT or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
                raise ValueError
            if set(payload) - {'model', 'messages', 'max_tokens', 'stream', 'provider'}:
                raise ValueError
            provider = payload['provider']
            if (payload['model'] not in self._models or payload['stream'] is not False
                    or type(payload['max_tokens']) is not int or not 1 <= payload['max_tokens'] <= 512
                    or provider.get('allow_fallbacks') is not False
                    or provider.get('require_parameters') is not True
                    or provider.get('data_collection') != 'deny' or provider.get('zdr') is not True
                    or len(provider.get('only', [])) != 1 or provider['only'][0] not in self._providers
                    or provider.get('order') != provider['only']
                    or set(provider) - {'only','order','allow_fallbacks','require_parameters','data_collection','zdr','max_price'}):
                raise ValueError
            for price in ('prompt','completion','request'):
                _money(provider['max_price'][price])
            messages = payload['messages']
            if (not isinstance(messages, list) or not 1 <= len(messages) <= 2
                    or any(set(m) != {'role','content'} or m['role'] not in {'system','user'}
                           or not isinstance(m['content'], str) for m in messages)):
                raise ValueError
            screen_outbound(payload)
            encoded = json.dumps(payload, allow_nan=False).encode()
            if len(encoded) > 32768:
                raise ValueError
            request = urllib.request.Request(ENDPOINT, data=encoded, headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._placeholder()})
            with self._opener.open(request, timeout=timeout_seconds) as response:
                raw = response.read(131073)
                if len(raw) > 131072:
                    raise ValueError
                body = json.loads(raw)
            if body['model'] != payload['model'] or body['provider'] != provider['only'][0]:
                raise ValueError
            choice = body['choices'][0]
            if len(body['choices']) != 1 or choice['finish_reason'] not in {'stop','length','refusal'}:
                raise ValueError
            text = choice['message']['content']
            if not isinstance(text, str) or len(text.encode()) > 65536:
                raise ValueError
            screen_outbound(text)
            usage = body['usage']
            details = usage.get('prompt_tokens_details') or {}
            completion = usage.get('completion_tokens_details') or {}
            normalized = Usage(usage['prompt_tokens'], usage['completion_tokens'],
                details.get('cached_tokens', 0), details.get('cache_write_tokens', 0),
                completion.get('reasoning_tokens', 0))
            normalized.validate_limits(1_000_000, payload['max_tokens'])
            billed = str(_money(str(usage['cost'])))
            if not isinstance(body['id'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', body['id']):
                raise ValueError
            return Reply(body['id'],body['model'],body['provider'],normalized,billed,choice['finish_reason'],text)
        except Exception:
            raise PilotError('invalid_output') from None


class OpenRouterWritingBinding:
    """Bind existing proxy credentials only after LivePilot verifies approval."""
    def __init__(self, *, issued_placeholder, https_proxy, allowed_models, clock=time.time):
        self._placeholder, self._proxy = issued_placeholder, https_proxy
        self._models, self.clock = frozenset(allowed_models), clock

    def bind_preexisting(self, approval: Approval):
        approval.validate(self.clock())
        return OpenRouterWritingTransport(issued_placeholder=self._placeholder, https_proxy=self._proxy,
            allowed_models=self._models, allowed_providers=approval.allowed_providers)


class OpenRouterWritingCatalog:
    """Refresh exact approved targets; never infer latest from display names.

    Alias mappings, provider privacy and cache price bounds come from verified
    host configuration. Missing endpoint fields or policy evidence fail closed.
    Using the entire input context gives a conservative bound without guessing
    tokenization. The budget may reject this bound; do not silently shrink it.
    """
    def __init__(self, *, https_proxy, alias_targets, provider, privacy_check,
                 cache_read_price, cache_write_price, request_price, clock=time.time):
        self.proxy, self.alias_targets, self.provider = _proxy(https_proxy), dict(alias_targets), provider
        self.privacy_check, self.clock = privacy_check, clock
        self.cache_read_price, self.cache_write_price = _money(cache_read_price), _money(cache_write_price)
        self.request_price = _money(request_price)

    def resolve(self, model, *, alias, prompt, max_output_tokens):
        try:
            if (alias not in ALIASES.values() or self.alias_targets.get(alias) != model
                    or not _MODEL.fullmatch(model) or type(max_output_tokens) is not int
                    or not 1 <= max_output_tokens <= 512):
                raise ValueError
            family = 'sonnet' if alias == ALIASES['routine'] else 'opus'
            if not model.startswith('anthropic/claude-' + family + '-'):
                raise ValueError
            url = 'https://openrouter.ai/api/v1/models/' + model + '/endpoints'
            with _opener(self.proxy, url).open(url, timeout=20) as response:
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise ValueError
                metadata = json.loads(raw)['data']
            if metadata['id'] != model:
                raise ValueError
            endpoint = next(e for e in metadata['endpoints'] if e['provider_name'] == self.provider and e['status'] == 0)
            if self.privacy_check(model, self.provider, endpoint) is not True:
                raise ValueError
            context, output_limit = endpoint['context_length'], endpoint['max_completion_tokens']
            if (type(context) is not int or type(output_limit) is not int
                    or context <= max_output_tokens or output_limit < max_output_tokens):
                raise ValueError
            pricing = endpoint['pricing']
            inp, out = _money(pricing['prompt']), _money(pricing['completion'])
            # Missing request-fee metadata uses the explicit operator-verified bound;
            # absence alone is not assumed to mean a zero fee.
            request_price = max(self.request_price, _money(pricing.get('request', '0')))
            read = max(self.cache_read_price, _money(pricing.get('input_cache_read', '0')))
            write = max(self.cache_write_price, _money(pricing.get('input_cache_write', '0')))
            now = self.clock()
            return Target(alias, model, self.provider, 'openrouter-endpoints-live', now, now+60,
                str(inp),str(out),str(read),str(write),str(request_price), output_limit,context,
                context-max_output_tokens, True,True,True,True,family)
        except Exception:
            raise PilotError('invalid_catalog_or_privacy') from None
