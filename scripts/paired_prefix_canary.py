#!/usr/bin/env python3
"""Prepare/run RC7 common eligible-prefix read-only diagnostic.

No model invocation by default. Live operation requires an independently hashed,
explicitly authorized plan, --allow-paid, credentials in the host environment,
and an acknowledgement of the local non-sandbox threat model. No health probes,
no automatic resume/retry, no benchmark selection, no silent model substitution.
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
import httpx
from reverpi.acceptance import RPC
from reverpi.config import Budget, Provider, StudyConfig
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.launcher import pi_paths, command, isolated_env, settings
from reverpi.ledger import Ledger
from reverpi.paired_prefix import PairedPlan, PairedBackend, fixture, require, workspace_manifest, paired_tools
from reverpi.preflight import validate_provider_environment
from reverpi.processes import stop_process_group
from reverpi.util import atomic_write, canonical, digest, source_manifest, strict_json_loads
from native_revalidation_smoke import server
from native_compaction_smoke import wire_view, reply



@contextlib.contextmanager
def paired_server(app):
    """Revoke permission before the server waits for in-flight work to exit.

    Revocation prevents new calls; it is not a supplier billing cancellation.
    The outer phase finalizer retains process cleanup and another idempotent
    revoke, including failures during gateway startup.
    """
    with server(app) as url:
        try:
            yield url
        finally:
            app.state.sessions.disable('paired')


def new_destination(path: Path):
    require(not path.is_symlink(), 'Output must not be a symlink')
    path = path.absolute()
    for p in path.parents:
        require(not p.is_symlink(), 'Output ancestors cannot be symlinks')
    path = path.resolve()
    require(not path.exists() and not path.is_relative_to(ROOT) and not ROOT.is_relative_to(path),
            'Use a new output outside the source tree; existing runs are not replayed')
    return path


class ScriptedActor:
    """State-based interface test actor; never a reasoning or quality benchmark."""
    def __init__(self, provider: Provider, files: dict[str, str], marker: str, recovery_interface="legacy"):
        self.p, self.names, self.marker = provider, list(files), marker
        self.recovery_interface = recovery_interface
    def __call__(self, request: httpx.Request):
        require(request.url.host == '127.0.0.1', 'Mock upstream must be loopback')
        body = strict_json_loads(request.content)
        require(body['model'] == 'mock-reasoner', 'Mock actor received a live model name')
        rows, names = wire_view(body, self.p.protocol)
        expected = {'read', 'recover_evidence'} | ({'search_evidence'} if self.recovery_interface == 'split_v1' else set())
        require(set(names) == expected, 'Unexpected diagnostic tool surface')
        if self.p.protocol == 'chat_completions':
            calls = [dict(id=c['id'], name=c['function']['name'], arguments=strict_json_loads(c['function']['arguments']))
                     for m in rows for c in m.get('tool_calls', [])]
            results = {m['tool_call_id']: m['content'] for m in rows if m.get('role') == 'tool'}
        else:
            calls = [dict(id=m['call_id'], name=m['name'], arguments=strict_json_loads(m['arguments']))
                     for m in rows if m.get('type') == 'function_call']
            results = {m['call_id']: m['output'] for m in rows if m.get('type') == 'function_call_output'}
        reads = [c for c in calls if c['name'] == 'read']
        recovered = [c for c in calls if c['name'] in {'recover_evidence', 'search_evidence'}]
        call = None
        if len(reads) < len(self.names):
            i = len(reads)
            call = {'id': f'rc7_read_{i}', 'name': 'read', 'arguments': {'path': self.names[i]}}
        elif self.marker not in canonical(rows) and not recovered:
            call = {'id': 'rc7_search', 'name': 'search_evidence' if self.recovery_interface == 'split_v1' else 'recover_evidence', 'arguments': {'query': 'historical_marker=', 'chars': 1000}}
        elif recovered and 'query' in recovered[-1]['arguments']:
            value = strict_json_loads(results[recovered[-1]['id']])
            match = next((x for x in value.get('matches', []) if self.marker in x.get('excerpt', '')), None)
            require(match is not None, 'Scripted archive search did not contain the expected span')
            start = match['match_start'] + len('historical_marker=')
            call = {'id': 'rc7_exact', 'name': 'recover_evidence', 'arguments': {'handle': match['handle'], 'start': start, 'chars': len(self.marker)}}
        elif recovered:
            value = strict_json_loads(results[recovered[-1]['id']])
            require(value.get('text') == self.marker, 'Exact scripted recovery mismatch')
        else:
            require(self.marker in canonical(rows), 'Scripted actor may not answer an invisible marker')
        return httpx.Response(200, json=reply(self.p.protocol, len(calls), call, self.marker if call is None else ''))


async def phase_run(out: Path, phase: str, plan: PairedPlan, files: dict, prompt: str, marker: str, identities: dict):
    folder = out/phase; folder.mkdir(mode=0o700)
    for name in ('home', 'agent-config'):
        (folder/name).mkdir(mode=0o700)
    st = settings(plan.provider.effort, plan.provider.max_output_tokens, plan.provider.context_window)
    atomic_write(folder/'agent-config/settings.json', canonical(st))
    actor = ScriptedActor(plan.provider, files, marker, plan.policy.recovery_interface) if plan.provider.mock else None
    backend = PairedBackend(root=ROOT, run=out, phase=phase, plan=plan, workspace=out/'workspace',
                            expected_workspace=identities, actor_transport=httpx.MockTransport(actor) if actor else None)
    mode = 'apply' if phase == 'projected' else 'observe'
    cfg = StudyConfig(name='rc7-readonly-paired', methods=['mask'], budget=plan.budget,
                      online_projection=plan.policy.model_copy(update={'mode': mode}),
                      compression={'recovery_search_mode': 'match'}, early_response_headers=True, response_heartbeat_seconds=.1)
    app = create_app(plan.provider, cfg, folder/'gateway', completion_backend=backend)
    token = app.state.sessions.create('paired', 'mask')
    atomic_write(folder/'study.json', canonical(cfg.model_dump()))
    node, cli, nodever, piver = pi_paths(ROOT)
    row = {'phase': phase, 'state': 'running', 'node': nodever, 'pi': piver, 'model': plan.provider.model,
           'mock': plan.provider.mock, 'manual_compact_rpc': False, 'tools': paired_tools(plan)}
    atomic_write(folder/'phase.json', canonical(row))
    proc = None; drain_task = None
    try:
        with paired_server(app) as url:
            argv = command(node, cli, ROOT/'pi/src/index.ts', folder/'session.jsonl', plan.provider.model, 'low', mode='rpc')
            argv += ['--tools', ','.join(paired_tools(plan)), '--extension', str(ROOT/'pi/src/paired-readonly.ts')]
            env = isolated_env(folder, url, token, 30, plan.max_prefix_requests+plan.max_suffix_requests+3)
            env.update(REVER_PAIRED_WORKSPACE=str(out/'workspace'), REVER_PAIRED_FILES=canonical(list(files)), PI_TELEMETRY='0')
            for k in ('LD_PRELOAD', 'REVER_OFFLINE_GUARD_LOG'):
                if k in os.environ: env[k] = os.environ[k]
            proc = await asyncio.create_subprocess_exec(*argv, cwd=out/'workspace', env=env,
                       stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                       start_new_session=True, limit=8*1024**2)
            with (folder/'rpc.jsonl').open('xb') as log, (folder/'stderr.log').open('xb') as errors:
                async def drain():
                    count = 0
                    while chunk := await proc.stderr.read(65536):
                        count += len(chunk); require(count <= 4*1024**2, 'Pi stderr limit')
                        errors.write(chunk); errors.flush()
                drain_task = asyncio.create_task(drain())
                rpc = RPC(proc, log, deadline=plan.wall_seconds_per_phase)
                await rpc.request('prompt', message=prompt)
                await stop_process_group(proc)
                await drain_task
            if phase == 'capture' and backend.stopped_kind == 'paired_boundary':
                row['state'] = 'boundary_captured'
            elif backend.stopped_kind:
                row.update(state='stopped', reason=backend.stopped_kind)
            else:
                assistants = []
                for line in (folder/'session.jsonl').read_text().splitlines():
                    entry = strict_json_loads(line)
                    m = entry.get('message', {})
                    if m.get('role') == 'assistant': assistants.append(m)
                require(assistants and assistants[-1].get('stopReason') not in {'error', 'aborted', 'length', 'toolUse'},
                        'No complete final assistant answer', 'agent_not_complete')
                answer = ''.join(x.get('text', '') for x in assistants[-1].get('content', []) if x.get('type') == 'text')
                row.update(state='completed', answer=answer, owned_marker_exact=answer.strip() == marker)
    except (Exception, asyncio.CancelledError) as err:
        if (phase == 'capture' and backend.stopped_kind == 'paired_boundary'
                and isinstance(err, LabError) and err.kind == 'pi_actor' and (out/'boundary.json').is_file()):
            row.update(state='boundary_captured', reason='intentional_preclaim_pause')
        else:
            row.update(state='stopped', reason=backend.stopped_kind or (err.kind if isinstance(err, LabError) else type(err).__name__))
        # Never hide cancellation/unknown outcome with an automatic new phase.
        if isinstance(err, asyncio.CancelledError): raise
    finally:
        app.state.sessions.disable('paired')
        await stop_process_group(proc)
        if drain_task is not None: await asyncio.gather(drain_task, return_exceptions=True)
        row.update(session_revoked=bool(app.state.sessions.get('paired')['disabled']),
                   experiment_dispatches=backend.ledger.totals(phase)['attempts'], completed_responses=backend.paid_count,
                   prefix_replays=backend.replayed_count,
                   central_cost=backend.ledger.totals(phase), gateway_cost=app.state.ledger.totals())
        row['fixture_unchanged'] = workspace_manifest(out/'workspace') == identities
        if not row['fixture_unchanged']: row.update(state='stopped', reason='workspace_changed')
        atomic_write(folder/'phase.json', canonical(row))
    return row


def preflight(plan: PairedPlan, *, allow_paid: bool, acknowledge_local: bool):
    require(plan.source_sha256 == digest(source_manifest(ROOT)), 'Plan is for another source tree')
    if not plan.provider.mock:
        require(allow_paid and plan.paid_authorized and acknowledge_local,
                'Live run requires explicit authorized plan, --allow-paid and --acknowledge-local-readonly')
        require(not os.environ.get('REVER_OFFLINE_VERIFICATION'), 'Live run forbidden in offline verification')
        validate_provider_environment(plan.provider)
    pi_paths(ROOT)


async def run(plan_path: Path, plan_sha: str, out: Path, allow_paid=False, acknowledge_local=False):
    data = strict_json_loads(plan_path.read_bytes())
    require(digest(data) == plan_sha, 'Independent plan SHA does not match')
    plan = PairedPlan.model_validate(data)
    preflight(plan, allow_paid=allow_paid, acknowledge_local=acknowledge_local)
    out = new_destination(out); out.mkdir(parents=True, mode=0o700)
    atomic_write(out/'PLAN.json', canonical(data))
    files, prompt, marker = fixture(plan.seed, plan.pressure)
    (out/'workspace').mkdir(mode=0o700)
    for name, content in files.items():
        atomic_write(out/'workspace'/name, content, mode=0o444)
    identities = workspace_manifest(out/'workspace')
    atomic_write(out/'FIXTURE.json', canonical({'files': identities, 'prompt': prompt, 'marker': marker,
                                                'gold_controller_only': True, 'not_a_general_snapshot': True}))
    summary = {'schema': 1, 'release': 'RC7', 'status': 'running', 'plan_sha': plan_sha,
               'source_sha256': plan.source_sha256, 'provider_mock': plan.provider.mock,
               'real_model_calls': 0 if plan.provider.mock else None,
               'paired_effect_identified': False, 'quality_or_superiority_established': False,
               'scripted_actor_only': plan.provider.mock, 'natural_coverage_population': 'one_owned_fixture',
               'read_only_replay_not_os_snapshot': True, 'phases': []}
    atomic_write(out/'summary.json', canonical(summary))
    try:
        capture = await phase_run(out, 'capture', plan, files, prompt, marker, identities)
        summary['phases'].append(capture)
        if capture['state'] != 'boundary_captured':
            summary['status'] = 'no_eligible_prefix' if capture['state'] == 'completed' else 'stopped'
        else:
            for phase in plan.branch_order:
                row = await phase_run(out, phase, plan, files, prompt, marker, identities)
                summary['phases'].append(row)
                atomic_write(out/'summary.json', canonical(summary))
                if row['state'] != 'completed':
                    summary['status'] = 'stopped'; break
            else:
                summary['status'] = 'paired_complete'
    except (Exception, asyncio.CancelledError) as err:
        summary.update(status='stopped', error_type=type(err).__name__)
        if isinstance(err, asyncio.CancelledError): raise
    finally:
        summary['central_cost'] = Ledger(out/'accounting.sqlite', plan.budget).totals()
        summary['source_unchanged'] = digest(source_manifest(ROOT)) == plan.source_sha256
        summary['fixture_unchanged'] = workspace_manifest(out/'workspace') == identities
        if not summary['source_unchanged'] or not summary['fixture_unchanged']: summary['status'] = 'stopped'
        # Standalone logical arm cost includes capture; experimental spend counts it once.
        capture_cost = next((r['central_cost'] for r in summary['phases'] if r['phase'] == 'capture'), {})
        summary['accounting_note'] = 'central_cost is newly dispatched experiment cost; each arm logically includes the capture cost once; replay events cost zero new calls, never zero deployment prefix cost'
        summary['common_prefix_cost'] = capture_cost
        atomic_write(out/'summary.json', canonical(summary))
    return summary


def prepare(out: Path, protocol: str, pressure: str, provider_path=None, budget_path=None, seed=None, recovery_interface="legacy"):
    out = new_destination(out)
    if provider_path:
        provider = Provider.model_validate(strict_json_loads(provider_path.read_bytes()))
        require(budget_path is not None, 'Live proposal requires explicit user-supplied budget file; no inherited budget')
        budget = Budget.model_validate(strict_json_loads(budget_path.read_bytes()))
    else:
        provider = Provider(name='rc7-scripted', protocol=protocol, mock=True, model='mock-reasoner',
                            base_url='http://127.0.0.1:1/v1', effort='low', concurrency=1, max_output_tokens=65536,
                            requests_per_minute=100000, tokens_per_minute=100000000,
                            retry={'max_attempts': 1, 'total_seconds': 60, 'base_seconds': 0, 'cap_seconds': 0})
        budget = Budget(max_total_tokens=4_000_000, per_cell_tokens=2_000_000, max_attempts=18, per_cell_attempts=6)
    seed = seed or secrets.token_hex(16)
    order = ['full', 'projected'] if int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 2 == 0 else ['projected', 'full']
    plan = PairedPlan(source_sha256=digest(source_manifest(ROOT)), provider=provider, budget=budget,
                      pressure=pressure, seed=seed, branch_order=order,
                      policy={"mode":"observe", "recovery_interface":recovery_interface},
                      wall_seconds_per_phase=180 if provider.mock else 3600)
    out.mkdir(parents=True, mode=0o700)
    atomic_write(out/'PLAN.json', canonical(plan.model_dump()))
    atomic_write(out/'PLAN.sha256', digest(plan.model_dump())+'\n')
    atomic_write(out/'AUTHORIZATION.json', canonical({'paid_authorized': False, 'proposal_only': not provider.mock,
                                                       'price_assumption': 'no official or proxy prices invented',
                                                       'upstream_output_cap_proven': False,
                                                       'declared_max_calls': plan.max_prefix_requests+2*plan.max_suffix_requests,
                                                       'full_workload_budget_guarantee': False}))
    return plan


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare'); prep.add_argument('--out', type=Path, required=True)
    prep.add_argument('--protocol', choices=['chat_completions', 'responses'], default='responses')
    prep.add_argument('--pressure', choices=['short', 'long'], default='long'); prep.add_argument('--seed')
    prep.add_argument('--provider', type=Path); prep.add_argument('--budget', type=Path)
    prep.add_argument('--recovery-interface', choices=['legacy','split_v1'], default='legacy')
    r = sub.add_parser('run'); r.add_argument('--plan', type=Path, required=True); r.add_argument('--plan-sha256', required=True)
    r.add_argument('--out', type=Path, required=True); r.add_argument('--allow-paid', action='store_true')
    r.add_argument('--acknowledge-local-readonly', action='store_true')
    a = p.parse_args()
    if a.command == 'prepare':
        result = prepare(a.out, a.protocol, a.pressure, a.provider, a.budget, a.seed, a.recovery_interface)
        print(canonical({'plan_sha': digest(result.model_dump()), 'paid_authorized': False})); return 0
    result = asyncio.run(run(a.plan, a.plan_sha256, a.out, a.allow_paid, a.acknowledge_local_readonly))
    print(canonical({'status': result['status'], 'central_cost': result['central_cost']}))
    return 0 if result['status'] in {'paired_complete', 'no_eligible_prefix'} else 2

if __name__ == '__main__': raise SystemExit(main())
