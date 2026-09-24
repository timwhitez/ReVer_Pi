"""Frozen, single-budget same-parent experiments; not a native benchmark runner.

The agent chooses its tools. This scheduler does not classify questions or pick
an action using gold. No interrupted arm is replayed; all planned units remain
in the denominator. Workspace copying is isolation of state, NOT a sandbox.
"""
from __future__ import annotations
import asyncio
import json
import shutil
from pathlib import Path
import random
import re
from .config import Provider, Budget, CompressionConfig
from .errors import LabError
from .ledger import Ledger
from .paired_interventions import Arm, read_intervention, run_arm, intervention_cell
from .revalidation import load_registry
from .transport import APIClient
from .util import atomic_write, bytes_digest, canonical, digest, process_lock, source_manifest, strict_json_loads


def tree_identity(root: Path) -> dict[str,str]:
    """Exact small development snapshot. Symlinks and special files are rejected."""
    root=root.resolve(strict=True)
    if not root.is_dir():raise ValueError('Workspace template must be a directory')
    result={};size=0
    for p in sorted(root.rglob('*')):
        if p.is_symlink():raise ValueError('Workspace snapshot contains a symlink')
        if p.is_dir():continue
        if not p.is_file():raise ValueError('Workspace snapshot contains a nonregular file')
        size+=p.stat().st_size
        if size>512*1024**2 or len(result)>=10000:raise ValueError('Use a bounded development snapshot')
        result[str(p.relative_to(root))]=bytes_digest(p.read_bytes())
    return result


def prepare_matrix(out: Path, parent: Path, parent_sha: str, provider: Provider, budget: Budget,
                   archive: CompressionConfig, *, workspace: Path, registry: Path,
                   registry_sha: str, repeats: int=2, seed: int=20260919,
                   recovery_calls: int=3, revalidation_calls: int=2, max_turns: int=6,
                   interrupted_policy: str='halt', evaluation_gold_sha256: str | None=None) -> dict:
    """Predeclare all repeats/options before any call. Copies only public task inputs."""
    if type(repeats) is not int or not 1<=repeats<=20 or type(seed) is not int:
        raise ValueError('Require 1..20 predeclared repeats and an integer seed')
    if interrupted_policy not in {'halt','quarantine_and_continue'}:raise ValueError('Unknown interruption policy')
    if evaluation_gold_sha256 is not None and not re.fullmatch(r'[a-f0-9]{64}',evaluation_gold_sha256):
        raise ValueError('Gold commitment must be a SHA-256, never the answers')
    obj=read_intervention(parent,parent_sha)
    specs=load_registry(registry,registry_sha)
    if not specs:raise ValueError('A public verifier registry is required for the four-arm design')
    workspace=workspace.resolve(strict=True);out=out.resolve()
    if out==workspace or out.is_relative_to(workspace) or workspace.is_relative_to(out):
        raise ValueError('Matrix output and immutable workspace template must be disjoint')
    identity=tree_identity(workspace)
    from .revalidation import within
    for spec in specs.values():
        for name in spec.inputs:within(workspace,name)
    arms=[Arm('noop',0,0,max_turns),Arm('recover',recovery_calls,0,max_turns),
          Arm('revalidate',0,revalidation_calls,max_turns),Arm('both',recovery_calls,revalidation_calls,max_turns)]
    units=[]
    for repeat in range(repeats):
        trial=f'rep-{repeat:03d}'
        for arm in arms:
            units.append({'unit':trial+'-'+arm.name,'trial_id':trial,'arm':arm.__dict__,
                          'cell':intervention_cell(parent_sha,arm,trial_id=trial)})
    random.Random(seed).shuffle(units)
    source=digest(source_manifest(Path(__file__).resolve().parents[2]))
    plan={'schema':1,'kind':'development_same_parent_matrix','independent_benchmark':False,
          'parent_sha256':parent_sha,'source_group':obj['checkpoint']['source_group'],
          'memory_text_sha256':bytes_digest(obj['memory']['text'].encode()),
          'provider':provider.model_dump(),'budget':budget.model_dump(),'archive':archive.model_dump(),
          'source_sha256':source,'registry_sha256':registry_sha,'workspace_files':identity,
          'units':units,'seed':seed,'interrupted_policy':interrupted_policy,
          'parent_compression_cost':obj['parent_compression_cost'],
          'formal_gate_passed':False,'workspace_clone_is_security_sandbox':False,
          'evaluation':{'gold_sha256':evaluation_gold_sha256,'grader':'exact_strings_v1','gold_sent_to_agent':False}}
    # A partial preparation is preserved rather than silently overwritten.
    out.mkdir(parents=True,exist_ok=False)
    shutil.copytree(workspace,out/'template');shutil.copyfile(parent,out/'parent.json')
    shutil.copyfile(registry,out/'registry.json')
    if tree_identity(out/'template')!=identity or bytes_digest((out/'parent.json').read_bytes())!=parent_sha or bytes_digest((out/'registry.json').read_bytes())!=registry_sha:
        raise ValueError('Input changed while preparing immutable matrix')
    atomic_write(out/'plan.json',canonical(plan))
    return {'plan_sha256':digest(plan),'units':len(units),'paid_model_calls':0,'directory':str(out)}


