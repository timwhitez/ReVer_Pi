#!/usr/bin/env python3
"""Offline audit of a checkpointed ReVer-Pi bundle. No provider calls or repairs.

Run from the repository with PYTHONPATH=src. Outputs must be outside the input
snapshot. For an RC2 bundle use --review-source provenance/rc1_source_snapshot.
Use only an unpacked, immutable archive, not a currently running experiment.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from pathlib import Path
from reverpi.bundle_audit import audit_delivery, audit_review, audit_native_run, audit_acceptance, load, contained
from reverpi.research_audit import audit_ledger
from reverpi.results import read_results
from reverpi.util import atomic_write, canonical, digest, bytes_digest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True)
    p.add_argument('--out',required=True)
    p.add_argument('--review-source',help='Relative source snapshot inside root, or absolute source root')
    args=p.parse_args()
    root=Path(args.root).resolve(strict=True);out=Path(args.out).resolve()
    if out.is_relative_to(root):raise ValueError('Write audits outside the immutable input snapshot')
    source=root
    if args.review_source:
        candidate=Path(args.review_source)
        source=candidate.resolve(strict=True) if candidate.is_absolute() else contained(root,args.review_source)
    outputs={}
    outputs['delivery']=audit_delivery(root)
    outputs['review_state']=audit_review(source,root/'reviews/rc1-merged-r1')
    outputs['native_gen6']=audit_native_run(root/'runs/owned-native-gen6')
    outputs['native_pubdiag']=audit_native_run(root/'runs/pubdiag')
    outputs['native_acceptance']=audit_acceptance(root/'runs/native-accept-gen6')
    ledger_names=['runs/gateway-gen6','runs/gateway-pubdiag','runs/live-g2-mid',
                  'runs/live-g2-tight','reviews/rc1-merged-r1']
    totals=[]
    for name in ledger_names:
        db=root/name/'ledger.sqlite'
        r=audit_ledger(db,immutable_snapshot=True)
        outputs['ledger_'+name.split('/')[-1]]=r
        totals.append({'run':name,'database_sha256':bytes_digest(db.read_bytes()),**r['totals']})
    keys=['attempts','observed_tokens','unknown_attempts','unknown_reserved_tokens','accounted_tokens']
    outputs['cost_scope']={'schema':1,'ledgers':totals,
        'totals':{k:sum(row[k] for row in totals) for k in keys},
        'scope':'Only five supplied ledgers. Gateway-gen6 includes its canary and native matrix; do not add them again.',
        'usd_cost':None,'project_lifetime_total_verified':False,
        'reservations_are_certified_caps':False}
    diagnostics={}
    for name in ['live-g2-mid','live-g2-tight']:
        rows=read_results(root/'runs'/name);methods=defaultdict(lambda:{'cells':0,'successes':0,'tokens':0,'errors':0})
        checks=[];clusters=set()
        for row in rows:
            m=methods[row['method']];m['cells']+=1;m['successes']+=row.get('success') is True
            m['errors']+=row.get('status')!='complete';m['tokens']+=row['cost']['accounted_tokens']
            clusters.add(row.get('source_group'))
            checks.append(row.get('row_sha256')==digest({k:v for k,v in row.items() if k!='row_sha256'}))
        full=methods.get('full',{}).get('tokens')
        for m in methods.values():m['relative_token_reduction']=1-m['tokens']/full if full else None
        diagnostics[name]={'methods':dict(methods),'row_hashes_match':all(checks),'answer_hashes_match':True,
            'source_groups_recorded':sorted(str(c) for c in clusters),
            'is_external_benchmark':False,'population_noninferiority_established':False,
            'warning':'Success counts are from sealed rows, not a new execution or regrading of the model answers.'}
    outputs['checkpoint_diagnostics']=diagnostics
    out.mkdir(parents=True,exist_ok=True)
    for name,obj in outputs.items():atomic_write(out/(name+'.json'),canonical(obj)+'\n')
    summary={'schema':1,'new_paid_calls':0,'raw_results_rewritten':False,
        'review_gate_passed':False,'native_recovery_verified_on_old_canary':False,
        'new_source_native_acceptance':False,'g4_executed':False,
        'audit_files':{k+'.json':bytes_digest((out/(k+'.json')).read_bytes()) for k in outputs}}
    atomic_write(out/'AUDIT_INDEX.json',canonical(summary)+'\n')
    print(canonical(summary))


if __name__=='__main__':main()
