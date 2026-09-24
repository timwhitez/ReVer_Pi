#!/usr/bin/env python3
"""S4 inspected-source pipeline; default operations are local or scripted Mock."""
from pathlib import Path
import argparse,asyncio,hashlib,sys
ROOT=Path(__file__).resolve().parents[2]
for path in [ROOT/'src',ROOT/'scripts',Path(__file__).resolve().parent]:sys.path.insert(0,str(path))
from reverpi.util import canonical,strict_json_loads,atomic_create,digest
from reverpi.paired_prefix import require
from reverpi_sources.contracts import SourcePlan,load_task,read_gold,score_answer,S4,new_output
from reverpi_sources.runner import prepare,run,authorization_intent,check_authorization
from reverpi_sources.audit import audit

def score(run_path,gold_path):
    record=audit(run_path);plan=SourcePlan.model_validate(strict_json_loads((run_path/'PLAN.json').read_bytes()));task=load_task(run_path/'TASK.json')
    golden=read_gold(gold_path,task,plan.gold_sha256);summary=strict_json_loads((run_path/'summary.json').read_bytes())
    rows=[]
    for p in summary['phases']:
        if p['phase']=='capture' and p['state']=='boundary_captured':continue
        answer=p.get('answer') if p['state']=='completed' else None
        rows.append({'phase':p['phase'],'run_state':p['state'],**score_answer(answer,task,golden)})
    return {'schema':'reverpi.s4.score.v1','plan_sha256':record['plan_sha'],'task_id':task['task_id'],'task_sha256':task['task_sha256'],'source_group':task['source_group'],'split':'development','mock':plan.provider.mock,'run_status':summary['status'],'rows':rows,
            'gold_sha256':plan.gold_sha256,'population_success_established':False,'no_eligible_is_not_paired_failure':True,
            'budget_cutoff_is_not_observed_wrong_final_answer':True}

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('prepare');a.add_argument('--out',type=Path,required=True);a.add_argument('--task',required=True)
    a.add_argument('--protocol',choices=['responses','chat_completions'],default='responses');a.add_argument('--scripted-profile',choices=['quick','walk'],default='walk')
    a.add_argument('--provider',type=Path);a.add_argument('--budget',type=Path);a.add_argument('--seed',default='s4-development-v1')
    a.add_argument('--prefix-cap',type=int,default=12);a.add_argument('--suffix-cap',type=int,default=12)
    a=sub.add_parser('run');a.add_argument('--plan',type=Path,required=True);a.add_argument('--plan-sha256',required=True);a.add_argument('--out',type=Path,required=True)
    a.add_argument('--allow-paid',action='store_true');a.add_argument('--acknowledge-local-readonly',action='store_true');a.add_argument('--authorization',type=Path)
    for cmd in ['audit','score','analyze']:
        a=sub.add_parser(cmd);a.add_argument('--run',type=Path,required=True);a.add_argument('--out',type=Path,required=True)
        if cmd in {'score','analyze'}:a.add_argument('--gold',type=Path,required=True)
    a=sub.add_parser('bind-approval');a.add_argument('--proposal',type=Path,required=True);a.add_argument('--authorization',type=Path,required=True);a.add_argument('--out',type=Path,required=True)
    a=sub.add_parser('fit-rule');a.add_argument('--records',type=Path,required=True);a.add_argument('--out',type=Path,required=True)
    a=sub.add_parser('calibrate-rule');a.add_argument('--candidate',type=Path,required=True);a.add_argument('--records',type=Path,required=True);a.add_argument('--risk-margin',type=float);a.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.cmd in {'fit-rule','calibrate-rule'}:
        from reverpi_sources.selector import fit,calibrate
        out=new_output(a.out);rows=strict_json_loads(a.records.read_bytes())
        result=fit(rows) if a.cmd=='fit-rule' else calibrate(strict_json_loads(a.candidate.read_bytes()),rows,risk_margin=a.risk_margin)
        atomic_create(out,canonical(result));print(canonical(result));return 0
    if a.cmd=='prepare':
        result=prepare(a.out,a.task,a.protocol,a.scripted_profile,a.provider,a.budget,a.seed,a.prefix_cap,a.suffix_cap)
        print(canonical({'plan_sha256':digest(result.model_dump()),'paid_authorized':False}));return 0
    if a.cmd=='run':
        result=asyncio.run(run(a.plan,a.plan_sha256,a.out,allow_paid=a.allow_paid,acknowledge_local=a.acknowledge_local_readonly,authorization=a.authorization))
        print(canonical({'status':result['status'],'mock':result['provider_mock'],'central_cost':result['central_cost']}));return 0 if result['status'] in {'paired_complete','no_eligible_prefix'} else 2
    if a.cmd=='bind-approval':
        old=SourcePlan.model_validate(strict_json_loads(a.proposal.read_bytes()));require(not old.provider.mock and not old.paid_authorized,'Expected unapproved live proposal')
        value=old.model_dump();value.update(paid_authorized=True,authorization_digest=hashlib.sha256(a.authorization.read_bytes()).hexdigest());plan=SourcePlan.model_validate(value);check_authorization(plan,a.authorization)
        out=new_output(a.out);out.mkdir(parents=True);atomic_create(out/'PLAN.json',canonical(plan.model_dump()));atomic_create(out/'PLAN.sha256',digest(plan.model_dump())+'\n');return 0
    out=new_output(a.out);require(not out.is_relative_to(a.run.resolve()),'Do not write into original evidence')
    if a.cmd=='analyze':
        from reverpi_sources.analysis import analyze
        result=analyze(a.run,a.gold)
    else:result=audit(a.run) if a.cmd=='audit' else score(a.run,a.gold)
    atomic_create(out,canonical(result));print(canonical(result));return 0
if __name__=='__main__':raise SystemExit(main())
