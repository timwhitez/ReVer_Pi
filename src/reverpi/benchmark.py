"""Frozen, sequential Harbor matrix. No automatic task replay or verifier guessing.

A benchmark is a separately acquired, licensed task collection. Bundled examples
are owned SEARCH fixtures. This controller never gives verifier files to the LLM.
Harbor owns sandbox setup, task execution and the official verifier.
"""
from __future__ import annotations
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .config import StrictModel, Provider, StudyConfig
from .data import load_jsonl,source_identity_keys
from .errors import LabError
from .processes import stop_process_group
from .gateway import Sessions
from .ledger import Ledger
from .results import write_results, read_results
from .memory import METHODS
from .util import atomic_write,canonical,digest,bytes_digest,source_manifest,process_lock,safe_id,strict_json_loads


class TaskRef(StrictModel):
    id: str = Field(min_length=1,max_length=128)
    path: str
    split: Literal['search','dev','gate','external']
    source_group: str = Field(min_length=1)
    provenance: dict[str,str]
    license: str = Field(min_length=1)
    synthetic: bool = False


class MatrixConfig(StrictModel):
    name: str = 'native-pilot'
    split: Literal['search','dev','gate','external'] = 'search'
    methods: list[str] = Field(default_factory=lambda:['pi_original','pi_native','mask','rever_lite'])
    repeats: int = Field(1,ge=1,le=10)
    seed: int = 20260912
    wall_seconds: int = Field(1800,ge=10,le=86400)
    trial_deadline_seconds: int = Field(3000,ge=60,le=172800)
    max_tools: int = Field(200,ge=1,le=10000)
    max_turns: int = Field(80,ge=1,le=10000)
    reward_key: str = 'reward'
    success_threshold: float = 1.0
    allow_agent_hosts: list[str] = Field(default_factory=list)
    # For now the controller is intentionally a single-host Docker path. Other
    # Harbor environments may use the adapter directly after a new acceptance run.
    environment: Literal['docker'] = 'docker'

    @model_validator(mode='after')
    def check(self):
        if not self.methods or len(set(self.methods)) != len(self.methods):
            raise ValueError('Methods must be nonempty and unique')
        if set(self.methods) - (set(METHODS)|{'pi_original','pi_native'}):
            raise ValueError('Unsupported native method')
        if 'full' in self.methods:
            raise ValueError('full is a checkpoint reference, not a native Pi algorithm; use pi_original/pi_native')
        if self.trial_deadline_seconds <= self.wall_seconds:
            raise ValueError('Reserve time for environment setup and the verifier')
        if any(not h or any(x in h for x in ['\n','\r','://',' ']) for h in self.allow_agent_hosts):
            raise ValueError('Allowlisted hosts must be hostnames or address literals, not URLs')
        return self


def tree_digest(root: Path) -> str:
    """Hash curator-side task files, including verifier identity, without loading them into a prompt."""
    h=hashlib.sha256();count=0;total=0
    if not root.is_dir():raise ValueError('Task directory missing: '+str(root))
    for p in sorted(root.rglob('*')):
        if any(part in {'.git','__pycache__','.pytest_cache'} for part in p.relative_to(root).parts):continue
        if p.is_symlink():raise ValueError('Task manifests require materialized files, not mutable symlinks')
        if not p.is_file():continue
        count+=1;total+=p.stat().st_size
        if count>20000 or total>2*1024**3:raise ValueError('Task hashing limits exceeded; curate a bounded task package')
        h.update(str(p.relative_to(root)).encode());h.update(b'\0')
        with p.open('rb') as f:
            while b:=f.read(65536):h.update(b)
        h.update(b'\0')
    return h.hexdigest()


def read_tasks(manifest: Path):
    rows=[TaskRef.model_validate(x) for x in load_jsonl(manifest)]
    if not rows or len({x.id for x in rows})!=len(rows):raise ValueError('Empty/duplicate task manifest')
    for row in rows:
        safe_id(row.id)
        path=Path(row.path)
        if not path.is_absolute():path=manifest.parent/path
        row.path=str(path.resolve())
        if not all((path/n).exists() for n in ['instruction.md','task.toml','tests/test.sh']):
            raise ValueError('Expected a Linux Harbor task with instruction.md, task.toml and tests/test.sh')
    return rows


