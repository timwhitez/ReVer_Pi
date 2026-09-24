#!/usr/bin/env python3
"""Complete S4 non-Provider validation. All native actors are scripted Mock.

Default includes inherited complete RC7.1 profile. --skip-core is only for an
incremental maintainer check; reports it explicitly and does not certify core.
Output must be fresh and outside the source. Linux C egress guard required.
"""
from pathlib import Path
import argparse,json,os,shutil,subprocess,sys,xml.etree.ElementTree as ET
R=Path(__file__).resolve().parent;ROOT=R.parents[1]
for p in (ROOT/'src',ROOT/'scripts',R):sys.path.insert(0,str(p))
from reverpi.util import canonical,atomic_write,digest,source_manifest,strict_json_loads
from reverpi_sources.contracts import new_output,research_manifest
from verify_offline import Steps,clean_environment

def verify(out,skip_core=False,unsealed=False):
    out=new_output(out);out.mkdir(parents=True,mode=0o700)
    for n in ['logs','runtime','home']: (out/n).mkdir()
    initial={'core':digest(source_manifest(ROOT)),'research':digest(research_manifest())}
    result={'schema':1,'status':'running','initial_identity':initial,'core_profile_executed':not skip_core,'source_manifest_checked':not unsealed,
            'real_model_calls':0,'model_quality_verified':False,'cold_install':False,'docker_harbor':False,'independent_model_review':False,'cases':[]}
    def save():atomic_write(out/'summary.json',canonical(result))
    save()
    cc=shutil.which('cc')
    if not cc or not sys.platform.startswith('linux'):raise RuntimeError('Linux/C required for accidental-egress guard')
    guard=out/'runtime/guard.so'
    subprocess.run([cc,'-shared','-fPIC','-O2',str(ROOT/'scripts/offline_socket_guard.c'),'-ldl','-o',str(guard)],check=True,timeout=30)
    env=clean_environment(ROOT,out/'home',guard,out/'guard.log');env['TERM']='dumb'
    steps=Steps(out,env)
    def execute(name,args,timeout=600,cwd=ROOT):
        row=steps.run(name,[str(x) for x in args],timeout=timeout,cwd=cwd);result['steps']=steps.rows;save()
        if row['status']!='passed':raise RuntimeError('Failed: '+name)
    try:
        if not unsealed:execute('release',[sys.executable,'scripts/verify_release.py'])
        if not skip_core:
            args=[sys.executable,'scripts/verify_rc71.py','--out',out/'core']
            if unsealed:args.append('--development-unsealed')
            execute('inherited_core',args,1800)
            core=strict_json_loads((out/'core/summary.json').read_bytes())
            if core['status']!='passed':raise RuntimeError('Core artifacts failed')
            result['core']={k:core[k] for k in ['python_tests','node_tests','status','source_unchanged','nonloopback_guard_events']}
        execute('s3_tests',[sys.executable,'-m','pytest','research/s3/tests','-q','-o','cache_dir='+str(out/'runtime/s3cache'),'--junitxml='+str(out/'s3.xml')])
        execute('s4_tests',[sys.executable,'-m','pytest','research/s4/tests','-q','-o','cache_dir='+str(out/'runtime/s4cache'),'--junitxml='+str(out/'s4.xml')])
        for v in ('s3','s4'):
            suites=list(ET.parse(out/(v+'.xml')).getroot().iter('testsuite'))
            if not suites or any(int(s.get(k,'0')) for s in suites for k in ['failures','errors','skipped']):raise RuntimeError('Incomplete research tests')
            result[v+'_tests']=sum(int(s.get('tests','0')) for s in suites)
        node=shutil.which('node',path=env['PATH'])
        execute('s4_node',[node,'--experimental-strip-types','--test',R/'pi/readonly.test.mjs'])
        if '# pass 7' not in (out/'logs/s4_node.log').read_text() or '# fail 0' not in (out/'logs/s4_node.log').read_text():raise RuntimeError('Node summary invalid')
        result['s4_node_tests']=7
        execute('s4_types',[node,ROOT/'pi/node_modules/typescript/bin/tsc','-p',R/'pi/tsconfig.json'])
        execute('oracle',[sys.executable,R/'verify_sources.py','--out',out/'oracles.json'])
        execute('numerical',[sys.executable,R/'crosscheck.py','--out',out/'numerical.json'])
        for taskfile in sorted((R/'data/tasks').glob('*.json')):
            task=taskfile.stem
            for protocol in ('responses','chat_completions'):
                for profile in ('quick','walk'):
                    label=task+'_'+protocol+'_'+profile;pd=out/(label+'_plan');run=out/label
                    execute(label+'_prepare',[sys.executable,R/'run.py','prepare','--task',task,'--protocol',protocol,'--scripted-profile',profile,'--out',pd],60)
                    sha=(pd/'PLAN.sha256').read_text().strip()
                    execute(label+'_native',[sys.executable,R/'run.py','run','--plan',pd/'PLAN.json','--plan-sha256',sha,'--out',run],480)
                    execute(label+'_audit',[sys.executable,R/'run.py','audit','--run',run,'--out',out/(label+'_audit.json')],120)
                    execute(label+'_score',[sys.executable,R/'run.py','score','--run',run,'--gold',R/'data/controller'/f'{task}.gold.json','--out',out/(label+'_score.json')],120)
                    execute(label+'_analysis',[sys.executable,R/'run.py','analyze','--run',run,'--gold',R/'data/controller'/f'{task}.gold.json','--out',out/(label+'_analysis.json')],120)
                    analyzed=strict_json_loads((out/(label+'_analysis.json')).read_bytes())
                    if analyzed['candidate_training_record'] is not None or analyzed['currency_cost'] is not None:raise RuntimeError('Mock mislabeled as training or currency evidence')
                    a=strict_json_loads((out/(label+'_audit.json')).read_bytes());score=strict_json_loads((out/(label+'_score.json')).read_bytes())
                    expected='no_eligible_prefix' if profile=='quick' else 'paired_complete'
                    if a['run_status']!=expected or not a['mock'] or any(x['correct'] for x in score['rows']):raise RuntimeError('Mock source contract mis-scored')
                    if profile=='walk' and not a['first_pair_identical_except_projection']:raise RuntimeError('Representation fork missing')
                    result['cases'].append({'case':label,'task':task,'protocol':protocol,'scripted_profile':profile,'audit':a,'score':score})
                    save()
        execute('tamper',[sys.executable,R/'tamper.py','--run',out/'cache-behavior_responses_walk','--out',out/'tamper'],240)
        t=strict_json_loads((out/'tamper/report.json').read_bytes())
        if t['status']!='passed' or len(t['cases'])!=14 or not all(x['rejected'] for x in t['cases']):raise RuntimeError('Negative checks incomplete')
        result['tamper_rejected']=14;result['status']='passed'
    except Exception as e:result.update(status='failed',error=type(e).__name__,reason=str(e))
    result['final_identity']={'core':digest(source_manifest(ROOT)),'research':digest(research_manifest())}
    result['identity_unchanged']=result['final_identity']==initial
    result['nonloopback_attempts']=(out/'guard.log').read_text() if (out/'guard.log').exists() else ''
    if not result['identity_unchanged'] or result['nonloopback_attempts']:result['status']='failed'
    save();return result
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--skip-core',action='store_true');p.add_argument('--development-unsealed',action='store_true');a=p.parse_args()
    r=verify(a.out,a.skip_core,a.development_unsealed);print(canonical({'status':r['status'],'cases':len(r['cases']),'real_model_calls':0}));raise SystemExit(0 if r['status']=='passed' else 2)
