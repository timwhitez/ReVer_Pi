#!/usr/bin/env python3
"""Offline research CLI. There is no live dispatch or authorization subcommand."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from reverpi_study.safeio import load,exclusive_json,require
from reverpi_study.analysis import analyze
from reverpi_study.planning import plan,verify_plan

def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='cmd',required=True)
    a=s.add_parser('analyze');a.add_argument('--evidence',type=Path,required=True);a.add_argument('--out',type=Path,required=True)
    a=s.add_parser('plan');a.add_argument('--registry',type=Path,required=True);a.add_argument('--seed',required=True);a.add_argument('--source-sha256',required=True);a.add_argument('--fork',choices=['luna','flash'],default='luna');a.add_argument('--out',type=Path,required=True)
    a=s.add_parser('verify-plan');a.add_argument('--plan',type=Path,required=True)
    a=p.parse_args()
    if a.cmd=='analyze':
        require(not a.out.absolute().is_relative_to(a.evidence.absolute()),'Output must be outside evidence')
        r=analyze(a.evidence);exclusive_json(a.out,r);print(json.dumps({'status':'passed','new_model_calls':0,'completed_pairs':r['completed_pairs']}))
    elif a.cmd=='plan':
        r=plan(load(a.registry),seed=a.seed,source_sha256=a.source_sha256,fork=a.fork);exclusive_json(a.out,r);print(json.dumps({'status':'proposal_only','paid_authorized':False,'sha256':r['design_sha256']}))
    else:verify_plan(load(a.plan));print(json.dumps({'status':'internally_consistent','runtime_ready':False}))
    return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError) as e:
        print(type(e).__name__+': '+str(e),file=sys.stderr);raise SystemExit(2)
