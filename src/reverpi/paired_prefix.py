"""RC7 common eligible-prefix experiment primitives.

This is a controlled read-only fixture experiment, not arbitrary OS snapshots.
Only complete exact prior replies can be replayed. The central ledger charges
shared capture once and both suffixes; virtual prefix replay never calls it.
Neither the tape nor a hash is an adversarial-owner security credential.
"""
from __future__ import annotations

import copy
import hashlib
import os
import zlib
from pathlib import Path
from dataclasses import asdict
from typing import Literal

import httpx
from pydantic import Field, model_validator

from .config import StrictModel, Provider, Budget, OnlineProjectionConfig
from .errors import LabError
from .ledger import Ledger
from .protocols import Completion, Message, build_request
from .transport import APIClient
from .util import canonical, digest, atomic_write, strict_json_loads, seal_cache, unseal_cache, source_manifest


class PairedPlan(StrictModel):
    schema_version: Literal['rc7.paired-prefix.v1'] = 'rc7.paired-prefix.v1'
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    provider: Provider
    budget: Budget
    policy: OnlineProjectionConfig = Field(default_factory=lambda: OnlineProjectionConfig(mode='observe'))
    pressure: Literal['short', 'long'] = 'long'
    seed: str = Field(min_length=8, max_length=100)
    branch_order: list[Literal['full', 'projected']] = Field(default_factory=lambda: ['full', 'projected'])
    max_prefix_requests: int = Field(6, ge=1, le=30)
    max_suffix_requests: int = Field(6, ge=1, le=30)
    wall_seconds_per_phase: int = Field(180, ge=30, le=7200)
    paid_authorized: bool = False
    scope: Literal['owned_readonly_conditional_effect_not_benchmark'] = 'owned_readonly_conditional_effect_not_benchmark'

    @model_validator(mode='after')
    def check_plan(self):
        p = self.provider
        if self.branch_order not in (['full', 'projected'], ['projected', 'full']):
            raise ValueError('Exactly two unique predeclared arms are required')
        if self.policy.mode != 'observe':
            raise ValueError('Common prefix must be observed without replacing any content')
        if p.effort != 'low' or p.concurrency != 1 or p.retry.max_attempts != 1 or p.retry.retry_ambiguous:
            raise ValueError('Only low/concurrency1/single-attempt/no ambiguous retry is allowed')
        if p.max_output_tokens != 65536 or p.context_window != 131072:
            raise ValueError('RC7 keeps the inherited 131072/65536 research configuration')
        if p.stream or p.supports_native_compaction:
            raise ValueError('RC7 recorder requires nonstream complete JSON, no native compact endpoint')
        if not p.mock and p.model not in {'deepseek-flash', 'gpt-6-luna'}:
            raise ValueError('Select an explicit supported research fork')
        if p.mock and p.model != 'mock-reasoner':
            raise ValueError('Mock plan requires mock-reasoner')
        if p.extra_headers_env or p.ca_bundle_env:
            raise ValueError('RC7 bounded recorder profile does not implement custom TLS/header forwarding')
        return self


def paired_tools(plan: PairedPlan) -> list[str]:
    return (["read", "recover_evidence", "search_evidence"]
            if plan.policy.recovery_interface == "split_v1" else ["read", "recover_evidence"])


def require(value: bool, message: str, kind: str = 'paired_contract') -> None:
    if not value:
        raise LabError(kind, message)


def signature(body: dict) -> dict:
    """Ignore ONLY per-launch random gateway op UUID, not dates, paths or tools."""
    allowed = {'op', 'messages', 'tools', 'observation_meta', 'effort', 'max_output_tokens'}
    require(isinstance(body, dict) and set(body) == allowed, 'Canonical request fields differ')
    return copy.deepcopy({k: v for k, v in body.items() if k != 'op'})


