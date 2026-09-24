#!/usr/bin/env python3
"""Offline, read-only audit of the ORIGINAL gen7 stage2 snapshot.

Pass the unmodified extracted input, not an upgraded working tree. Model caches
are validated locally; no HTTP calls, automatic retries or source edits occur.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from reverpi.util import digest,unseal_cache,source_manifest
from reverpi.review import review_jobs,validate_findings


def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def db_snapshot(p):
    if not p.is_file():raise FileNotFoundError(p)
    for suffix in ['-wal','-journal']:
        side=Path(str(p)+suffix)
        if side.exists() and side.stat().st_size:raise ValueError('Nonempty journal: obtain a consistent snapshot first')
    db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    db.row_factory=sqlite3.Row
    return db


def ledger_rows(path):
    with db_snapshot(path) as db:
        rows=[dict(x) for x in db.execute('SELECT * FROM attempts ORDER BY id')]
    return rows


def totals(rows):
    known=sum(r['actual_tokens'] or 0 for r in rows)
    reserved=sum(r['reserve_tokens'] for r in rows if r['actual_tokens'] is None)
    return {'attempts':len(rows),'observed_tokens':known,'unknown_reserve_tokens':reserved,
            'accounted_tokens':known+reserved,'unknown_attempts':sum(r['actual_tokens'] is None for r in rows),
            'currency_actual':None}


def audit(root: Path):
    before={str(p.relative_to(root)):sha(p) for p in root.rglob('*') if p.is_file()}
    manifest=json.loads((root/'MANIFEST_GEN7B.json').read_text())
    mismatches=[p for p,h in manifest['files'].items() if before.get(p)!=h]
    report={'schema':1,'input_manifest':{'declared_file_count':manifest['file_count'],
          'hashed_files':len(manifest['files']),'mismatches':mismatches},'independent_benchmark':False}
    if mismatches:raise ValueError('Original delivery manifest mismatch')
    gold={r['id']:r['answers'] for r in map(json.loads,(root/'data/fixtures/gold.jsonl').read_text().splitlines())}
    comparisons={};arms=[];all_paid=[]
    for arm in ['f2-summary-norecovery','f2-summary-recovery']:
        folder=root/'runs'/arm
        rows=json.loads((folder/'results.json').read_text());attempts=ledger_rows(folder/'ledger.sqlite')
        all_paid.extend(attempts)
        groups=[]
        for method in sorted({r['method'] for r in rows}):
            cells=[r for r in rows if r['method']==method];literal=0;generated=0;hashes=[]
            for row in cells:
                if digest({k:v for k,v in row.items() if k!='row_sha256'})!=row['row_sha256']:
                    raise ValueError('Result row digest mismatch')
                memory=json.loads((folder/'memories'/f"{row['cell']}.json").read_text())
                generated+=int(memory['generated'])
                expected=[v for values in gold[row['task_id']].values() for v in values]
                literal+=int(all(v in memory['text'] for v in expected))
                comparisons[(arm,method,row['task_id'])]=memory['text']
                hashes.append({'task':row['task_id'],'memory_text_sha256':hashlib.sha256(memory['text'].encode()).hexdigest()})
            groups.append({'method':method,'cells':len(cells),'success':sum(r['success'] for r in cells),
                 'generated':generated,'all_gold_literals_present':literal,
                 'row_tokens':sum(r['cost']['known_tokens'] for r in cells),'memory_identities':hashes})
        t=totals(attempts)
        if sum(g['row_tokens'] for g in groups)!=t['observed_tokens']:raise ValueError('F2 rows/ledger mismatch')
        arms.append({'name':arm,'methods':groups,'ledger':t})
    report['F2']={'arms':arms,'total':totals(all_paid),'same_memory_pairs':{m:sum(
       comparisons[('f2-summary-norecovery',m,t)]==comparisons[('f2-summary-recovery',m,t)]
       for t in gold) for m in ['summary','rever_summary']},
       'claim_limit':'Ceiling result on this fixture; not impossibility of a synthetic recovery domain; rever_summary generative branch inactive.'}
    source,jobs=review_jobs(root,3)
    by_suffix={digest(job)[:32]:job for job in jobs}
    pilots=[];review_attempts=[]
    for name in ['pilot-v4pro','pilot-v4pro-b']:
        folder=root/'reviews'/name;p=folder/'ledger.sqlite';rows=ledger_rows(p);review_attempts.extend(rows)
        with db_snapshot(p) as db:
            frozen=json.loads(db.execute("SELECT value FROM meta WHERE key='review_source'").fetchone()[0])
            providers=[json.loads(x[0]) for x in db.execute("SELECT value FROM meta WHERE key LIKE 'provider:%'")]
            ops=[dict(x) for x in db.execute('SELECT op,state,result,error FROM operations ORDER BY created')]
        if frozen!=source:raise ValueError('Review source identity does not match original snapshot')
        outcomes=[]
        for op in ops:
            job=by_suffix.get(op['op'].split('_',1)[1])
            if job is None:raise ValueError('Review op cannot be matched to its frozen source job')
            outcome={'op':op['op'],'file':job['file'],'round':job['round'],'state':op['state']}
            if op['state']=='complete':
                completion=unseal_cache(op['result'])
                try:
                    findings=validate_findings(completion['text'],job,root,False)
                    outcome.update(evidence_valid=True,verdict=findings['verdict'],findings=len(findings['findings']))
                except ValueError:outcome.update(evidence_valid=False)
            outcomes.append(outcome)
        times=[{'id':row['id'],'error_kind':row['error_kind'],'state':row['state'],
            'seconds_until_next_attempt_start':rows[i+1]['started']-row['started'] if i+1<len(rows) else None}
            for i,row in enumerate(rows)]
        pilots.append({'name':name,'source_identity_match':True,'source_files':len(source),
             'planned_full_review_jobs':len(jobs),'read_timeout_seconds':providers[0]['read_seconds'],
             'records':outcomes,'ledger':totals(rows),'attempt_intervals_not_request_durations':times,
             'completed_evidence_valid':sum(x.get('evidence_valid',False) for x in outcomes),
             'verdict_counts':dict(Counter(x['verdict'] for x in outcomes if 'verdict' in x))})
    report['E2']={'pilots':pilots,'total':totals(review_attempts),
       'failure_probability_upper_one_sided_95_if_5_iid_completions':1-.05**(1/5),
       'claim_limit':'5/5 conditional schema/evidence adherence is not an end-to-end 100% rate, finding precision, code correctness, or model superiority. Client/upstream origin not identified.'}
    report['stage_paid_token_accounting']=totals(all_paid+review_attempts)
    rst=(root/'reports/gen7_20260918/c2_rst_captures.txt').read_text().splitlines()
    key=lambda line: re.search(r'IP (.+?) > (.+?): Flags (\[[^]]+\]), seq (\d+)',line).groups()
    container=[x for x in rst if 'veth' in x or 'br-' in x]
    access=(root/'reports/gen7_20260918/c2_gateway2_access.log').read_text()
    report['C2']={'text_rst_lines':len(rst),'container_interface_lines':len(container),
       'unique_container_reset_flows':len({key(x) for x in container}),
       'veth_lines':sum('veth' in x for x in rst),'bridge_lines':sum('br-' in x for x in rst),
       'loopback_lines':sum(' lo ' in x for x in rst),
       'session_200_log_lines':sum('/session' in x and '200 OK' in x for x in access.splitlines()),
       'binary_packet_captures_found':[str(p.relative_to(root)) for p in root.rglob('*') if p.suffix in {'.pcap','.pcapng'}],
       'same_delayed_mock_direct_control_present':False,
       'original_delay_proxy_source_found':False,
       'claim_limit':'Strong path/latency-associated diagnostic; exact RST cause and excluded components not certified. Bridge/veth observations are duplicate views, not independent resets.'}
    after={str(p.relative_to(root)):sha(p) for p in root.rglob('*') if p.is_file()}
    report['input_tree_unchanged']=before==after
    if not report['input_tree_unchanged']:raise ValueError('Input tree changed during read-only audit')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-root',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();result=audit(args.input_root)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'F2':result['F2']['total'],'E2':result['E2']['total'],
                      'stage':result['stage_paid_token_accounting'],'input_unchanged':result['input_tree_unchanged']}))
if __name__=='__main__':main()
