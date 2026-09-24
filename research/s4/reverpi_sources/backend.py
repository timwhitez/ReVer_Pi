"""S4 adapter of frozen RC7.1 paired backend, generalized only for recursive snapshots.

Upstream in this repository: src/reverpi/paired_prefix.py. Keeps the central
ledger and request-replay logic; production implementation remains unchanged.
"""
from __future__ import annotations
import copy
from pathlib import Path
from dataclasses import asdict
import httpx
from reverpi.paired_prefix import RecordingTransport, signature, paired_tools, require
from reverpi.protocols import Completion
from reverpi.ledger import Ledger
from reverpi.transport import APIClient
from reverpi.errors import LabError
from reverpi.util import digest,canonical,source_manifest,atomic_write,seal_cache,unseal_cache
from .contracts import SourcePlan,tree_manifest,research_manifest

class SourceBackend:
    """Trusted gateway callback; all real dispatches use ONE campaign ledger."""
    def __init__(self, *, root: Path, run: Path, phase: str, plan: SourcePlan,
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
            require(tree_manifest(self.workspace) == self.expected_workspace, 'Fixture changed before dispatch')
            require(digest(source_manifest(self.root)) == self.plan.source_sha256, 'Source changed before dispatch')
            require(digest(research_manifest()) == self.plan.research_sha256, 'Research adapter changed before dispatch')
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
