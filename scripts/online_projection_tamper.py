#!/usr/bin/env python3
"""Run ten negative controls on private copies of owned projection evidence."""
from __future__ import annotations
import argparse,hashlib,shutil,sqlite3,tempfile
from pathlib import Path
from online_projection_audit import audit,load,require
from reverpi.util import canonical,digest,atomic_write,unseal_cache,seal_cache


def mutate(base:Path,kind:str):
    r=load(base,'report.json');p=r['protocols'][0];case=base/f'{p}__online_apply__long'
    if kind=='quality_overclaim':r['quality_measured']=True;atomic_write(base/'report.json',canonical(r));return
    if kind=='missing_case':r['rows'].pop();atomic_write(base/'report.json',canonical(r));return
    if kind=='manual_compact':
        with (case/'rpc.jsonl').open('a') as f:f.write(canonical({'type':'response','command':'compact','success':True})+'\n')
        return
    if kind=='source_marker':
        q=case/'session.jsonl';q.write_text(q.read_text().replace('HISTORICAL_MARKER_','CHANGED_MARKER_'));return
    if kind=='archive_corrupt':
        with sqlite3.connect(case/'gateway/archive.sqlite') as d:d.execute("update blobs set content=content||' changed' where namespace='owned_online'")
        return
    if kind in {'counter_consistent_reseal','changed_reason_consistent_reseal'}:
        q=case/'projection_events.json';events=load(case,q.name);e=events[3]['record'];item=e['observations'][0]
        if kind=='counter_consistent_reseal':item['full_sends_before']=99
        else:item['reason']='first_full_exposures'
        atomic_write(q,canonical(events))
        with sqlite3.connect(case/'gateway/projection.sqlite') as d:
            d.execute('update projection_events set record=? where seq=?',(seal_cache(e),events[3]['seq']))
        return
    if kind=='pending_sidecar':(case/'gateway/projection.sqlite-journal').write_bytes(b'not-a-static-snapshot');return
    if kind in {'actual_wire','tool_schema'}:
        i=3 if kind=='actual_wire' else 7;q=case/f'wire_{i:03d}.json';body=load(case,q.name)
        if kind=='actual_wire':
            key='messages' if p=='chat_completions' else 'input'
            row=next(x for x in body[key] if x.get('role')=='tool' or x.get('type')=='function_call_output')
            row['content' if p=='chat_completions' else 'output']='REPLACED BODY'
        elif p=='chat_completions':body['tools'][0]['function']['description']+=' extra instruction'
        else:body['tools'][0]['description']+=' extra instruction'
        atomic_write(q,canonical(body));idx=load(case,'requests.json');idx[i]['body_sha256']=digest(body)
        atomic_write(case/'requests.json',canonical(idx));local=load(case,'report.json');local['requests']=idx
        atomic_write(case/'report.json',canonical(local))
        for j,row in enumerate(r['rows']):
            if row['case_dir']==case.name:r['rows'][j]=local
        atomic_write(base/'report.json',canonical(r));return
    raise ValueError('Unknown negative control')


CASES=('quality_overclaim','missing_case','manual_compact','source_marker','archive_corrupt',
       'counter_consistent_reseal','changed_reason_consistent_reseal','pending_sidecar','actual_wire','tool_schema')


def run(source:Path,out:Path):
    require(not out.exists() and not out.is_symlink(),'Use a new output directory');source=source.resolve(strict=True)
    require(not out.resolve().is_relative_to(source),'Do not write inside input evidence');out.mkdir(parents=True)
    original={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    baseline=audit(source);rows=[]
    with tempfile.TemporaryDirectory(prefix='rc6-negative-') as temp:
        for kind in CASES:
            clone=Path(temp)/kind;shutil.copytree(source,clone);mutate(clone,kind)
            try:audit(clone)
            except (ValueError,KeyError,TypeError,sqlite3.Error) as error:rows.append({'case':kind,'rejected':True,'error_type':type(error).__name__,'message':str(error)})
            else:rows.append({'case':kind,'rejected':False})
            shutil.rmtree(clone)
    after={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    require(original==after,'Input evidence changed')
    result={'schema':1,'status':'passed' if all(x['rejected'] for x in rows) else 'failed','cases':rows,
            'source_sha':baseline['source_sha'],'original_files_unchanged':len(original),'real_model_calls':0}
    atomic_write(out/'report.json',canonical(result));return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    r=run(a.run,a.out);print(canonical({'status':r['status'],'rejected':sum(x['rejected'] for x in r['cases'])}));return 0 if r['status']=='passed' else 1
if __name__=='__main__':raise SystemExit(main())
