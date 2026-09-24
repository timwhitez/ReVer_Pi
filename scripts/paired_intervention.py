#!/usr/bin/env python3
"""Prepare a fixed-memory development checkpoint or run one explicitly budgeted arm.

The prepare command never calls a model. Run accepts both supported Provider
protocols and refuses live use without --allow-paid. Use one common ledger for
all arms of a parent to enforce their aggregate budget. Interrupted arm folders
are evidence: no automatic replay or overwriting is supported.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from reverpi.config import Provider,Budget,CompressionConfig,load
from reverpi.data import Checkpoint
from reverpi.memory import Memory
from reverpi.ledger import Ledger
from reverpi.transport import APIClient
from reverpi.paired_interventions import Arm,freeze_intervention,read_intervention,run_arm,intervention_cell
from reverpi.revalidation import load_registry
from reverpi.util import digest,strict_json_loads
from reverpi.errors import LabError


async def run(args):
    provider=load(args.provider,Provider);budget=load(args.budget,Budget)
    if not provider.mock and not args.allow_paid:raise ValueError('Live use requires --allow-paid and a frozen budget')
    if args.out.exists():raise ValueError('Use a fresh arm directory; never erase an interrupted arm')
    read_intervention(args.parent,args.parent_sha256)
    arm=Arm(args.arm,args.recovery_calls,args.revalidation_calls,args.max_turns)
    verifiers=None
    if args.revalidation_calls:
        if not args.acknowledge_task_sandbox or not args.workspace or not args.registry or not args.registry_sha256:
            raise ValueError('Revalidation requires a disposable task sandbox, workspace, and SHA-pinned registry')
        verifiers=load_registry(args.registry,args.registry_sha256)
    ledger=Ledger(args.ledger,budget)
    cell=intervention_cell(args.parent_sha256, arm, trial_id=args.trial_id)
    with ledger.db() as db:
        if db.execute('SELECT 1 FROM operations WHERE cell=? LIMIT 1',(cell,)).fetchone():
            raise ValueError('This arm already has ledger operations; do not repeat it in a fresh folder')
    async with APIClient(provider,ledger) as client:
        return await run_arm(args.parent,args.parent_sha256,arm,client,args.out,
            archive_config=CompressionConfig(recovery_chars=args.recovery_chars,recovery_search_mode=args.recovery_search_mode),
            workspace=args.workspace,verifiers=verifiers,trial_id=args.trial_id)


def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    f=s.add_parser('prepare');f.add_argument('--checkpoint',type=Path,required=True)
    f.add_argument('--memory',type=Path,required=True);f.add_argument('--parent-cost',type=Path,required=True)
    f.add_argument('--out',type=Path,required=True);f.add_argument('--require-generated',action='store_true')
    r=s.add_parser('run');r.add_argument('--parent',type=Path,required=True);r.add_argument('--parent-sha256',required=True)
    r.add_argument('--provider',type=Path,required=True);r.add_argument('--budget',type=Path,required=True)
    r.add_argument('--ledger',type=Path,required=True);r.add_argument('--out',type=Path,required=True);r.add_argument('--arm',required=True)
    r.add_argument('--recovery-calls',type=int,default=0);r.add_argument('--revalidation-calls',type=int,default=0)
    r.add_argument('--trial-id',help='Predeclared replicate identity; never use to retry an ambiguous operation')
    r.add_argument('--recovery-search-mode',choices=['head','match'],default='head')
    r.add_argument('--recovery-chars',type=int,default=6000);r.add_argument('--max-turns',type=int,default=6)
    r.add_argument('--workspace',type=Path);r.add_argument('--registry',type=Path);r.add_argument('--registry-sha256')
    r.add_argument('--acknowledge-task-sandbox',action='store_true');r.add_argument('--allow-paid',action='store_true')
    args=p.parse_args()
    try:
        if args.command=='prepare':
            checkpoint=Checkpoint.model_validate(strict_json_loads(args.checkpoint.read_bytes()))
            memory=Memory.model_validate(strict_json_loads(args.memory.read_bytes()))
            cost=strict_json_loads(args.parent_cost.read_bytes())
            if not isinstance(cost,dict):raise ValueError('Parent cost must be a ledger-cost object')
            result={'parent_sha256':freeze_intervention(args.out,checkpoint,memory,parent_cost=cost,
                    expected_generated=True if args.require_generated else None),'paid_model_calls':0}
        else:result=asyncio.run(run(args))
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (LabError,ValueError,OSError,TypeError,KeyError) as exc:
        print(json.dumps({'error':exc.kind if isinstance(exc,LabError) else type(exc).__name__,
                          'message':str(exc) if isinstance(exc,LabError) else 'Preflight or execution failed; preserve the arm and ledger.'}),file=sys.stderr)
        raise SystemExit(2)
if __name__=='__main__':main()