def fixture(seed: str, pressure: str) -> tuple[dict[str, str], str, str]:
    """Five linked notes. Gold is not sent by the controller in any prompt."""
    names = ['note_' + hashlib.sha256(f'{seed}/name/{i}'.encode()).hexdigest()[:12] + '.txt' for i in range(5)]
    marker = 'RC7_' + hashlib.sha256(f'{seed}/answer'.encode()).hexdigest()[:32]
    n = 170 if pressure == 'long' else 8
    files = {}
    for i, name in enumerate(names):
        lines = [f'{i}:{j:03d}: ' + hashlib.sha256(f'{seed}/{i}/{j}'.encode()).hexdigest() + '\n' for j in range(n)]
        if i == 0:
            lines.insert(n // 2, 'historical_marker=' + marker + '\n')
        lines.append('next_note=' + (names[i + 1] if i + 1 < len(names) else 'END') + '\n')
        files[name] = ''.join(lines)
    prompt = (f'The entry note is {names[0]}. Follow its next_note references until END. '
              'Report only the historical_marker in the entry note. Use the notes as needed; '
              'do not invent a marker. This is a controlled read-only diagnostic, not a coding benchmark.')
    return files, prompt, marker


def workspace_manifest(path: Path) -> dict[str, str]:
    require(path.is_dir() and not path.is_symlink(), 'Missing regular workspace')
    result = {}
    for p in sorted(path.iterdir()):
        require(p.is_file() and not p.is_symlink(), 'Unexpected workspace entry')
        result[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def decode_recorded_body(data: bytes, encoding: str, maximum: int) -> bytes:
    """Bounded decoding for audit; hashes still bind original transfer bytes."""
    if encoding in {'', 'identity'}:
        require(len(data) <= maximum, 'Decoded response exceeds bound')
        return data
    require(encoding in {'gzip', 'deflate'}, 'Unsupported response Content-Encoding')
    try:
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == 'gzip' else zlib.MAX_WBITS)
        result = dec.decompress(data, maximum + 1)
    except zlib.error as err:
        raise LabError('response_encoding', 'Invalid compressed response') from err
    require(len(result) <= maximum and dec.eof and not dec.unused_data and not dec.unconsumed_tail,
            'Compressed response is too large, truncated or concatenated')
    return result


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, stream, destination: Path, maximum: int, finish):
        self.stream, self.destination, self.maximum, self.finish = stream, destination, maximum, finish
    async def __aiter__(self):
        count = 0
        h = hashlib.sha256()
        complete = False
        with self.destination.open('xb') as f:
            os.chmod(self.destination, 0o600)
            try:
                async for part in self.stream:
                    require(count + len(part) <= self.maximum, 'Response capture limit exceeded', 'response_capture_limit')
                    count += len(part)
                    f.write(part); f.flush(); h.update(part)
                    yield part
                complete = True
            finally:
                self.finish(count, h.hexdigest(), complete)
    async def aclose(self):
        await self.stream.aclose()


class RecordingTransport(httpx.AsyncBaseTransport):
    """Record body bytes, never authorization/request headers. No hidden retry."""
    def __init__(self, inner, folder: Path, provider: Provider):
        self.inner, self.folder, self.provider = inner, folder, provider
        folder.mkdir(mode=0o700)
        self.rows = []
    async def handle_async_request(self, request):
        index = len(self.rows)
        prefix = f'{index:03d}'
        request.headers['Accept-Encoding'] = 'identity'  # no content negotiation dependent on optional packages
        data = await request.aread()
        atomic_write(self.folder/f'{prefix}.request.bin', data)
        body = strict_json_loads(data)
        require(body.get('model') == self.provider.model, 'Outbound model mismatch')
        row = {'index': index, 'path': request.url.path, 'model': body['model'],
               'request_file': prefix+'.request.bin', 'request_sha256': hashlib.sha256(data).hexdigest(),
               'response_file': prefix+'.response.bin', 'state': 'dispatch_intent'}
        self.rows.append(row); self.persist()
        try:
            response = await self.inner.handle_async_request(request)
            row['content_encoding'] = response.headers.get('content-encoding', '').lower()
            require(row['content_encoding'] in {'', 'identity', 'gzip', 'deflate'}, 'Unsupported response Content-Encoding')
            row['status'] = response.status_code
            row['state'] = 'headers_received'; self.persist()
            def finish(count, sha, complete):
                row.update(response_bytes=count, response_sha256=sha, body_complete=complete,
                           state='body_complete' if complete else 'body_incomplete'); self.persist()
            wrapped = RecordingStream(response.stream, self.folder/row['response_file'], self.provider.max_response_bytes, finish)
            return httpx.Response(response.status_code, headers=response.headers, stream=wrapped, extensions=response.extensions)
        except BaseException as err:
            row.update(state='transport_exception', exception_type=type(err).__name__); self.persist(); raise
    def persist(self):
        atomic_write(self.folder/'index.json', canonical(self.rows))
    async def aclose(self):
        await self.inner.aclose()


