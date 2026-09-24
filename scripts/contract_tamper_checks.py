#!/usr/bin/env python3
"""Negative controls against the fixed local compaction artifact auditor.

Always mutate disposable copies; never alter the supplied run. No network/model.
"""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from reverpi.util import canonical,digest,strict_json_loads,atomic_write
from native_compaction_audit import audit
from native_compaction_smoke import wire_view,MARKER,require

def run(source:Path)->dict:
    audit(source)
    protocols=strict_json_loads((source/'report.json').read_bytes())['protocols']
    cases=[]
    for mutation,expected in [('missing_cell','Missing/duplicate'),('native_certificate','Overstated'),
            ('reintroduced_marker','Wrong visibility'),('absent_memory','Committed memory not actually'),
            ('corrupt_recovery','Requested recovery blob'),('changed_model','Unexpected model'),
            ('changed_rejected_history','Rejected compaction changed checkpoint history'),('changed_tool_schema','Tool schema matching failed')]:
        with tempfile.TemporaryDirectory(prefix='rever-tamper-') as tmp:
            copy=Path(tmp)/'copy';shutil.copytree(source,copy)
            top=strict_json_loads((copy/'report.json').read_bytes())
            candidate=next(x for x in top['rows'] if x['protocol']==protocols[0] and x['condition']=='mask_checkpoint' and x['pressure']=='long')
            folder=copy/candidate['case_dir']
            if mutation=='changed_rejected_history':
                rejected=next(x for x in top['rows'] if x['pressure']=='oversized_recent')
                with (copy/rejected['case_dir']/'session.jsonl').open('a') as f:f.write('\n')
            elif mutation=='missing_cell':top['rows'].pop()
            elif mutation=='native_certificate':top['native_gate_eligible']=True
            elif mutation=='corrupt_recovery':
                with sqlite3.connect(folder/'gateway/archive.sqlite') as db:
                    db.execute('PRAGMA journal_mode=DELETE')
                    db.execute('UPDATE blobs SET content=? WHERE namespace=? AND handle=?',
                        ('corrupted bytes','offline_boundary',candidate['recovery']['recovery_handle']))
            else:
                req=candidate['followup'];body=strict_json_loads((folder/req['body_path']).read_bytes())
                if mutation=='changed_model':body['model']='deepseek-flash'
                elif mutation=='changed_tool_schema':
                    tool=body['tools'][0]
                    spec=tool.get('function',tool)
                    spec['description']='Changed action description in a negative control'
                else:
                    messages,_=wire_view(body,candidate['protocol'])
                    summary=next(m for m in messages if m.get('role')=='user' and isinstance(m.get('content'),str)
                        and 'The conversation history before this point' in m['content'])
                    if mutation=='reintroduced_marker':summary['content']+='\n'+MARKER
                    else:summary['content']='Compaction summary deliberately removed by negative control.'
                messages,_=wire_view(body,candidate['protocol'])
                changed={**req,'body_sha256':digest(body),'wire_content_bytes':len(canonical(messages).encode()),
                    'marker_in_wire':MARKER in canonical(messages)}
                candidate['followup']=changed
                candidate['requests']=[changed if r['index']==req['index'] else r for r in candidate['requests']]
                atomic_write(folder/req['body_path'],canonical(body))
                atomic_write(folder/'requests.json',canonical(candidate['requests']))
                atomic_write(folder/'report.json',canonical(candidate))
            atomic_write(copy/'report.json',canonical(top))
            detected=False;observed=''
            try:audit(copy)
            except (ValueError,RuntimeError) as exc:
                observed=str(exc);detected=expected in observed
            require(detected,'Negative control not specifically detected: '+mutation+' / '+observed)
            cases.append({'mutation':mutation,'detected':detected,'expected_error_fragment':expected})
    return {'schema':1,'passed':True,'negative_controls':cases,'source_run_modified':False,
        'new_model_calls':0,'not_a_malicious_operator_authenticity_proof':True}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();require(not a.out.exists(),'New output required')
    before={str(p.relative_to(a.run)):hashlib.sha256(p.read_bytes()).hexdigest() for p in a.run.rglob('*') if p.is_file()}
    result=run(a.run)
    after={str(p.relative_to(a.run)):hashlib.sha256(p.read_bytes()).hexdigest() for p in a.run.rglob('*') if p.is_file()}
    require(before==after,'Auditor modified the source evidence')
    atomic_write(a.out,canonical(result));print(canonical(result));return 0
if __name__=='__main__':raise SystemExit(main())