def load_plan(root: Path, expected_sha: str) -> dict:
    plan=strict_json_loads((root/'plan.json').read_bytes())
    if digest(plan)!=expected_sha or plan.get('kind')!='development_same_parent_matrix' or plan.get('schema')!=1:
        raise ValueError('Matrix plan identity mismatch')
    if digest(source_manifest(Path(__file__).resolve().parents[2]))!=plan['source_sha256']:
        raise LabError('source_changed','Use the exact frozen source, or a new matrix generation')
    if tree_identity(root/'template')!=plan['workspace_files']:
        raise ValueError('Workspace template changed')
    read_intervention(root/'parent.json',plan['parent_sha256'])
    load_registry(root/'registry.json',plan['registry_sha256'])
    return plan


def commit_unit(folder: Path, unit: dict, plan_sha: str):
    files={name:bytes_digest((folder/name).read_bytes()) if (folder/name).exists() else None for name in
           ('plan.json','result.json','tool_receipts.json')}
    if not all(files.get(n) for n in ('plan.json','result.json')):raise ValueError('Unit lacks a terminal result')
    atomic_write(folder/'COMMIT.json',canonical({'matrix_sha256':plan_sha,'unit':unit,'files':files}))


def read_committed(folder: Path, unit: dict, plan_sha: str) -> dict:
    c=strict_json_loads((folder/'COMMIT.json').read_bytes())
    if c.get('matrix_sha256')!=plan_sha or c.get('unit')!=unit or not {'plan.json','result.json'}<=set(c.get('files',{})):
        raise LabError('result_changed','Mismatched committed matrix unit')
    if set(c['files'])-{'plan.json','result.json','tool_receipts.json'}:
        raise LabError('result_changed','Unexpected committed file path')
    for name,h in c['files'].items():
        actual=bytes_digest((folder/name).read_bytes()) if (folder/name).exists() else None
        if folder.is_symlink() or (folder/name).is_symlink() or actual!=h:
            raise LabError('result_changed','Committed unit was modified; no replacement run permitted')
    r=strict_json_loads((folder/'result.json').read_bytes())
    if r.get('cell')!=unit['cell'] or r.get('trial_id')!=unit['trial_id']:
        raise LabError('result_changed','Committed result has a different cell identity')
    return r