def audit_tasks(rows):
    owners={};conflicts=[];flags=[];seen_text={};bags=[]
    for r in rows:
        for key in source_identity_keys(r.source_group,r.provenance):
            for other in owners.get(key,[]):
                if other.split!=r.split:conflicts.append({'kind':'shared_source','key':key,'left':other.id,'right':r.id})
            owners.setdefault(key,[]).append(r)
        text=(Path(r.path)/'instruction.md').read_text(encoding='utf8')
        fp=digest(text)
        if fp in seen_text and seen_text[fp].split!=r.split:
            conflicts.append({'kind':'same_instruction','left':seen_text[fp].id,'right':r.id})
        seen_text[fp]=r;bags.append(set(re.findall(r'\w+',text.lower())))
    if len(rows)>2000:
        raise ValueError('Large-manifest near-duplicate audit requires an independently reviewed scalable implementation')
    for i,a in enumerate(rows):
        for j,b in enumerate(rows[:i]):
            if a.split==b.split:continue
            sim=len(bags[i]&bags[j])/max(1,len(bags[i]|bags[j]))
            if sim>=.85:flags.append({'left':a.id,'right':b.id,'instruction_jaccard':sim})
    return {'tasks':len(rows),'conflicts':conflicts,'near_duplicate_flags':flags,'pretraining_contamination':'unknown',
            'scope':'Curator identities + exact/near public instruction checks. Not a proof of repository ancestry or patch non-overlap.'}


def make_plan(root:Path, manifest:Path, config:MatrixConfig, provider:Provider, gateway_config:StudyConfig,
              out:Path, *, probe_path:Path|None=None, review_path:Path|None=None, worker_path:Path|None=None, acceptance_path:Path|None=None):
    rows=read_tasks(manifest);audit=audit_tasks(rows)
    if audit['conflicts'] or audit['near_duplicate_flags']:raise LabError('dataset_overlap','Resolve cross-split source/instruction overlap before planning')
    selected=[r for r in rows if r.split==config.split]
    if not selected:raise ValueError('The selected split has no tasks')
    if set(config.methods)-set(gateway_config.methods):raise ValueError('Matrix methods are absent from the gateway allowlist')
    code_sha=digest(source_manifest(root));certificates={}
    if config.split in {'gate','external'}:
        if provider.mock or probe_path is None or review_path is None or worker_path is None or acceptance_path is None:
            raise LabError('heldout_locked','Formal native plans require a real probe, independent reviews and a prepared worker')
        if not any(r.split in {'search','dev'} for r in rows):
            raise LabError('audit_scope','Formal native plans require the development-source inventory for cross-split auditing')
        from .review import check_review
        pr=json.loads(probe_path.read_text());review=check_review(review_path,root)
        if pr.get('mock') or not pr.get('compatible_for_this_probe') or pr.get('provider_sha')!=digest(provider.model_dump()):
            raise LabError('heldout_locked','Live protocol probe does not match this exact Provider')
        if review.get('source_sha')!=code_sha or not review.get('eligible_for_human_acceptance'):
            raise LabError('heldout_locked','Independent code review is incomplete or for different source')
        if config.split=='external' and any(r.synthetic for r in selected):raise LabError('synthetic_external','Owned fixtures are not an independent external benchmark')
        if config.split=='external' and any(not r.provenance.get('benchmark') for r in selected):
            raise ValueError('External tasks require their actual named benchmark provenance')
        envpath=root/'environment.lock.json'
        if not envpath.exists():raise LabError('environment_unverified','Capture and test the native environment before formal evaluation')
        env=json.loads(envpath.read_text())
        if env.get('harbor') != '0.22.0' or not env.get('native_checks_passed'):
            raise LabError('environment_unverified','Native environment tests were not recorded as passed')
        from .acceptance import check_acceptance
        acc=json.loads(acceptance_path.read_text())
        check_acceptance(acc,code_sha,digest(provider.model_dump()),config.methods,
            require_recovery_methods=[m for m in config.methods if m not in {'pi_original','pi_native'}])
        if acc.get('gateway_config_sha') != digest(gateway_config.model_dump()):
            raise LabError('native_acceptance_required','Acceptance used a different gateway configuration')
        certificates={'probe_sha':digest(pr),'review_sha':digest(review),'native_acceptance_sha':digest(acc)}
    worker=None
    if worker_path is not None:
        worker=json.loads(Path(str(worker_path)+'.json').read_text())
        if worker.get('archive_sha256')!=bytes_digest(worker_path.read_bytes()) or worker.get('source_sha')!=code_sha:
            raise LabError('worker_mismatch','Worker archive/version differs from the reviewed source')
    hashes={r.id:tree_digest(Path(r.path)) for r in rows}
    grid=[]
    for task in selected:
        for rep in range(config.repeats):
            methods=list(config.methods);random.Random(digest([task.id,rep,config.seed])).shuffle(methods)
            for method in methods:
                grid.append({'task_id':task.id,'source_group':task.source_group,'method':method,'repeat':rep,'path':task.path,
                             'task_sha':hashes[task.id]})
    frozen={'schema':1,'track':'native_pi_harbor','matrix':config.model_dump(),'provider':provider.model_dump(),
            'gateway_study':gateway_config.model_dump(),'code_sha':code_sha,'manifest_path':str(manifest.resolve()),
            'manifest_sha':bytes_digest(manifest.read_bytes()),'all_task_hashes':hashes,'all_tasks':[r.model_dump() for r in rows],
            'worker':worker,'certificates':certificates,'audit':audit,'mock':provider.mock}
    plan_id=digest(frozen)
    for row in grid:row['cell']='bm_'+digest([plan_id,row['task_id'],row['method'],row['repeat']])[:40]
    plan={**frozen,'plan_id':plan_id,'grid':grid}
    if out.exists():raise ValueError('Plan exists; create a new explicit generation, never silently overwrite')
    atomic_write(out,canonical(plan));return {'plan_id':plan_id,'cells':len(grid),'split':config.split,'mock':provider.mock,'plan':str(out)}


