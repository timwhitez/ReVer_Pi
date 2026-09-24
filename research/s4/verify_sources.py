#!/usr/bin/env python3
"""Re-execute explicitly bundled controller probes; no actor or Provider calls.

This is not an independent upstream test suite. Manual expected values are
crosschecked against the fixed installed-source subsets with import provenance.
"""
from pathlib import Path
import base64, csv, hashlib, json, os, subprocess, sys
R=Path(__file__).resolve().parent;ROOT=R.parents[1]
for x in (ROOT/'src',ROOT/'scripts',R):sys.path.insert(0,str(x))
from reverpi.util import canonical, strict_json_loads, atomic_create
from reverpi_sources.contracts import load_task, tree_manifest, read_gold, new_output
from reverpi.paired_prefix import require

def verify():
    records=strict_json_loads((R/'data/controller/SOURCE_REGISTRY.json').read_bytes());checked=[]
    for r in records:
        sid=r['id'];record=R/'data/controller/distribution_records'/sid/'RECORD'
        lookup={row[0]:row[1] for row in csv.reader(record.read_text().splitlines())}
        for f in r['files']:
            raw=(R/'data/sources'/sid/f['path']).read_bytes();sha=hashlib.sha256(raw).hexdigest()
            require(sha==f['sha256'],'Source hash differs')
            require(lookup[f['installed_record_path']]=='sha256='+base64.urlsafe_b64encode(bytes.fromhex(sha)).decode().rstrip('='),'Installed RECORD differs')
        checked.append({'source_id':sid,'files':len(r['files']),'record_entries_checked':True,'external_wheel_verified':False})
    results=[]
    for path in sorted((R/'data/tasks').glob('*.json')):
        t=load_task(path);snapshot=R/'data/sources'/t['source_id'];before=tree_manifest(snapshot)
        goldpath=R/'data/controller'/f"{t['task_id']}.gold.json";g=read_gold(goldpath,t,hashlib.sha256(goldpath.read_bytes()).hexdigest())
        code=(R/'data/controller'/f"{t['task_id']}.oracle.py").read_text();require(hashlib.sha256(code.encode()).hexdigest()==g['probe_sha256'],'Oracle code changed')
        # Only maintained probes run; not a facility for untrusted shell commands.
        prologue='import sys,socket,json\nsys.dont_write_bytecode=True\nsys.path.insert(0,'+repr(str(snapshot))+')\n'
        prologue+='def deny(*a,**k): raise RuntimeError("network prohibited in source oracle")\nsocket.socket.connect=deny\nsocket.create_connection=deny\n'
        package=t['source_id'].rsplit('-',1)[0]
        suffix='\nimport '+package+' as _package\nfrom pathlib import Path as _P\nif not _P(_package.__file__).resolve().is_relative_to(_P('+repr(str(snapshot))+').resolve()): raise RuntimeError("Oracle imported wrong package")\n'
        env={k:os.environ[k] for k in ('PATH','LD_PRELOAD','REVER_OFFLINE_GUARD_LOG','REVER_OFFLINE_VERIFICATION') if k in os.environ}
        p=subprocess.run([sys.executable,'-I','-B','-c',prologue+code+'\nprint(json.dumps(out,sort_keys=True))\n'+suffix],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=20)
        if p.returncode:raise RuntimeError(f"Probe failed {t['task_id']}: {p.stderr}")
        result=strict_json_loads(p.stdout);require(canonical(result)==canonical(g['expected']),'Manual/oracle mismatch')
        require(before==tree_manifest(snapshot)==t['files'],'Snapshot changed')
        results.append({'task_id':t['task_id'],'source_group':t['source_group'],'status':'passed','observed':result,'snapshot_unchanged':True})
    return {'schema':1,'sources':checked,'tasks':results,'real_model_calls':0,'oracle_scope':'manual expectations vs execution of fixed source; no upstream-independent quality certification'}

def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=new_output(a.out);r=verify();atomic_create(out,canonical(r));print(canonical({'status':'passed','tasks':len(r['tasks'])}));return 0
if __name__=='__main__':raise SystemExit(main())
