#!/usr/bin/env python3
"""Self-contained RC7.1 offline profile, no legacy bulk-history dependency.

Runs all repository unit tests, real Node/TypeScript, real Pi with scripted local
Mock responses, raw artifact audits and negative controls. No live provider,
Docker, installation, budget authorization or model-effect certification.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from reverpi.util import atomic_write,canonical,digest,source_manifest,strict_json_loads
from verify_offline import clean_environment,validate_destination,Steps


def run(out:Path,unsealed=False):
    out=validate_destination(ROOT,out);out.mkdir(parents=True,mode=0o700)
    for name in ('logs','home','runtime'):(out/name).mkdir()
    r={'schema':1,'release':'RC7.1','status':'running','source_sha256':digest(source_manifest(ROOT)),
       'real_model_calls':0,'model_effect_verified':False,'independent_model_review_passed':False,
       'docker_harbor_run':False,'cold_install_this_run':False,'paid_authorized':False,'steps':[]}
    def persist():atomic_write(out/'summary.json',canonical(r))
    persist();cc=shutil.which('cc')
    if not sys.platform.startswith('linux') or not cc:
        r.update(status='blocked',reason='Requires Linux and a C compiler for local accidental-egress guard');persist();return r
    guard=out/'runtime/guard.so'
    with (out/'logs/guard_compile.log').open('x') as f:
        c=subprocess.run([cc,'-shared','-fPIC','-O2',str(ROOT/'scripts/offline_socket_guard.c'),'-ldl','-o',str(guard)],stdout=f,stderr=subprocess.STDOUT,timeout=30)
    if c.returncode:
        r.update(status='blocked',reason='Guard compilation failed');persist();return r
    env=clean_environment(ROOT,out/'home',guard,out/'guard.log');steps=Steps(out,env)
    def execute(name,args,timeout=600,cwd=ROOT):
        row=steps.run(name,args,cwd=cwd,timeout=timeout);r['steps']=steps.rows;persist()
        if row['status']!='passed':raise RuntimeError('Failed: '+name)
    try:
        if not unsealed:execute('release_manifest',[sys.executable,'scripts/verify_release.py'],120)
        execute('python_full',[sys.executable,'-m','pytest','-q','-o','cache_dir='+str(out/'runtime/pytest-cache'),'--junitxml='+str(out/'pytest.xml')],1200)
        suites=list(ET.parse(out/'pytest.xml').getroot().iter('testsuite'))
        r['python_tests']=sum(int(x.get('tests','0')) for x in suites)
        if not suites or any(int(x.get(k,'0')) for x in suites for k in ('failures','errors','skipped')):
            raise ValueError('Incomplete or failing Python JUnit record')
        node=shutil.which('node',path=env['PATH'])
        if not node:raise ValueError('Node executable missing')
        execute('node_full',[node,'--experimental-strip-types','--test',*[str(p) for p in sorted((ROOT/'pi/tests').glob('*.test.mjs'))]],300,ROOT/'pi')
        node_log=(out/'logs/node_full.log').read_text()
        import re
        passed=re.search(r'^# pass (\d+)$',node_log,re.M)
        if not passed or any(re.search(r'^# '+k+r' [1-9]\d*$',node_log,re.M) for k in ('fail','cancelled','skipped')):
            raise ValueError('Node TAP summary missing or incomplete')
        r['node_tests']=int(passed.group(1))
        execute('typescript',[node,'node_modules/typescript/bin/tsc','--noEmit'],120,ROOT/'pi')
        audited=[]
        # Test both protocols; retain legacy and split as distinct interfaces.
        for interface,protocol,pressure in (
            ('split_v1','responses','short'),('split_v1','responses','long'),
            ('split_v1','chat_completions','short'),('split_v1','chat_completions','long'),
            ('legacy','responses','long'),('legacy','chat_completions','long')):
            label=interface+'_'+protocol+'_'+pressure;pd=out/(label+'_plan');case=out/label
            execute(label+'_prepare',[sys.executable,'scripts/paired_prefix_canary.py','prepare','--out',str(pd),
                '--protocol',protocol,'--pressure',pressure,'--seed','rc71-frozen-'+label,'--recovery-interface',interface],30)
            sha=(pd/'PLAN.sha256').read_text().strip()
            execute(label+'_native',[sys.executable,'scripts/paired_prefix_canary.py','run','--plan',str(pd/'PLAN.json'),
                '--plan-sha256',sha,'--out',str(case)],600)
            af=out/(label+'_audit.json')
            execute(label+'_audit',[sys.executable,'scripts/verify_paired_prefix.py','--run',str(case),'--out',str(af)],90)
            a=strict_json_loads(af.read_bytes());expected='paired_complete' if pressure=='long' else 'no_eligible_prefix'
            if a['status']!='passed' or a['run_status']!=expected or not a['mock']:raise ValueError('Unexpected native contract result')
            if pressure=='long':
                proj=next(x for x in a['phases'] if x['phase']=='projected')
                if proj['exact_recovery_calls']!=1 or proj['marker_exact'] is not True or not a['first_pair_identical_except_projection']:
                    raise ValueError('Missing native recovery or matched first branch')
            audited.append({'case':label,**a});r['native_audits']=audited;persist()
        execute('paired_tamper',[sys.executable,'scripts/paired_prefix_tamper.py','--run',str(out/'split_v1_responses_long'),'--out',str(out/'tamper')],180)
        t=strict_json_loads((out/'tamper/report.json').read_bytes())
        if t['status']!='passed' or len(t['cases'])!=12 or not all(x['rejected'] for x in t['cases']):raise ValueError('Incomplete negative controls')
        r['tamper_rejected']=12
        live=ROOT/'evidence/RC7_Live_Pilot_Evidence/live_run'
        execute('prior_live_readonly',[sys.executable,'scripts/analyze_rc7_pilot.py','--run',str(live),'--out',str(out/'prior_live_analysis.json')],120)
        old=strict_json_loads((out/'prior_live_analysis.json').read_bytes())
        outcomes={x['phase']:x['completion_by_frozen_cap'] for x in old['budgeted_outcomes']['arms']}
        if old['experiment_observed_tokens']!=142559 or outcomes!={'full':1,'projected':0} or not old['schema_runtime_witnesses']:
            raise ValueError('Prior evidence changed or expected witness absent')
        r.update(status='passed',prior_live_calls_reaudited=15,new_model_calls_in_prior_analysis=0)
    except Exception as e:r.update(status='failed',error_type=type(e).__name__,reason=str(e))
    r['final_source_sha256']=digest(source_manifest(ROOT));r['source_unchanged']=r['source_sha256']==r['final_source_sha256']
    r['nonloopback_guard_events']=(out/'guard.log').read_text() if (out/'guard.log').exists() else ''
    if not r['source_unchanged'] or r['nonloopback_guard_events']:r['status']='failed'
    persist();return r


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--development-unsealed',action='store_true',help='Maintainer only: skip delivery manifest, never tests')
    a=p.parse_args();r=run(a.out,a.development_unsealed);print(canonical({'status':r['status'],'source_unchanged':r.get('source_unchanged'),'real_model_calls':0}));return 0 if r['status']=='passed' else 2
if __name__=='__main__':raise SystemExit(main())