def validate_plan(root:Path,path:Path,worker:Path|None=None):
    plan=json.loads(path.read_text());frozen={k:v for k,v in plan.items() if k not in {'plan_id','grid'}}
    if plan['plan_id']!=digest(frozen) or plan['code_sha']!=digest(source_manifest(root)):
        raise LabError('freeze_changed','Plan or implementation differs from the frozen source')
    mp=Path(plan['manifest_path'])
    if bytes_digest(mp.read_bytes())!=plan['manifest_sha']:raise LabError('freeze_changed','Task manifest changed')
    for r in plan['all_tasks']:
        if tree_digest(Path(r['path']))!=plan['all_task_hashes'][r['id']]:raise LabError('freeze_changed','A task or verifier has changed since planning')
    expected=[];cfg=MatrixConfig.model_validate(plan['matrix'])
    for task in plan['all_tasks']:
        if task['split']!=cfg.split:continue
        for rep in range(cfg.repeats):
            methods=list(cfg.methods);random.Random(digest([task['id'],rep,cfg.seed])).shuffle(methods)
            for method in methods:
                expected.append({'task_id':task['id'],'source_group':task['source_group'],'method':method,'repeat':rep,'path':task['path'],
                    'task_sha':plan['all_task_hashes'][task['id']],'cell':'bm_'+digest([plan['plan_id'],task['id'],method,rep])[:40]})
    if expected!=plan['grid']:raise LabError('grid_changed','Benchmark grid was modified outside its plan')
    if worker is not None:
        if not plan.get('worker') or bytes_digest(worker.read_bytes())!=plan['worker']['archive_sha256']:
            raise LabError('worker_mismatch','The exact prepared worker must be part of the frozen plan')
    return plan


def harbor_command(executable,row,plan,gateway_run,gateway_url,worker,jobs):
    cfg=plan['matrix']
    argv=[executable,'run','-p',row['path'],'--agent','reverpi.harbor_agent:ReVerPiAgent',
          '-m',plan['provider']['model'],'-k','1','-n','1','--max-retries','0','--env',cfg['environment'],
          '--job-name',row['cell'],'--jobs-dir',str(jobs)]
    for key,value in {'gateway_run':str(gateway_run),'gateway_url':gateway_url,'worker_archive':str(worker),
                      'method':row['method'],'cell_id':row['cell'],'wall_seconds':cfg['wall_seconds'],
                      'max_tools':cfg['max_tools'],'max_turns':cfg['max_turns']}.items():
        argv+=['--ak',f'{key}={value}']
    for host in cfg['allow_agent_hosts']:argv+=['--allow-agent-host',host]
    return argv