async def execute_matrix(root: Path, expected_sha: str, *, allow_paid=False,
                         acknowledge_unsandboxed=False, transport=None) -> dict:
    """One ledger for all arms. Cached terminal commits do not cause new calls."""
    root=root.resolve(strict=True)
    if not acknowledge_unsandboxed:raise ValueError('Run in a disposable task sandbox and explicitly acknowledge execution')
    plan=load_plan(root,expected_sha)
    provider=Provider.model_validate(plan['provider'])
    if not provider.mock and not allow_paid:raise ValueError('Live runs require --allow-paid with the frozen aggregate budget')
    from .preflight import validate_provider_environment
    validate_provider_environment(provider)
    budget=Budget.model_validate(plan['budget']);cfg=CompressionConfig.model_validate(plan['archive'])
    specs=load_registry(root/'registry.json',plan['registry_sha256'])
    records=[];halt=None
    def stop_for(result):
        if not result.get('source_unchanged'):return 'source_changed'
        kind=(result.get('failure') or {}).get('kind')
        if kind in {'operation_in_doubt','transport_ambiguous'} and plan['interrupted_policy']=='quarantine_and_continue':return None
        return kind if kind in {'budget_exhausted','authentication','permission','quota','model_drift',
            'operation_in_doubt','run_stopped','cache_integrity','provider_cooldown','idempotency_conflict',
            'identity_changed','disk_guard','reservation_breach','transport_ambiguous','configuration_preflight'} else None
    with process_lock(root/'writer.lock'):
        ledger=Ledger(root/'ledger.sqlite',budget);ledger.bind('paired_matrix',plan)
        def snapshot():
            report={'schema':1,'plan_sha256':expected_sha,'planned_units':len(plan['units']),
                    'visited_units':len(records),'unvisited_units':len(plan['units'])-len(records),
                    'units':records,'halt_reason':halt,'cost':ledger.totals(),
                    'source_unchanged':digest(source_manifest(Path(__file__).resolve().parents[2]))==plan['source_sha256'],
                    'parent_compression_cost':plan['parent_compression_cost'],
                    'cost_note':'Reader ledger excludes parent setup. Charge it once to experimental spend, once per deployment arm comparison.',
                    'scored_by_this_runner':False,'formal_gate_passed':False,'independent_benchmark':False}
            atomic_write(root/'matrix_report.json',canonical(report));return report
        try:
            async with APIClient(provider,ledger,transport=transport) as client:
                for unit in plan['units']:
                    load_plan(root,expected_sha) # fail before the next dispatch on drift
                    folder=root/'arms'/unit['unit'];workspace=root/'workspaces'/unit['unit']
                    if (folder/'COMMIT.json').exists():
                        result=read_committed(folder,unit,expected_sha)
                        records.append({'unit':unit['unit'],'status':result['status'],'reused_commit':True,
                                        'cell':unit['cell'],'result_sha256':bytes_digest((folder/'result.json').read_bytes())})
                        halt=stop_for(result)
                        if halt:break
                        continue
                    with ledger.db() as db:
                        existing=db.execute('SELECT 1 FROM operations WHERE cell=? LIMIT 1',(unit['cell'],)).fetchone()
                    if folder.exists() or workspace.exists() or existing:
                        records.append({'unit':unit['unit'],'cell':unit['cell'],'status':'quarantined_interruption',
                                        'replayed':False})
                        if plan['interrupted_policy']=='halt':halt='interrupted_unit';break
                        snapshot();continue
                    if ledger.stop_reason():halt=ledger.stop_reason();break
                    workspace.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copytree(root/'template',workspace)
                    if tree_identity(workspace)!=plan['workspace_files']:raise ValueError('Workspace clone mismatch')
                    atomic_write(root/'progress.json',canonical({'state':'running','unit':unit,'plan_sha256':expected_sha}))
                    result=await run_arm(root/'parent.json',plan['parent_sha256'],Arm(**unit['arm']),client,folder,
                                         archive_config=cfg,workspace=workspace,verifiers=specs,trial_id=unit['trial_id'])
                    commit_unit(folder,unit,expected_sha)
                    records.append({'unit':unit['unit'],'cell':unit['cell'],'status':result['status'],'reused_commit':False,
                                    'result_sha256':bytes_digest((folder/'result.json').read_bytes())})
                    halt=stop_for(result)
                    if halt:break
                    snapshot()
        except BaseException as exc:
            halt=exc.kind if isinstance(exc,LabError) else type(exc).__name__
            raise
        finally:
            report=snapshot()
            atomic_write(root/'progress.json',canonical({'state':'halted' if halt else 'finished','halt_reason':halt,
                                                        'plan_sha256':expected_sha,'visited_units':len(records)}))
        return report
