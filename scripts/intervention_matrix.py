#!/usr/bin/env python3
"""Freeze/run a four-arm, common-budget development matrix. Never reads gold."""
from pathlib import Path
import sys, argparse, asyncio, json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from reverpi.config import Provider,Budget,CompressionConfig,load
from reverpi.intervention_matrix import prepare_matrix,execute_matrix
from reverpi.errors import LabError

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='cmd',required=True)
    f=sub.add_parser('prepare')
    for key in ('parent','workspace','registry','provider','budget','compression','out'):f.add_argument('--'+key,type=Path,required=True)
    for key in ('parent-sha256','registry-sha256'):f.add_argument('--'+key,required=True)
    f.add_argument('--repeats',type=int,default=2);f.add_argument('--seed',type=int,default=20260919)
    f.add_argument('--max-turns',type=int,default=6);f.add_argument('--recovery-calls',type=int,default=3);f.add_argument('--revalidation-calls',type=int,default=2)
    f.add_argument('--evaluation-gold-sha256',help='Only a SHA-256 commitment; this runner never reads gold')
    f.add_argument('--interrupted-policy',choices=['halt','quarantine_and_continue'],default='halt')
    r=sub.add_parser('run');r.add_argument('--root',type=Path,required=True);r.add_argument('--plan-sha256',required=True)
    r.add_argument('--allow-paid',action='store_true');r.add_argument('--acknowledge-unsandboxed',action='store_true')
    a=p.parse_args()
    try:
        if a.cmd=='prepare':
            result=prepare_matrix(a.out,a.parent,a.parent_sha256,load(a.provider,Provider),load(a.budget,Budget),
                load(a.compression,CompressionConfig),workspace=a.workspace,registry=a.registry,registry_sha=a.registry_sha256,
                repeats=a.repeats,seed=a.seed,max_turns=a.max_turns,recovery_calls=a.recovery_calls,revalidation_calls=a.revalidation_calls,
                interrupted_policy=a.interrupted_policy,evaluation_gold_sha256=a.evaluation_gold_sha256)
        else:result=asyncio.run(execute_matrix(a.root,a.plan_sha256,allow_paid=a.allow_paid,acknowledge_unsandboxed=a.acknowledge_unsandboxed))
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (LabError,ValueError,OSError,TypeError,KeyError) as exc:
        print(json.dumps({'error':exc.kind if isinstance(exc,LabError) else type(exc).__name__,
                          'message':'Matrix stopped; preserve its ledger and all partial units.'}),file=sys.stderr)
        raise SystemExit(2)
if __name__=='__main__':main()