def read_official_result(directory:Path,row:dict,reward_key:str,threshold:float):
    candidates=[]
    for p in directory.rglob('result.json'):
        try:r=strict_json_loads(p.read_text())
        except (ValueError,OSError):continue
        if isinstance(r,dict) and 'trial_name' in r and 'task_name' in r:candidates.append((p,r))
    if len(candidates)!=1:raise LabError('verifier_result_missing','Expected exactly one authoritative Harbor trial result, not an aggregate or guessed score')
    path,result=candidates[0]
    config=result.get('config')
    agent=config.get('agent') if isinstance(config,dict) else None
    kwargs=agent.get('kwargs') if isinstance(agent,dict) else None
    if not isinstance(kwargs,dict) or kwargs.get('cell_id')!=row['cell']:
        raise LabError('result_identity','Trial result does not carry the planned cell identity')
    if 'method' in row and kwargs.get('method')!=row['method']:
        raise LabError('result_identity','Trial compression method differs from the planned condition')
    verifier=result.get('verifier_result')
    rewards=verifier.get('rewards') if isinstance(verifier,dict) else None
    value=rewards.get(reward_key) if isinstance(rewards,dict) else None
    if type(value) not in {int,float} or not math.isfinite(value):
        raise LabError('verifier_result_missing','Declared numeric reward is absent; no zero or success is invented')
    return {'status':'complete','success':value>=threshold,'official_reward':value,'reward_key':reward_key,
            'official_result_path':str(path.resolve()),'official_result_sha':bytes_digest(path.read_bytes()),
            'official_task_checksum':result.get('task_checksum'),'harbor_exception':result.get('exception_info'),
            'execution_success':not bool(result.get('exception_info'))}


def _gateway_ledger(path:Path,plan):
    ledger=Ledger(path/'ledger.sqlite',StudyConfig.model_validate(plan['gateway_study']).budget)
    with ledger.db() as db:
        code=db.execute("SELECT value FROM meta WHERE key='gateway_code'").fetchone()
        study=db.execute("SELECT value FROM meta WHERE key='gateway_study'").fetchone()
        providers=db.execute("SELECT value FROM meta WHERE key LIKE 'provider:%'").fetchall()
    if not code or json.loads(code[0])!=plan['code_sha'] or not study or json.loads(study[0])!=plan['gateway_study']:
        raise LabError('gateway_identity','Gateway source/study differs from the plan')
    if len(providers)!=1 or json.loads(providers[0][0])!=plan['provider']:
        raise LabError('gateway_identity','Gateway Provider differs from the plan')
    return ledger


