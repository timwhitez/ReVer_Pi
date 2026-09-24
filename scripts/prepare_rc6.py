#!/usr/bin/env python3
"""Create fresh owned fixtures and opt-in configurations. NEVER calls a provider.

The example live profile is a proposal, not authorization. This command does not
inspect credentials, instantiate APIClient, build workers, start containers,
select held-out tasks, or change old files. New paid work needs a separate frozen
plan and explicit external authorization; no balance or certificate migrates.
"""
from __future__ import annotations
import argparse,hashlib,sys,tomllib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from reverpi.config import StudyConfig
from reverpi.devtasks import generate_tasks,validate_oracles
from reverpi.util import canonical,digest,source_manifest,atomic_write
from verify_offline import validate_destination


def prepare(out:Path,image:str='python:3.11-slim')->dict:
    out=validate_destination(ROOT,out);out.mkdir(parents=True,mode=0o700)
    result={'schema':1,'kind':'rc6_environment_preparation','status':'running','source_sha':digest(source_manifest(ROOT)),
            'model_calls':0,'paid_authorized':False,'g4_authorized':False,'token_budget_authorized':0,
            'independent_benchmark':False,'docker_tested_here':False,'natural_model_behavior_verified':False}
    atomic_write(out/'report.json',canonical(result))
    owned=generate_tasks(out/'owned-tasks',image=image);oracles=validate_oracles(out/'owned-tasks')
    tasks=[]
    for path in sorted((out/'owned-tasks').glob('*/task.toml')):
        t=tomllib.loads(path.read_text());name=t['task']['name']
        if not name.startswith('rever-owned/') or t['environment']['network_mode']!='no-network':
            raise ValueError('Generated task name/network differs from the owned contract')
        tasks.append({'name':name,'task_toml':str(path.relative_to(out)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    configs=[]
    for name,method,mode in [('pi_original','pi_original','off'),('tools_observe','mask','observe'),('online_apply','mask','apply')]:
        st=StudyConfig(name='rc6-'+name,methods=[method],online_projection={'mode':mode},compression={'recovery_search_mode':'match'})
        path=out/f'configs/{name}.json';atomic_write(path,canonical(st.model_dump()));configs.append({'condition':name,'path':str(path.relative_to(out)),'study_sha':digest(st.model_dump())})
    proposal={'schema':1,'status':'NOT_AUTHORIZED','model':'deepseek-flash','effort':'low','concurrency':1,
              'protocol':'chat_completions','source_sha':result['source_sha'],'conditions':configs,
              'native_versions':{'Pi':'0.84.2','Harbor':'0.22.0','Node_minimum':'22.19.0'},
              'output_reservation_tokens':65536,'native_context_window':131072,
              'planned_paid_units':0,'token_budget_authorized':0,'automatic_budget_rollover':False,
              'prerequisites':['new-source Docker scripted online profile','original raw wire compatibility with approved provider',
                  'explicit operator budget and immutable run identity','task gold and source isolation','predeclared denominator and endpoint'],
              'warnings':['No claim that a configured reservation is an upstream physical billing cap.',
                          'Prepared configurations are not a complete funded live plan.',
                          'Do not run the owned reference solutions as a model performance experiment.']}
    atomic_write(out/'LIVE_PROPOSAL_NOT_AUTHORIZED.json',canonical(proposal))
    result.update(status='passed',owned_generation=owned,oracles=oracles,tasks=tasks,configs=configs,
                  image=image,image_digest_pinned='@sha256:' in image)
    atomic_write(out/'report.json',canonical(result));return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--image',default='python:3.11-slim');a=p.parse_args()
    r=prepare(a.out,a.image);print(canonical({'status':r['status'],'tasks':len(r['tasks']),'paid_authorized':False}));return 0
if __name__=='__main__':raise SystemExit(main())
