#!/usr/bin/env python3
"""Read-only audit of the supplied gen9 ZIP, using only Python's standard library.

No extraction of executable files, no model calls, and no mutation of original
SQLite evidence. A nonempty WAL/journal is rejected instead of ignored.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile,zipfile
from pathlib import Path,PurePosixPath
from collections import Counter,defaultdict

def sha(b):return hashlib.sha256(b).hexdigest()
def canonical(o):return json.dumps(o,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(o):return sha(canonical(o))

class Bundle:
    def __init__(self,path):
        self.path=Path(path);self.z=zipfile.ZipFile(path);self.files={}
        entries=self.z.infolist();total=0
        tops={i.filename.split('/')[0] for i in entries}
        if len(tops)!=1:raise ValueError('Expected one explicit archive root')
        self.prefix=next(iter(tops))+'/'
        for i in entries:
            if i.is_dir():continue
            name=i.filename.removeprefix(self.prefix);p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or name in self.files:raise ValueError('Unsafe/duplicate archive path')
            total+=i.file_size
            if i.file_size>200*1024**2 or total>1024**3:raise ValueError('Archive exceeds audit bounds')
            self.files[name]=i.filename
    def raw(self,name):return self.z.read(self.files[name])
    def obj(self,name):return json.loads(self.raw(name))
    def sql(self,name,query):
        for suffix in ('-wal','-journal'):
            if name+suffix in self.files and self.raw(name+suffix):raise ValueError('Nonempty journal requires a consistent database snapshot')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'snapshot.sqlite';p.write_bytes(self.raw(name));p.chmod(0o400)
            with sqlite3.connect(p.as_uri()+'?mode=ro&immutable=1',uri=True) as db:
                db.row_factory=sqlite3.Row
                return [dict(r) for r in db.execute(query)]
    def close(self):self.z.close()

def cost(b,name):
    r=b.sql(name,'SELECT COUNT(*) attempts,COALESCE(SUM(actual_tokens),0) observed_tokens,'
       'SUM(CASE WHEN actual_tokens IS NULL THEN 1 ELSE 0 END) unknown_attempts,'
       'COALESCE(SUM(CASE WHEN actual_tokens IS NULL THEN reserve_tokens ELSE 0 END),0) unknown_reserved_tokens '
       'FROM attempts')[0]
    r['accounted_tokens']=r['observed_tokens']+r['unknown_reserved_tokens'];r['database_sha256']=sha(b.raw(name))
    r['currency_cost']=None;r['reservations_are_certified_upper_bounds']=False
    return r

def audit(path):
    b=Bundle(path)
    try:
        actual={n:sha(b.raw(n)) for n in b.files}
        declared=b.obj('MANIFEST_GEN9.json')['files']
        mismatches=[{'path':n,'declared_sha256':h,'actual_sha256':actual.get(n)} for n,h in declared.items() if actual.get(n)!=h]
        rows=b.obj('reviews/rc3-flash-r1/reviews.json');m=b.obj('reviews/rc3-flash-r1/review_manifest.json')
        findings=[f for r in rows if r.get('result') for f in r['result']['findings']]
        frozen=json.loads(b.sql('reviews/rc3-flash-r1/ledger.sqlite',"SELECT value FROM meta WHERE key='review_source'")[0]['value'])
        changed=[n for n,h in frozen.items() if actual.get(n)!=h]
        failures=Counter((r.get('error') or {}).get('kind') for r in rows if r['status']!='complete')
        nvalid=sum(r['status']=='complete' for r in rows)
        review={'planned':m['planned_jobs'],'visited':len(rows),'valid':nvalid,'failures':dict(failures),
                'verdicts':dict(Counter(r['result']['verdict'] for r in rows if r.get('result'))),
                'findings':len(findings),'severities':dict(Counter(f['severity'] for f in findings)),
                'records_hash_matches':digest(rows)==m['review_records_sha'],
                'manifest_findings_match':findings==m['findings'],'review_source_files':len(frozen),
                'input_files_different_from_review_snapshot':changed,
                'all_jobs_valid':m['all_jobs_valid'],'source_unchanged':m['source_unchanged'],
                'conditional_evidence_validity':nvalid/(len(rows)-failures.get('transport_ambiguous',0)),
                'end_to_end_validity':nvalid/len(rows),'scope':'Independent contexts, same model; findings not ground truth'}
        natives=b.obj('runs/gen9-allowlist/results.json');methods=defaultdict(lambda:{'cells':0,'successes':0,'tokens':0})
        native_checks=[]
        native_cells={r['cell'] for r in natives}
        for r in natives:
            x=methods[r['method']];x['cells']+=1;x['successes']+=r.get('success') is True;x['tokens']+=r['cost']['known_tokens']
            # Explicit relocation map from the archived absolute path, not a guessed filename.
            frozen_prefix='/root/reverpi/rc3_work/ReVerPi_v1.3_RC3/'
            if not r['official_result_path'].startswith(frozen_prefix):raise ValueError('Unknown native path mapping')
            p=r['official_result_path'][len(frozen_prefix):]
            native_checks.append({'cell':r['cell'],'row_hash_ok':digest({k:v for k,v in r.items() if k!='row_sha256'})==r['row_sha256'],
                                  'official_result_bytes_match':sha(b.raw(p))==r['official_result_sha'],
                                  'mapped_path':p})
        compacts=b.sql('runs/gateway-gen9/sessions.sqlite','SELECT session,state FROM compact_ops')
        native={'cells':len(natives),'methods':dict(methods),'checks':native_checks,
                'gateway_compactions_for_benchmark':[c for c in compacts if c['session'] in native_cells],
                'is_independent_benchmark':False,'source_groups':sorted({r['source_group'] for r in natives})}
        f3=b.obj('reports/gen8_20260918/f3_four_arm_evaluation.json');cells=[];sums=defaultdict(lambda:{'cells':0,'correct':0,'observed_tokens':0})
        for arm,reps in f3['arms'].items():
            for rep in reps:
                folder='runs/paired-gen8-'+('' if rep['rep']=='a' else rep['rep']+'-')+arm
                r=b.obj(folder+'/result.json');plan=b.obj(folder+'/plan.json')
                events=b.obj(folder+'/tool_receipts.json') if folder+'/tool_receipts.json' in b.files else []
                # Compare archived answer hash to the preexisting evaluation commitment; no gold is sent to any model.
                
                if set(r['answers'])!={'historical_token'}:raise ValueError('Unexpected F3 question identity')
                answer=r['answers']['historical_token'];correct=sha(answer.encode())==f3['gold_sha256']
                row={'folder':folder,'arm':arm,'rep':rep['rep'],'parent_sha':r['parent_sha256'],
                     'memory_sha':r['memory_text_sha256'],'correct':correct,
                     'evaluation_matches':correct==rep['correct'] and sha(answer.encode())==rep['answer_sha256'],
                     'tool_attempts':dict(Counter(e['tool'] for e in events)),
                     'tool_successful_receipts':dict(Counter(e['tool'] for e in events if not e.get('receipt',{}).get('error'))),
                     'tokens':r['cost']['known_tokens'],'source_group':plan['source_group'],
                     'ledger_ref':'runs/paired-gen8-'+('shared' if rep['rep']=='a' else rep['rep'])+'.sqlite'}
                cells.append(row);x=sums[arm];x['cells']+=1;x['correct']+=correct;x['observed_tokens']+=row['tokens']
        f3_report={'cells':cells,'arms':dict(sums),'total_observed_tokens':sum(c['tokens'] for c in cells),
                   'unique_parents':len({c['parent_sha'] for c in cells}),'unique_memories':len({c['memory_sha'] for c in cells}),
                   'source_groups':sorted({c['source_group'] for c in cells}),
                   'correct_both_cells':[c for c in cells if c['arm']=='both' and c['correct']],
                   'shared_ledgers':{name:cost(b,name) for name in sorted({c['ledger_ref'] for c in cells})},
                   'population_or_interaction_effect_established':False}
        costs={k:cost(b,k+'/ledger.sqlite') for k in ('reviews/rc3-flash-r1','runs/gateway-gen9')}
        total={k:sum(v[k] for v in costs.values()) for k in ('attempts','observed_tokens','unknown_attempts','unknown_reserved_tokens','accounted_tokens')}
        return {'schema':1,'input_zip_sha256':sha(Path(path).read_bytes()),
                'delivery':{'declared_files':len(declared),'actual_files':len(actual),'mismatches':mismatches,
                            'unlisted':[n for n in actual if n not in declared]},
                'review':review,'native_gen9':native,'f3':f3_report,'latest_two_ledgers':costs,
                'latest_two_ledger_totals':total,'f3_is_reported_separately_not_added_twice':True,
                'raw_evidence_modified':False,'new_paid_calls':0,'project_lifetime_cost_verified':False}
    finally:b.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();result=audit(a.archive);a.out.parent.mkdir(parents=True,exist_ok=True)
    with a.out.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'out':str(a.out),'new_paid_calls':0,'input_zip_sha256':result['input_zip_sha256']},indent=2))
if __name__=='__main__':main()
