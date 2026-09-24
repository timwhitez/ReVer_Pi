#!/usr/bin/env python3
"""RC7 negative audits in disposable copies; original evidence stays byte-identical."""
import argparse
import copy
import hashlib
import shutil
import sqlite3
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from reverpi.util import canonical,digest,strict_json_loads,seal_cache,unseal_cache,atomic_write
from verify_paired_prefix import audit


def tree(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}


def update(path,fn):
    d=strict_json_loads(path.read_bytes());fn(d);atomic_write(path,canonical(d))


def change_summary(p): update(p/'summary.json',lambda d:d.update(quality_or_superiority_established=True))
def change_tape(p):
    d=unseal_cache((p/'tape.json').read_text());d['entries'][0]['response']['text']='FORGED'
    atomic_write(p/'tape.json',seal_cache(d))
def change_input(p):
    def f(d):
        d[0]['source']['tools'][0]['description']='different tool';d[0]['source_sha']=digest(d[0]['source'])
    update(p/'full/events.json',f)
def change_wire(p):
    folder=p/'projected/wire';rows=strict_json_loads((folder/'index.json').read_bytes());row=rows[0]
    q=folder/row['request_file'];b=strict_json_loads(q.read_bytes());b['model']='different';raw=canonical(b).encode()
    atomic_write(q,raw);row['request_sha256']=hashlib.sha256(raw).hexdigest();atomic_write(folder/'index.json',canonical(rows))
def change_exposures(p):
    ep=p/'projected/events.json';es=strict_json_loads(ep.read_bytes());e=next(x for x in es if x['prepared']['observations'])
    e['prepared']['observations'][0]['full_sends_before']+=1
    atomic_write(ep,canonical(es))
    db=sqlite3.connect(p/'projected/gateway/projection.sqlite')
    db.execute('update projection_events set record=? where op=?',(seal_cache(e['prepared']),e['gateway_op']));db.commit();db.close()
def change_budget_usage(p):
    db=sqlite3.connect(p/'accounting.sqlite');db.execute('update attempts set actual_tokens=0 where id=1');db.commit();db.close()
def change_archive(p):
    db=sqlite3.connect(p/'projected/gateway/archive.sqlite');db.execute("update blobs set content=content||'tampered' where rowid=(select min(rowid) from blobs)");db.commit();db.close()
def change_counter(p):
    db=sqlite3.connect(p/'full/gateway/projection.sqlite');db.execute('update observations set full_sends=full_sends+1');db.commit();db.close()
def change_answer(p):
    update(p/'full/phase.json',lambda d:d.update(answer='FORGED'))
    def f(d):
        next(x for x in d['phases'] if x['phase']=='full')['answer']='FORGED'
    update(p/'summary.json',f)
def change_noop(p):update(p/'summary.json',lambda d:d.update(status='no_eligible_prefix'))
def change_fixture(p):
    q=next((p/'workspace').iterdir());q.chmod(0o600);q.write_text(q.read_text()+'x')
def change_response(p):
    q=p/'capture/wire/000.response.bin';q.write_bytes(q.read_bytes()[:-1])

MUTATIONS={'quality_overclaim':change_summary,'sealed_tape_forgery':change_tape,'tool_surface_change':change_input,
           'wire_rehashed':change_wire,'exposure_resealed':change_exposures,'actual_usage_changed':change_budget_usage,
           'archive_corrupt':change_archive,'stored_counter':change_counter,'answer_changed':change_answer,
           'false_no_eligible':change_noop,'workspace_changed':change_fixture,'raw_response_truncated':change_response}


def run(source,out):
    if out.exists() or out.is_symlink():raise ValueError('Use a new destination')
    source=source.resolve(strict=True);before=tree(source)
    if audit(source)['run_status']!='paired_complete':raise ValueError('Negative suite requires completed long paired case')
    out.mkdir(parents=True,mode=0o700);rows=[]
    for name,fn in MUTATIONS.items():
        folder=out/name;shutil.copytree(source,folder);fn(folder)
        try:audit(folder)
        except ValueError as e:rows.append({'case':name,'rejected':True,'reason':str(e)})
        else:rows.append({'case':name,'rejected':False})
        shutil.rmtree(folder)
    if tree(source)!=before:raise ValueError('Original changed during negative controls')
    r={'schema':1,'status':'passed' if all(x['rejected'] for x in rows) else 'failed',
       'cases':rows,'original_unchanged':True,'real_model_calls':0,'scope':'same-session negative evidence checks, not independent model reviewers'}
    atomic_write(out/'report.json',canonical(r));return r


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();r=run(a.run,a.out);print(canonical(r));return 0 if r['status']=='passed' else 2
if __name__=='__main__':raise SystemExit(main())
