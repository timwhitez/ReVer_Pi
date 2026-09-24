#!/usr/bin/env python3
"""Read-only audit of the supplied TB24 evidence window. No model/remote calls.

Verifies the contained rows and ledgers, not unavailable original Harbor results.
Never resolves absolute paths stored in evidence. Writes only a new report file.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import math
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reverpi.util import (atomic_create, canonical, contained_regular_file,
                          digest, strict_json_loads)
from audit_rc5_campaign import copy_sqlite_family

FILES = {
    'plan':'plans/tb24-official-flash.json',
    'manifest':'runs/tb24-official-flash/manifest.json',
    'rows':'runs/tb24-official-flash/results.json',
    'tb_ledger':'runs/gateway-tb24-official3/ledger.sqlite',
    'review_ledger':'reports/rc52_authorized_round/v4pro-scoped/ledger.sqlite',
    'reviews':'reports/rc52_authorized_round/v4pro-scoped/reviews.json',
    'report':'reports/rc52_authorized_round/OFFICIAL_WINDOW_FINAL_REPORT.md',
}


def hash_file(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024*1024),b''): h.update(part)
    return h.hexdigest()


def exact_sign(wins:int, losses:int) -> dict:
    """Conditional on discordant pairs, binomial p=.5; no power/equivalence claim."""
    if type(wins) is not int or type(losses) is not int or min(wins,losses)<0:
        raise ValueError('Require nonnegative integer counts')
    n=wins+losses
    if n>10000: raise ValueError('Exact audit count exceeds its bounded diagnostic scope')
    if n==0: return {'discordant_pairs':0,'two_sided':1.0,'one_sided_candidate_greater':1.0}
    denominator=2**n
    observed=math.comb(n,wins)
    two=sum(math.comb(n,k) for k in range(n+1) if math.comb(n,k)<=observed)/denominator
    greater=sum(math.comb(n,k) for k in range(wins,n+1))/denominator
    return {'discordant_pairs':n,'two_sided':min(1.0,two),'one_sided_candidate_greater':greater}


def validate_rows(rows, grid):
    if not isinstance(rows,list) or not isinstance(grid,list): raise ValueError('Expected arrays')
    plans={}; actual={}
    for cell in grid:
        if not isinstance(cell,dict) or not isinstance(cell.get('cell'),str) or cell['cell'] in plans:
            raise ValueError('Duplicate/malformed planned cell')
        plans[cell['cell']]=cell
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('cell'),str) or row['cell'] in actual:
            raise ValueError('Duplicate/malformed result cell')
        if row['cell'] not in plans: raise ValueError('Unplanned result')
        if digest({k:v for k,v in row.items() if k!='row_sha256'})!=row.get('row_sha256'):
            raise ValueError('Result row checksum mismatch')
        if any(row.get(k)!=v for k,v in plans[row['cell']].items()):
            raise ValueError('Result/plan identity mismatch')
        if type(row.get('success')) is not bool or type(row.get('execution_success')) is not bool:
            raise ValueError('Missing explicit reward/execution outcomes')
        if row.get('status')!='complete': raise ValueError('Window has nonterminal rows; use an incomplete-window audit')
        actual[row['cell']]=row
    if actual.keys()!=plans.keys(): raise ValueError('Missing planned result cells')
    return actual


def ledger_data(source, target, root):
    copy_sqlite_family(source,target,root=root)
    con=sqlite3.connect(target,timeout=5); con.row_factory=sqlite3.Row
    try:
        if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Ledger integrity check failed')
        attempts=[dict(x) for x in con.execute('SELECT * FROM attempts ORDER BY id')]
        operations=[dict(x) for x in con.execute('SELECT * FROM operations ORDER BY created,op')]
        for op in operations:
            if op['result']:
                envelope=strict_json_loads(op['result'])
                if envelope.get('cache_schema')!=1 or digest(envelope.get('payload'))!=envelope.get('sha256'):
                    raise ValueError('Cached response checksum mismatch')
                op['decoded']=envelope['payload']
        known=sum(a['actual_tokens'] for a in attempts if a['actual_tokens'] is not None)
        unknown=sum(a['reserve_tokens'] for a in attempts if a['actual_tokens'] is None)
        return {'observed_tokens':known,'unknown_reserved_tokens':unknown,'accounted_tokens':known+unknown,
                'attempts':len(attempts),'known_attempts':sum(a['actual_tokens'] is not None for a in attempts),
                'unknown_attempts':sum(a['actual_tokens'] is None for a in attempts),
                'error_counts':dict(Counter(a['error_kind'] or 'none' for a in attempts)),
                'usd':None,'prices_frozen_and_verified':False},attempts,operations
    finally:con.close()


def audit(root:Path):
    root=root.resolve(strict=True)
    paths={k:contained_regular_file(root,v) for k,v in FILES.items()}
    before={v:hash_file(paths[k]) for k,v in FILES.items()}
    # Sidecars, if ever added, are captured in the input identity as well.
    for k in ('tb_ledger','review_ledger'):
        for suffix in ('-wal','-shm','-journal'):
            p=Path(str(paths[k])+suffix)
            if p.exists() or p.is_symlink():
                relative=p.relative_to(root).as_posix()
                before[relative]=hash_file(contained_regular_file(root,relative))
    plan=strict_json_loads(paths['plan'].read_bytes())
    manifest=strict_json_loads(paths['manifest'].read_bytes())
    rows=strict_json_loads(paths['rows'].read_bytes())
    reviews=strict_json_loads(paths['reviews'].read_bytes())
    actual=validate_rows(rows,plan['grid'])
    if any(manifest.get(k)!=v for k,v in plan.items()): raise ValueError('Run manifest differs from frozen plan')
    methods=set(x['method'] for x in rows)
    if methods!={'pi_original','mask'}: raise ValueError('This version audits precisely Pi versus mask')
    pairs={}
    for row in rows:
        key=(row['task_id'],row['repeat'])
        if row['method'] in pairs.setdefault(key,{}): raise ValueError('Duplicate task/method/repeat')
        pairs[key][row['method']]=row
    if any(set(v)!=methods for v in pairs.values()): raise ValueError('Unpaired task')
    with tempfile.TemporaryDirectory(prefix='reverpi-window-readonly-') as t:
        tb,attempts,ops=ledger_data(paths['tb_ledger'],Path(t)/'tb.sqlite',root)
        review_cost,_,_=ledger_data(paths['review_ledger'],Path(t)/'review.sqlite',root)
    if set(a['cell'] for a in attempts)-set(actual): raise ValueError('Ledger has unmapped cells')
    for row in rows:
        aa=[a for a in attempts if a['cell']==row['cell']]
        known=sum(a['actual_tokens'] or 0 for a in aa)
        reserve=sum(a['reserve_tokens'] for a in aa if a['actual_tokens'] is None)
        expected={'known_tokens':known,'accounted_tokens':known+reserve,'attempts':len(aa),
                  'unknown_attempts':sum(a['actual_tokens'] is None for a in aa)}
        if any(row['cost'].get(k)!=v for k,v in expected.items()): raise ValueError('Cell cost/ledger mismatch')
    stats={}
    for method in sorted(methods):
        rr=[r for r in rows if r['method']==method]; cells={r['cell'] for r in rr}
        usage=Counter();tools=Counter();complete_requests=0
        for a in attempts:
            if a['cell'] not in cells or not a['raw_usage']:continue
            u=strict_json_loads(a['raw_usage'])
            usage.update(input=u.get('input_tokens',0),output=u.get('output_tokens',0),
                         cached_input=(u.get('input_tokens_details') or {}).get('cached_tokens',0),
                         reasoning_included_in_output=(u.get('output_tokens_details') or {}).get('reasoning_tokens',0))
        for op in ops:
            if op['cell'] in cells and 'decoded' in op:
                complete_requests+=1
                tools.update(c['name'] for c in op['decoded'].get('calls',[]))
        stats[method]={'successes':sum(r['success'] for r in rr),'tasks':len(rr),
                      'known_tokens':sum(r['cost']['known_tokens'] for r in rr),
                      'accounted_tokens':sum(r['cost']['accounted_tokens'] for r in rr),
                      'completed_cached_responses':complete_requests,'usage_breakdown':dict(usage),
                      'tool_calls_in_completed_cached_responses':dict(tools)}
    both_success=sum(p['pi_original']['success'] and p['mask']['success'] for p in pairs.values())
    both_failed=sum(not p['pi_original']['success'] and not p['mask']['success'] for p in pairs.values())
    wins=[key[0] for key,p in pairs.items() if p['mask']['success'] and not p['pi_original']['success']]
    losses=[key[0] for key,p in pairs.items() if not p['mask']['success'] and p['pi_original']['success']]
    timeouts=[{'cell':r['cell'],'task':r['task_id'],'method':r['method'],'success':r['success'],
               'exception':r['harbor_exception']} for r in rows if r.get('harbor_exception')]
    findings=[f for x in reviews if x.get('result') for f in x['result'].get('findings',[])]
    if any(x.get('code_sha')!=plan['code_sha'] for x in reviews): raise ValueError('Review/source identity mismatch')
    total={k:tb[k]+review_cost[k] for k in ('observed_tokens','unknown_reserved_tokens','accounted_tokens')}
    total['usd']=None
    for relative,h in before.items():
        if hash_file(contained_regular_file(root,relative))!=h:raise ValueError('Original input changed')
    for k in ('tb_ledger','review_ledger'):
        for suffix in ('-wal','-shm','-journal'):
            p=Path(str(paths[k])+suffix)
            if (p.exists() or p.is_symlink()) and p.relative_to(root).as_posix() not in before:
                raise ValueError('A sidecar appeared during static audit')
    return {'schema':1,'input_files':before,'source_sha':plan['code_sha'],'input_unchanged':True,
            'result_row_checks_passed':len(rows),'plan_and_cell_cost_checks_passed':True,
            'split':plan['matrix']['split'],'formal_g4_certified':False,'methods':stats,
            'paired':{'tasks':len(pairs),'both_success':both_success,'both_fail':both_failed,
                      'candidate_only':sorted(wins),'baseline_only':sorted(losses),
                      **exact_sign(len(wins),len(losses))},
            'execution_success_counts':dict(Counter(str(r['execution_success']) for r in rows)),
            'exception_rows':timeouts,'exceptions_with_successful_reward':sum(r['success'] for r in timeouts),
            'review':{'visited':len(reviews),'valid':sum(x['status']=='complete' for x in reviews),
                      'failures':dict(Counter(x['error']['kind'] for x in reviews if 'error' in x)),
                      'verdicts':dict(Counter(x['result']['verdict'] for x in reviews if 'result' in x)),
                      'findings':len(findings),'severities':dict(Counter(f['severity'] for f in findings))},
            'ledger_costs':{'tb':tb,'review':review_cost,'total_supplied_ledgers':total},
            'limits':{'original_official_results_verified':False,'original_task_timeout_files_verified':False,
                      'custom_compaction_activation':'UNKNOWN; separate sessions/compact_ops database absent',
                      'native_pi_compaction_activation':'UNKNOWN; session logs absent',
                      'performance_equivalence_established':False,'noninferiority_established':False,
                      'previous_truncated_window_causal_comparison_validated':False,
                      'review_precision_proven_by_finding_count':False,'paid_calls_this_audit':0,
                      'absolute_paths_in_evidence_followed':False,'provider_billing_upper_bound_certified':False}}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if args.out.resolve().is_relative_to(args.root.resolve()):p.error('Use a new report outside the input evidence')
    try:
        result=audit(args.root);atomic_create(args.out,canonical(result).encode())
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError) as exc:
        print(f'Audit rejected: {type(exc).__name__}: {exc}',file=sys.stderr);return 2
    print(canonical({'report':str(args.out),'input_unchanged':True,'cost':result['ledger_costs']['total_supplied_ledgers']}));return 0


if __name__=='__main__':raise SystemExit(main())
