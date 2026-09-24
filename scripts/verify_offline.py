#!/usr/bin/env python3
"""Execute the bounded zero-model verification profile, not research experiments.

Linux + C compiler required for the fail-closed nonloopback network guard.
Dependencies are checked, not downloaded. Every run uses a NEW output directory.
The guard is defense against accidental egress from cooperating dynamic programs,
not a sandbox against malicious code or a substitute for Docker/Harbor testing.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def clean_environment(root:Path,home:Path,guard:Path,guard_log:Path) -> dict[str,str]:
    env=dict(os.environ)
    for key in list(env):
        k=key.upper()
        if (any(word in k for word in ('API_KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL'))
                or k.endswith(('_PROXY','_KEY')) or k.startswith(('REVER_', 'PI_', 'AWS_', 'AZURE_', 'GOOGLE_', 'GITHUB_', 'OPENAI_', 'ANTHROPIC_', 'DEEPSEEK_'))
                or k in {'PYTHONPATH','PYTHONHOME','PYTHONOPTIMIZE','PYTHONINSPECT','PYTHONSTARTUP','NODE_OPTIONS','LD_PRELOAD'}):
            env.pop(key,None)
    env.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'config'),
        PYTHONPATH=str(root/'src'),PYTHONDONTWRITEBYTECODE='1',
        LD_PRELOAD=str(guard),REVER_OFFLINE_GUARD_LOG=str(guard_log),
        NO_PROXY='127.0.0.1,localhost,::1',REVER_OFFLINE_VERIFICATION='1')
    return env


def validate_destination(root:Path,out:Path) -> Path:
    if out.is_symlink():raise ValueError('Output cannot be a symlink')
    out=out.resolve()
    root=root.resolve()
    if out.exists() or out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError('Use a fresh output directory outside the source and its ancestors')
    for folder in ('src','tests','pi','scripts','provenance','data'):
        if out.is_relative_to(root/folder):raise ValueError('Output would modify implementation or historical evidence')
    return out


def atomic_report(path:Path,obj):
    temporary=path.with_suffix(path.suffix+'.new')
    temporary.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)


class Steps:
    def __init__(self,out:Path,env:dict[str,str]):
        self.out=out;self.env=env;self.rows=[]
    def run(self,name:str,argv:list[str],*,cwd:Path=ROOT,timeout:float=600,
            expected:tuple[int,...]=(0,)) -> dict:
        log=self.out/'logs'/f'{name}.log';start=time.monotonic()
        row={'name':name,'command':argv,'cwd':str(cwd),'expected_returncodes':list(expected),
             'started_at_epoch':time.time(),'status':'running'}
        self.rows.append(row);atomic_report(self.out/'progress.json',self.rows)
        proc=None
        try:
            with log.open('x') as stream:
                proc=subprocess.Popen(argv,cwd=cwd,env=self.env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
                row['pid']=proc.pid
                try:row['returncode']=proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    row['returncode']=124;row['timed_out']=True
                    self.stop(proc)
            row['status']='passed' if row['returncode'] in expected else 'failed'
        except BaseException as exc:
            if proc is not None:self.stop(proc)
            row.update(status='failed',error=type(exc).__name__)
            if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
        finally:
            if proc is not None:self.stop(proc)
            row['elapsed_seconds']=round(time.monotonic()-start,3)
            if log.exists():row['log_sha256']=hashlib.sha256(log.read_bytes()).hexdigest()
            atomic_report(self.out/'progress.json',self.rows)
        return row
    @staticmethod
    def stop(proc):
        # The immediate child may already have exited while descendants remain.
        try:
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            if proc.poll() is None:
                try:proc.wait(timeout=2)
                except subprocess.TimeoutExpired:pass
        finally:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            if proc.poll() is None:proc.wait(timeout=5)


def verify_mock_artifacts(out:Path) -> dict:
    """Require sealed 12x12 grids AND analysis; CLI exit zero is insufficient."""
    from reverpi.results import read_results
    rows_by_protocol={}
    for protocol in ('chat','responses'):
        records=read_results(out/f'{protocol}-all12')
        if len(records)!=144 or any(r.get('status')!='complete' for r in records):
            raise ValueError('Expected a fully completed 12x12 mock matrix')
        analysis=json.loads((out/f'{protocol}-analysis.json').read_text())
        comparisons=analysis.get('comparisons') if isinstance(analysis,dict) else None
        if (not isinstance(comparisons,list) or len(comparisons)!=11
                or any(not isinstance(x,dict) or x.get('baseline')!='full'
                       or x.get('paired_tasks')!=12 for x in comparisons)):
            raise ValueError('Invalid mock analysis artifact')
        probe=json.loads((out/f'{protocol}-probe/probe.json').read_text())
        expected_protocol='chat_completions' if protocol=='chat' else 'responses'
        if (not isinstance(probe,dict) or probe.get('mock') is not True
                or probe.get('protocol')!=expected_protocol
                or probe.get('compatible_for_this_probe') is not True
                or not isinstance(probe.get('checks'),dict)
                or set(probe['checks'])!={'function_call','nonempty_complete_response','tool_and_reasoning_replay'}
                or any(x is not True for x in probe['checks'].values())):
            raise ValueError('Probe did not verify the explicit Mock protocol')
        rows_by_protocol[protocol]={'rows':len(records),'statuses':['complete'],
            'inference_about_real_models':False}
    return rows_by_protocol


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--development-unsealed',action='store_true',
        help='For maintainers testing a changed tree; explicitly does NOT verify a distribution manifest')
    args=parser.parse_args()
    out=validate_destination(ROOT,args.out);out.mkdir(parents=True)
    (out/'logs').mkdir();(out/'runtime').mkdir();(out/'runtime/home').mkdir()
    summary={'schema':1,'profile':'rc5_5_zero_model_verification',
        'paid_model_requests_authorized':False,'real_model_calls':0,
        'real_model_calls_basis':'fixed Mock/local call graph plus accidental-egress guard; not provider billing telemetry',
        'supplier_billing_telemetry_available':False,
        'release_manifest_checked':not args.development_unsealed,
        'whole_tree_independent_review_passed':False,'paid_campaign_ready':False,
        'model_quality_improvement_established':False,'steps':[],
        'docker_harbor_executed':False,'external_sidecar_executed':False,
        'clean_npm_install_certified':False,'all_offline_operations_universally_completed':False}
    compiler=shutil.which('cc')
    if not sys.platform.startswith('linux') or compiler is None:
        summary.update(status='blocked',reason='Linux C socket guard unavailable; no unguarded test fallback')
        atomic_report(out/'summary.json',summary);return 2
    guard=out/'runtime/offline_socket_guard.so'
    with (out/'logs/guard_compile.log').open('x') as f:
        try:
            built=subprocess.run([compiler,'-shared','-fPIC','-O2','-Wall','-Wextra',
            str(ROOT/'scripts/offline_socket_guard.c'),'-o',str(guard),'-ldl'],stdout=f,stderr=subprocess.STDOUT,timeout=30)
        except (OSError,subprocess.TimeoutExpired) as exc:
            summary.update(status='blocked',reason='Network guard compiler failed',error=type(exc).__name__)
            atomic_report(out/'summary.json',summary);return 2
    if built.returncode:
        summary.update(status='blocked',reason='Network guard compilation failed')
        atomic_report(out/'summary.json',summary);return 2
    env=clean_environment(ROOT,out/'runtime/home',guard,out/'logs/nonloopback_blocks.log')
    steps=Steps(out,env)
    # Test the actual preloaded guard before executing any project code.
    test="""import socket,errno
