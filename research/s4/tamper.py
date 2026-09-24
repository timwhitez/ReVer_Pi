#!/usr/bin/env python3
"""Raw-evidence negative controls; original run stays byte-identical."""
from pathlib import Path
import sys
R=Path(__file__).resolve().parent;ROOT=R.parents[1]
for p in (ROOT/'src',ROOT/'scripts',R):sys.path.insert(0,str(p))
import paired_prefix_tamper as original
from reverpi_sources.audit import audit as raw_audit
from reverpi.errors import LabError

def audit(path):
    try:return raw_audit(path)
    except LabError as e:raise ValueError(str(e)) from e
from reverpi_sources.contracts import new_output

def change_fixture(p):
    q=next(q for q in (p/'workspace').rglob('*.py') if q.is_file());q.chmod(0o600);q.write_bytes(q.read_bytes()+b'\n# altered\n')
def change_executor(p):original.update(p/'EXECUTOR.json',lambda d:d['research'].update({'fake.py':'0'*64}))
def change_task(p):original.update(p/'TASK.json',lambda d:d.update(prompt=d['prompt']+' changed'))

def run(source,out):
    # Reuse the established mutations and copy loop, explicitly binding THIS auditor.
    original.audit=audit;original.MUTATIONS=dict(original.MUTATIONS,workspace_changed=change_fixture,executor_identity=change_executor,public_prompt=change_task)
    return original.run(source,new_output(out))
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    result=run(a.run,a.out);print(result['status']);raise SystemExit(0 if result['status']=='passed' else 2)
