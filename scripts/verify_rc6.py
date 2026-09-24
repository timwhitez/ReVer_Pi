#!/usr/bin/env python3
"""Run the complete fixed RC6 zero-model profile, then independently audit it.

Uses fixed MockTransport only; nonloopback guard is for accidental connections,
not a malicious-code sandbox. No dependency installation or Docker invocation.
Requires the pinned local Python/Node/Pi dependencies. Missing capabilities are
reported, never substituted with real endpoints or upgraded packages.
"""
from __future__ import annotations
import argparse,json,shutil,sys,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from reverpi.util import canonical,digest,source_manifest,atomic_write
from verify_offline import clean_environment,validate_destination,Steps


def validate_summaries(old:dict,audited:dict,negative:dict,prepared:dict,source_sha:str)->None:
    if (old.get('status')!='offline_profile_passed' or old.get('executable_profile_passed') is not True
        or old.get('source_unchanged') is not True or old.get('source_sha256')!=source_sha
        or old.get('unexpected_nonloopback_calls')!=0
        or not old.get('steps') or any(x.get('status')!='passed' for x in old['steps'])):
        raise ValueError('Legacy profile has not satisfied its exact artifact contract')
    if (audited.get('status')!='passed' or audited.get('cells')!=12 or audited.get('source_sha')!=source_sha
        or audited.get('real_model_quality_verified') is not False or audited.get('scripted_actor_only') is not True):
        raise ValueError('Online independent audit is incomplete or overclaims')
    expected={'quality_overclaim','missing_case','manual_compact','source_marker','archive_corrupt',
              'counter_consistent_reseal','changed_reason_consistent_reseal','pending_sidecar','actual_wire','tool_schema'}
    cases=negative.get('cases',[])
    if (negative.get('status')!='passed' or negative.get('source_sha')!=source_sha or len(cases)!=10
        or {x.get('case') for x in cases}!=expected or any(x.get('rejected') is not True for x in cases)):
        raise ValueError('Negative control coverage or outcomes changed')
    if (prepared.get('status')!='passed' or len(prepared.get('tasks',[]))!=6
        or prepared.get('source_sha')!=source_sha or prepared.get('paid_authorized') is not False):
        raise ValueError('Prepared fixture/authority contract changed')


def run(out:Path,development:bool=False)->dict:
    out=validate_destination(ROOT,out);out.mkdir(parents=True,mode=0o700)
    for d in ('logs','runtime','home'):(out/d).mkdir(mode=0o700)
    summary={'schema':1,'kind':'rc6_zero_model_profile','status':'running','source_sha':digest(source_manifest(ROOT)),
             'real_model_calls':0,'real_model_call_basis':'fixed Mock/local call graph and accidental-egress guard; not provider billing telemetry',
             'paid_authorized':False,'whole_tree_independent_review_passed':False,'g4_authorized':False,
             'real_model_quality_verified':False,'native_threshold_policy_verified':False,
             'automatic_observation_projection_scripted_only':True,'docker_harbor_rc6_tested':False,
             'clean_dependency_installation_here':False,'steps':[]}
    atomic_write(out/'summary.json',canonical(summary))
    compiler=shutil.which('cc')
    if not sys.platform.startswith('linux') or not compiler:
        summary.update(status='blocked',reason='Linux C compiler required for the accidental-egress guard');atomic_write(out/'summary.json',canonical(summary));return summary
    guard=out/'runtime/guard.so'
    with (out/'logs/guard_compile.log').open('x') as stream:
        r=subprocess.run([compiler,'-shared','-fPIC','-O2',str(ROOT/'scripts/offline_socket_guard.c'),'-ldl','-o',str(guard)],stdout=stream,stderr=subprocess.STDOUT,timeout=30)
    if r.returncode:
        summary.update(status='blocked',reason='Guard compilation failed');atomic_write(out/'summary.json',canonical(summary));return summary
    env=clean_environment(ROOT,out/'home',guard,out/'guard.log');steps=Steps(out,env)
    baseline=[sys.executable,'scripts/verify_offline.py','--out',str(out/'legacy-profile')]
    if development:baseline+=['--development-unsealed']
    jobs=[('legacy_full_profile',baseline,1800),
          ('online_single_prompt',[sys.executable,'scripts/online_projection_smoke.py','--out',str(out/'online')],600),
          ('online_independent_audit',[sys.executable,'scripts/online_projection_audit.py','--run',str(out/'online'),'--out',str(out/'online-audit.json')],120),
          ('online_negative_controls',[sys.executable,'scripts/online_projection_tamper.py','--run',str(out/'online'),'--out',str(out/'online-tamper')],180),
          ('fresh_owned_environment',[sys.executable,'scripts/prepare_rc6.py','--out',str(out/'prepared')],180)]
    for name,argv,timeout in jobs:
        row=steps.run(name,argv,cwd=ROOT,timeout=timeout);summary['steps']=steps.rows
        atomic_write(out/'summary.json',canonical(summary))
        if row['status']!='passed':
            summary.update(status='failed',stopped_at=name);break
    else:
        old=json.loads((out/'legacy-profile/summary.json').read_text())
        audited=json.loads((out/'online-audit.json').read_text());negative=json.loads((out/'online-tamper/report.json').read_text());prepared=json.loads((out/'prepared/report.json').read_text())
        try:validate_summaries(old,audited,negative,prepared,summary['source_sha'])
        except (ValueError,TypeError,KeyError) as error:
            summary.update(status='failed',reason=str(error))
        else:summary.update(status='passed',legacy_steps=len(old.get('steps',[])),online_cells=12,tamper_rejected=10,
                            owned_tasks=6,online_audit=audited)
    summary['final_source_sha']=digest(source_manifest(ROOT));summary['source_unchanged']=summary['source_sha']==summary['final_source_sha']
    if not summary['source_unchanged']:summary.update(status='failed',reason='Source changed during verification')
    atomic_write(out/'summary.json',canonical(summary));return summary


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--development-unsealed',action='store_true',help='Maintainer only: skip distribution manifest verification, not tests')
    a=p.parse_args();r=run(a.out,a.development_unsealed);print(canonical({'status':r['status'],'real_model_calls':0,'source_unchanged':r.get('source_unchanged')}));return 0 if r['status']=='passed' else 2
if __name__=='__main__':raise SystemExit(main())