s=socket.socket();s.settimeout(.1)
try:s.connect(('203.0.113.1',443))
except OSError as e:assert e.errno==errno.EACCES
else:raise AssertionError('Egress guard inactive')
finally:s.close()
s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1)
c=socket.create_connection(s.getsockname(),timeout=1);peer,_=s.accept()
c.close();peer.close();s.close();print('external blocked; loopback allowed')
"""
    if steps.run('guard_selftest',[sys.executable,'-c',test])['status']!='passed':
        summary.update(status='blocked',reason='Network guard self-test failed',steps=steps.rows)
        atomic_report(out/'summary.json',summary);return 2
    sys.path.insert(0,str(ROOT/'src'))
    from reverpi.util import source_manifest,digest
    initial_source=digest(source_manifest(ROOT));summary['source_sha256']=initial_source
    deps={};mismatches=[]
    for line in (ROOT/'requirements.lock').read_text().splitlines():
        if '==' not in line or line.lstrip().startswith('#'):continue
        name,pinned=line.split('==',1)
        try:actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:actual=None
        deps[name]={'expected':pinned,'installed':actual}
        if actual!=pinned:mismatches.append(name)
    atomic_report(out/'python_dependencies.json',deps)
    summary['python_dependency_pins_match']=not mismatches
    node=shutil.which('node');node_version=None
    if node:
        try:
            result=subprocess.run([node,'--version'],capture_output=True,text=True,env=env,timeout=10)
            node_version=result.stdout.strip() if result.returncode==0 else None
        except (OSError,subprocess.TimeoutExpired) as exc:
            summary['node_probe_error']=type(exc).__name__
    matched=re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)',node_version or '')
    node_ready=bool(matched and tuple(map(int,matched.groups())) >= (22,19,0))
    summary.update(python=sys.version.split()[0],node=node_version,
        docker_cli_available=shutil.which('docker') is not None,
        harbor_installed=importlib.util.find_spec('harbor') is not None)
    if not args.development_unsealed:
        r=steps.run('release_integrity',[sys.executable,'scripts/verify_release.py'])
        if r['status']!='passed':
            summary.update(status='blocked',reason='Distributed files changed',steps=steps.rows)
            atomic_report(out/'summary.json',summary);return 2
    if mismatches:
        summary.update(status='blocked',reason='Pinned Python dependencies do not match',steps=steps.rows)
        atomic_report(out/'summary.json',summary);return 2
    steps.run('python_full',[sys.executable,'-m','pytest','-q','-o','faulthandler_timeout=90',
        '-o',f'cache_dir={out}/runtime/pytest-cache','--junitxml',str(out/'pytest.xml')])
    steps.run('compile_all',[sys.executable,'-c',
        "from pathlib import Path; files=[p for d in ('src','tests','scripts') for p in Path(d).rglob('*.py')]; "
        "[compile(p.read_bytes(),str(p),'exec') for p in files]; print('compiled without pyc:',len(files))"])
    try:
        import xml.etree.ElementTree as ET
        suites=ET.parse(out/'pytest.xml').getroot()
        totals={key:sum(int(s.attrib.get(key,0)) for s in suites.iter('testsuite'))
                for key in ('tests','failures','errors')}
        if totals['tests']<=0 or totals['failures'] or totals['errors']:
            raise ValueError('Pytest report lacks successful executed tests')
        steps.rows.append({'name':'pytest_artifact_semantics','status':'passed',**totals})
    except Exception as exc:
        steps.rows.append({'name':'pytest_artifact_semantics','status':'failed','error':type(exc).__name__})
    if node_ready and (ROOT/'pi/node_modules/typescript/bin/tsc').is_file():
        steps.run('typescript',[node,'node_modules/typescript/bin/tsc','--noEmit'],cwd=ROOT/'pi')
        files=[str(p.relative_to(ROOT/'pi')) for p in sorted((ROOT/'pi/tests').glob('*.test.mjs'))]
        steps.run('node_contracts',[node,'--experimental-strip-types','--test',*files],cwd=ROOT/'pi')
        steps.run('native_revalidation',[sys.executable,'scripts/native_revalidation_smoke.py','--out',str(out/'native')])
        steps.run('native_compaction',[sys.executable,'scripts/native_compaction_smoke.py','--out',str(out/'native-compaction')],timeout=600)
        steps.run('native_compaction_semantics',[sys.executable,'scripts/native_compaction_audit.py','--run',str(out/'native-compaction'),'--out',str(out/'native-compaction-audit.json')])
        steps.run('native_compaction_negative_controls',[sys.executable,'scripts/contract_tamper_checks.py','--run',str(out/'native-compaction'),'--out',str(out/'native-compaction-negative-controls.json')])
        steps.run('network_delay',[sys.executable,'scripts/network_fixture.py','local','--out',str(out/'network'),'--node',node,'--delays','0,25'])
    else:
        steps.rows.append({'name':'native_node_checks','status':'blocked','reason':'Node >=22.19 and pinned node_modules required'})
    for name in ('historical','public-tests','campaign','review-cache'):
        steps.run(name,[sys.executable,'scripts/offline_fixture_checks.py',name,'--out',str(out/name)])
    steps.run('mechanism_witness',[sys.executable,'scripts/mechanism_demo.py','--out',str(out/'mechanism')])
    # Fixed explicit mock configs only. No arbitrary provider argument is accepted.
    import yaml
    for protocol in ('chat','responses'):
        provider=ROOT/f'configs/mock.{protocol}.yaml'
        if yaml.safe_load(provider.read_text()).get('mock') is not True:
            steps.rows.append({'name':f'mock_{protocol}','status':'failed','reason':'Mock flag not true'});continue
        steps.run(f'mock_probe_{protocol}',[sys.executable,'-m','reverpi','probe','--provider',str(provider),
            '--config','configs/pilot.yaml','--out',str(out/f'{protocol}-probe')])
        steps.run(f'mock_all12_{protocol}',[sys.executable,'-m','reverpi','run','--provider',str(provider),
            '--config','configs/smoke-all.yaml','--public','data/fixtures/public.jsonl',
            '--gold','data/fixtures/gold.jsonl','--out',str(out/f'{protocol}-all12')])
        steps.run(f'mock_analyze_{protocol}',[sys.executable,'-m','reverpi','analyze',
            '--run',str(out/f'{protocol}-all12'),'--baseline','full','--out',str(out/f'{protocol}-analysis.json')])
    # A zero exit code alone is not proof that a CLI entry point did any work.
    try:
        rows_by_protocol=verify_mock_artifacts(out)
        atomic_report(out/'mock_artifacts.json',rows_by_protocol)
        steps.rows.append({'name':'mock_artifact_semantics','status':'passed','rows_per_protocol':144})
    except Exception as exc:
        steps.rows.append({'name':'mock_artifact_semantics','status':'failed','error':type(exc).__name__})
    try:
        from offline_artifact_contracts import validate_profile_artifacts
        artifact_checks=validate_profile_artifacts(out)
        atomic_report(out/'fixed_profile_artifact_contracts.json', artifact_checks)
        steps.rows.append({'name':'fixed_profile_artifact_semantics','status':'passed',
            'families':artifact_checks['fixed_profile_families_verified']})
    except Exception as exc:
        steps.rows.append({'name':'fixed_profile_artifact_semantics','status':'failed','error':type(exc).__name__})
    summary['source_unchanged']=digest(source_manifest(ROOT))==initial_source
    summary['steps']=steps.rows
    blocked=out/'logs/nonloopback_blocks.log'
    count=len(blocked.read_text().splitlines()) if blocked.exists() else 0
    summary['blocked_nonloopback_syscalls_including_selftest']=count
    summary['unexpected_nonloopback_calls']=max(0,count-1)
    good=all(x['status']=='passed' for x in steps.rows) and summary['source_unchanged'] and count==1
    summary.update(executable_profile_passed=good,status='offline_profile_passed' if good else 'needs_attention',
        expected_research_blocks_preserved=['old_source_a_gold_schema','old_three_slot_budget_stress','no_paid_authority'],
        unverified=['clean_npm_ci','Docker/Harbor/sidecar execution','user-server process state',
                    'real-provider behavior','independent model review','agent performance'])
    atomic_report(out/'summary.json',summary)
    print(json.dumps({'status':summary['status'],'steps':len(steps.rows),'real_model_calls':0,'report':str(out/'summary.json')},indent=2))
    return 0 if good else 1


if __name__=='__main__':
    raise SystemExit(main())