async def run_matrix(root:Path,plan_path:Path,gateway_run:Path,gateway_url:str,worker:Path,out:Path,*,allow_paid=False,acknowledge_heldout=False):
    plan=validate_plan(root,plan_path,worker)
    if not plan['mock'] and not allow_paid:raise ValueError('Benchmark execution needs --allow-paid')
    if plan['matrix']['split'] in {'gate','external'} and not acknowledge_heldout:raise LabError('heldout_locked','Explicit held-out acknowledgment required')
    from urllib.parse import urlsplit
    if urlsplit(gateway_url).hostname in {'127.0.0.1','localhost','::1'}:
        raise ValueError('Docker cannot use the host loopback URL; configure a restricted reachable gateway and its allowlist')
    executable=shutil.which('harbor')
    if not executable:raise ValueError('Harbor/Docker must be installed and accepted before native evaluation')
    if importlib.metadata.version("harbor") != "0.22.0":
        raise ValueError("Native controller is pinned to Harbor 0.22.0; adapt and re-review before changing it")
    # Fail clearly on CLI drift; do not silently change the benchmark invocation.
    helptext=subprocess.run([executable,'run','--help'],capture_output=True,text=True,timeout=30,check=True).stdout
    for flag in ['--agent','--jobs-dir','--max-retries']+(['--allow-agent-host'] if plan['matrix']['allow_agent_hosts'] else []):
        if flag not in helptext:raise ValueError('Installed Harbor CLI lacks required flag '+flag)
    ledger=_gateway_ledger(gateway_run,plan);sessions=Sessions(gateway_run/'sessions.sqlite')
    out.mkdir(parents=True,exist_ok=True)
    with process_lock(out/'matrix.lock'):
        manifest={**plan,'study':{'split':plan['matrix']['split'],'methods':plan['matrix']['methods']},'results_schema':2,'gateway_run':str(gateway_run.resolve())}
        manifest_path=out/'manifest.json'
        if manifest_path.exists() and strict_json_loads(manifest_path.read_bytes())!=manifest:
            raise LabError('manifest_changed','Existing native manifest differs; preserve the old run and create a new directory')
        ledger.bind('benchmark_plan',plan['plan_id'])
        if not manifest_path.exists():atomic_write(manifest_path,canonical(manifest))
        target=out/'results.json';rows=[{**r,'status':'pending','success':None} for r in plan['grid']]
        if target.exists():
            old=read_results(out,manifest)
            if len(old)!=len(rows) or any(any(a.get(k)!=b[k] for k in b if k not in {'status','success'}) for a,b in zip(old,rows)):
                raise LabError('grid_changed','Saved result cells do not match the immutable plan')
            rows=old
        if any(r['status']=='running' or r.get('requires_environment_reconciliation') for r in rows):
            raise LabError('benchmark_in_doubt','A prior trial may still be executing. Collect/reconcile it; never auto-replay task side effects.')
        write_results(target,rows)
        for row in rows:
            if row['status']!='pending':continue
            if ledger.stop_reason():break
            # A completed official result already present is NOT silently rerun.
            jobdir=out/'jobs'/row['cell']
            if jobdir.exists():raise LabError('benchmark_in_doubt','Unexpected pre-existing Harbor job directory')
            argv=harbor_command(executable,row,plan,gateway_run.resolve(),gateway_url,worker.resolve(),(out/'jobs').resolve())
            row.update(status='running',started=time.time());write_results(target,rows)
            proc=None
            try:
                # Deliberately no host API key / .env is forwarded. Per-task secret
                # services need an explicitly audited integration, not inherited credentials.
                env={k:os.environ[k] for k in ['PATH','LANG','LC_ALL','TZ','VIRTUAL_ENV','DOCKER_HOST'] if k in os.environ}
                controller_home=out/'controller-home';controller_home.mkdir(exist_ok=True)
                env['HOME']=str(controller_home.resolve());env['PYTHONPATH']=str((root/'src').resolve());env['NO_COLOR']='1'
                logpath=out/'controller-logs'/f"{row['cell']}.log";logpath.parent.mkdir(exist_ok=True)
                with logpath.open('wb') as log:
                    os.chmod(logpath,0o600)
                    proc=await asyncio.create_subprocess_exec(*argv,env=env,cwd=out,stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
                    async def drain():
                        n=0
                        while b:=await proc.stdout.read(65536):
                            n+=len(b)
                            if n>100*1024**2:raise LabError('log_quota','Harbor controller log exceeds 100 MiB')
                            log.write(b);log.flush()
                    async with asyncio.timeout(plan['matrix']['trial_deadline_seconds']):
                        async with asyncio.TaskGroup() as group:
                            group.create_task(drain());group.create_task(proc.wait())
                row['controller_exit_code']=proc.returncode
                row.update(read_official_result(jobdir,row,plan['matrix']['reward_key'],plan['matrix']['success_threshold']))
            except asyncio.CancelledError:
                row.update(status='interrupted_unknown',success=None,requires_environment_reconciliation=True,error={'kind':'cancelled'});raise
            except (TimeoutError,LabError,OSError,ExceptionGroup) as err:
                row.update(status='infrastructure_error',success=None,requires_environment_reconciliation=True,error=err.record() if isinstance(err,LabError) else {'kind':type(err).__name__})
            finally:
                sessions.disable(row['cell'])
                await stop_process_group(proc)
                row.update(cost=ledger.totals(row['cell']),finished=time.time())
                write_results(target,rows)
            if row['status']!='complete':break  # Inspect infrastructure before spending on more sandboxes.
        return {'cells':len(rows),'finished':sum(r['status']!='pending' for r in rows),'heldout_scores_sealed':plan['matrix']['split'] in {'gate','external'},'out':str(out)}


def collect_matrix(out:Path,gateway_run:Path,*,mark_interrupted=False,acknowledge_cleanup=False):
    """No task execution. Import an existing result or explicitly mark an orphan unknown."""
    plan=json.loads((out/'manifest.json').read_text());ledger=_gateway_ledger(gateway_run,plan)
    with ledger.db() as db:
        bound = db.execute("SELECT value FROM meta WHERE key='benchmark_plan'").fetchone()
    if bound is None or json.loads(bound[0]) != plan['plan_id']:
        raise LabError("ledger_identity", "Result collection requires the ledger bound to this exact benchmark plan")
    sessions=Sessions(gateway_run/'sessions.sqlite')
    with process_lock(out/'matrix.lock'):
        rows=read_results(out,plan)
        for row in rows:
            if row['status'] not in {'running','infrastructure_error','interrupted_unknown'}:continue
            try:row.update(read_official_result(out/'jobs'/row['cell'],row,plan['matrix']['reward_key'],plan['matrix']['success_threshold']))
            except LabError:
                if mark_interrupted:
                    if not acknowledge_cleanup:raise ValueError('Confirm orphan environment/process cleanup before marking interrupted')
                    row.update(status='interrupted_unknown',success=None,operator_acknowledged_cleanup=True)
                else:continue
            row['requires_environment_reconciliation']=False
            sessions.disable(row['cell']);row['cost']=ledger.totals(row['cell'])
        for row in rows:row['cost']=ledger.totals(row['cell'])
        write_results(out/'results.json',rows)
    return {'collected':True,'automatic_replays':0}