class PairedBackend:
    """Trusted gateway callback; all real dispatches use ONE campaign ledger."""
    def __init__(self, *, root: Path, run: Path, phase: str, plan: PairedPlan,
                 workspace: Path, expected_workspace: dict, actor_transport=None):
        require(phase in {'capture', 'full', 'projected'}, 'Invalid phase')
        self.root, self.run, self.phase, self.plan = root, run, phase, plan
        self.folder = run/phase
        self.workspace, self.expected_workspace = workspace, expected_workspace
        self.events = []
        self.paid_count = 0
        self.replayed_count = 0
        self.tape = [] if phase == 'capture' else unseal_cache((run/'tape.json').read_text())['entries']
        self.boundary = None if phase == 'capture' else unseal_cache((run/'boundary.json').read_text())
        self.ledger = Ledger(run/'accounting.sqlite', plan.budget)
        self.ledger.bind('paired_plan', plan.model_dump())
        require((actor_transport is not None) is plan.provider.mock, 'Mock/live transport identity mismatch')
        inner = actor_transport if actor_transport is not None else httpx.AsyncHTTPTransport(retries=0)
        self.recorder = RecordingTransport(inner, self.folder/'wire', plan.provider)
        self.client = APIClient(plan.provider, self.ledger, transport=self.recorder)
        self.stopped_kind = None

    def save(self):
        atomic_write(self.folder/'events.json', canonical(self.events))
        if self.phase == 'capture':
            atomic_write(self.run/'tape.json', seal_cache({'schema': 1, 'entries': self.tape}))

    async def __call__(self, *, state, body, sent, prepared):
        index = len(self.events)
        original = body.model_dump()
        source = signature(original)
        row = {'index': index, 'gateway_op': body.op, 'source': source, 'source_sha': digest(source),
               'prepared': prepared, 'route': 'not_dispatched', 'state': 'intent'}
        self.events.append(row); self.save()
        try:
            require(workspace_manifest(self.workspace) == self.expected_workspace, 'Fixture changed before dispatch')
            require(digest(source_manifest(self.root)) == self.plan.source_sha256, 'Source changed before dispatch')
            require(prepared is not None, 'Projection evidence is required for both matched arms')
            expected_mode = 'apply' if self.phase == 'projected' else 'observe'
            require(prepared['mode'] == expected_mode, 'Phase projection mode mismatch')
            expected_tools = set(paired_tools(self.plan))
            require({t['name'] for t in body.tools} == expected_tools and len(body.tools) == len(expected_tools),
                    'Matched diagnostic tool surface differs from the frozen recovery interface')
            if self.phase == 'capture':
                if prepared['eligible_count']:
                    require(prepared['applied_count'] == 0, 'Common capture was already projected')
                    self.boundary = {'schema': 1, 'source': source, 'source_sha': digest(source),
                                     'prefix_length': len(self.tape), 'prepared': prepared,
                                     'workspace_sha': digest(self.expected_workspace)}
                    atomic_write(self.run/'boundary.json', seal_cache(self.boundary))
                    row.update(state='boundary', route='paused_before_claim')
                    raise LabError('paired_boundary', 'Eligible common request captured before any upstream claim')
                require(self.paid_count < self.plan.max_prefix_requests, 'Common prefix request cap', 'prefix_cap')
            elif index < len(self.tape):
                entry = self.tape[index]
                require(source == entry['source'], 'Prefix request mismatch; no live fallback', 'prefix_mismatch')
                require(prepared['applied_count'] == 0, 'Projection occurred before the frozen boundary')
                require([asdict(m) for m in sent] == entry['sent'], 'Prefix sent-message mismatch', 'prefix_mismatch')
                result = Completion(**copy.deepcopy(entry['response']))
                row.update(state='complete', route='prefix_replay', response=result.to_dict())
                self.replayed_count += 1
                return result
            else:
                if index == len(self.tape):
                    require(source == self.boundary['source'], 'Branch starting state mismatch', 'boundary_mismatch')
                    require(prepared['eligible_count'] > 0, 'First branch request is no longer eligible')
                    if self.phase == 'projected':
                        require(prepared['applied_count'] > 0, 'Projected branch did not project')
                    else:
                        require(prepared['applied_count'] == 0, 'Full branch was changed')
                require(self.paid_count < self.plan.max_suffix_requests, 'Suffix request cap', 'suffix_cap')
            op = f'rc7:{self.phase}:{self.paid_count:03d}'
            row.update(route='central_dispatch', central_op=op, wire_index=len(self.recorder.rows))
            self.save()
            result = await self.client.complete(sent, tools=body.tools, effort=body.effort,
                                                max_output_tokens=body.max_output_tokens, op=op, cell=self.phase)
            self.paid_count += 1
            row.update(state='complete', response=result.to_dict())
            if self.phase == 'capture':
                self.tape.append({'source': source, 'sent': [asdict(m) for m in sent],
                                  'response': result.to_dict(), 'central_op': op})
            return result
        except BaseException as err:
            kind = err.kind if isinstance(err, LabError) else type(err).__name__
            self.stopped_kind = kind
            row.update(error_kind=kind)
            if kind != 'paired_boundary': row['state'] = 'failed'
            raise
        finally:
            self.save()

    async def aclose(self):
        await self.client.close()
