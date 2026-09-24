#!/usr/bin/env python3
"""Full frozen zero-model RC7 profile: regression, real Pi/Mock pairing, audits.

No Docker, installation, live route, provider credential or paid authorization.
Uses the repository's dynamic-process accidental-egress guard (not a hostile
code sandbox). Use a fresh directory outside the source. See summary.json and
per-command logs, not merely the outer process exit code.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from reverpi.util import canonical,digest,source_manifest,atomic_write,strict_json_loads
from verify_offline import clean_environment,validate_destination,Steps


def run(out:Path,unsealed=False):
    out=validate_destination(ROOT,out);out.mkdir(parents=True,mode=0o700)
    for name in ('logs','home','runtime'):(out/name).mkdir()
    summary={'schema':1,'kind':'rc7.offline-profile','status':'running',
             'source_sha256':digest(source_manifest(ROOT)),'real_model_calls':0,'real_model_fees':0,
             'model_quality_verified':False,'docker_harbor_rc7_run':False,'paid_authorized':False,
             'dependency_installation_this_run':False,'steps':[]}
    def persist():atomic_write(out/'summary.json',canonical(summary))
    persist()
    compiler=shutil.which('cc')
    if not sys.platform.startswith('linux') or not compiler:
        summary.update(status='blocked',reason='Linux C compiler required');persist();return summary
    guard=out/'runtime/guard.so'
    with (out/'logs/guard_compile.log').open('x') as log:
        result=subprocess.run([compiler,'-shared','-fPIC','-O2',str(ROOT/'scripts/offline_socket_guard.c'),'-ldl','-o',str(guard)],stdout=log,stderr=subprocess.STDOUT,timeout=30)
    if result.returncode:
        summary.update(status='blocked',reason='Guard compile failed');persist();return summary
    env=clean_environment(ROOT,out/'home',guard,out/'guard.log');steps=Steps(out,env)
    def execute(name,args,timeout):
        row=steps.run(name,args,cwd=ROOT,timeout=timeout);summary['steps']=steps.rows;persist()
        if row['status']!='passed':raise RuntimeError('Step failed: '+name)
    try:
        base=[sys.executable,'scripts/verify_rc6.py','--out',str(out/'legacy')]
        if unsealed:base.append('--development-unsealed')
        execute('inherited_full_profile',base,1800)
        legacy=strict_json_loads((out/'legacy/summary.json').read_bytes())
        if legacy['status']!='passed' or legacy['source_unchanged'] is not True:
            raise ValueError('Inherited profile did not meet its artifact contract')
        audited=[]
        for protocol in ('chat_completions','responses'):
            for pressure in ('short','long'):
                label=protocol+'_'+pressure;plan_dir=out/(label+'_plan');case=out/label
                execute(label+'_prepare',[sys.executable,'scripts/paired_prefix_canary.py','prepare','--out',str(plan_dir),
                        '--protocol',protocol,'--pressure',pressure,'--seed','rc7-frozen-'+label],30)
                sha=(plan_dir/'PLAN.sha256').read_text().strip()
                execute(label+'_native',[sys.executable,'scripts/paired_prefix_canary.py','run','--plan',str(plan_dir/'PLAN.json'),
                        '--plan-sha256',sha,'--out',str(case)],600)
                execute(label+'_audit',[sys.executable,'scripts/verify_paired_prefix.py','--run',str(case),'--out',str(out/(label+'_audit.json'))],60)
                a=strict_json_loads((out/(label+'_audit.json')).read_bytes());audited.append(a)
                expected='paired_complete' if pressure=='long' else 'no_eligible_prefix'
                if a['status']!='passed' or a['run_status']!=expected or not a['mock']:
                    raise ValueError('Fixed positive/negative pairing contract changed')
        execute('paired_tamper',[sys.executable,'scripts/paired_prefix_tamper.py','--run',str(out/'responses_long'),
                '--out',str(out/'tamper')],120)
        negative=strict_json_loads((out/'tamper/report.json').read_bytes())
        if negative['status']!='passed' or len(negative['cases'])!=12 or not all(x['rejected'] for x in negative['cases']):
            raise ValueError('Negative control coverage incomplete')
        xml=ET.parse(out/'legacy/legacy-profile/pytest.xml').getroot()
        suites=list(xml.iter('testsuite'))
        summary.update(status='passed',paired_case_audits=audited,tamper_rejected=12,
                       python_tests=sum(int(x.get('tests','0')) for x in suites),
                       python_failures=sum(int(x.get('failures','0'))+int(x.get('errors','0')) for x in suites))
    except Exception as err:
        summary.update(status='failed',error_type=type(err).__name__,reason=str(err))
    summary['final_source_sha256']=digest(source_manifest(ROOT))
    summary['source_unchanged']=summary['source_sha256']==summary['final_source_sha256']
    summary['nonloopback_guard_events']=(out/'guard.log').read_text() if (out/'guard.log').exists() else ''
    if not summary['source_unchanged'] or summary['nonloopback_guard_events']:summary['status']='failed'
    persist();return summary


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--development-unsealed',action='store_true',help='Maintainer only; skip delivery manifest, never tests')
    a=p.parse_args();r=run(a.out,a.development_unsealed);print(canonical({'status':r['status'],'source_unchanged':r['source_unchanged'],'real_model_calls':0}));return 0 if r['status']=='passed' else 2
if __name__=='__main__':raise SystemExit(main())
