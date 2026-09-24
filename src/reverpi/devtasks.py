"""Six original SEARCH-only Harbor smoke tasks, not independent research evidence.

The controller's verifier and oracle are not copied into the agent image. They
are loaded by Harbor's verifier/oracle stages. Licenses are explicitly CC0.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from .util import atomic_write,canonical,digest

TASKS = [
('utf8-prefix','Implement prefix(text, budget), the longest Unicode-character prefix whose UTF-8 encoding fits a nonnegative integer byte budget. Reject negative budgets.',
 'def prefix(text, budget):\n    return text[:budget]\n',
 'def prefix(text, budget):\n    if budget < 0: raise ValueError("negative budget")\n    used=0;result=[]\n    for ch in text:\n        size=len(ch.encode("utf-8"))\n        if used+size>budget: break\n        used+=size;result.append(ch)\n    return "".join(result)\n',
 [('prefix("abcdef",3)','abc'),('prefix("中文ab",6)','中文'),('prefix("a😀b",4)','a'),('prefix("😀",3)',''),('prefix("",0)','')]),
('dependency-closure','Implement closure(graph, roots), returning a sorted list including roots and all transitively reachable dependency keys. Missing keys have no children. Cycles must terminate.',
 'def closure(graph, roots):\n    return sorted(set(roots)|{c for r in roots for c in graph.get(r,[])})\n',
 'def closure(graph, roots):\n    seen=set();todo=list(roots)\n    while todo:\n        x=todo.pop()\n        if x in seen: continue\n        seen.add(x);todo.extend(graph.get(x,[]))\n    return sorted(seen)\n',
 [('closure({"a":["b"],"b":["c"],"c":["d"]},["a"])',['a','b','c','d']),('closure({"a":["b"],"b":["a"]},["a"])',['a','b']),('closure({},["z"])',['z']),('closure({},[])',[]),('closure({"a":["c"],"b":["c"]},["a","b"])',['a','b','c'])]),
('revoke-versions','Implement resolve(events). Each event is ["set", key, id, value] or ["revoke", key, id]. A revoke deactivates that id, not earlier versions. Return the newest active value per key; omit keys with no active version. IDs are unique within a key.',
 'def resolve(events):\n    result={}\n    for e in events:\n        if e[0]=="set": result[e[1]]=e[3]\n        else: result.pop(e[1],None)\n    return result\n',
 'def resolve(events):\n    hist={};revoked=set()\n    for e in events:\n        if e[0]=="set": hist.setdefault(e[1],[]).append((e[2],e[3]))\n        else: revoked.add((e[1],e[2]))\n    result={}\n    for k,items in hist.items():\n        for ident,value in reversed(items):\n            if (k,ident) not in revoked: result[k]=value;break\n    return result\n',
 [('resolve([["set","x","1","A"],["set","x","2","B"],["revoke","x","2"]])',{'x':'A'}),('resolve([["set","x","1",1],["revoke","x","1"]])',{}),('resolve([])',{}),('resolve([["set","x","1",1],["set","y","1",2],["revoke","x","1"]])',{'y':2}),('resolve([["set","x","1",1],["set","x","2",2],["revoke","x","1"]])',{'x':2})]),
('evidence-validity','Implement valid_pass(evidence, current). Evidence has a passed boolean and dependencies mapping version keys to exact values. A valid pass requires passed is True, at least one dependency, and every dependency matches current. Missing keys are not a match. Return bool.',
 'def valid_pass(evidence,current):\n    return bool(evidence.get("passed"))\n',
 'def valid_pass(evidence,current):\n    deps=evidence.get("dependencies",{})\n    return evidence.get("passed") is True and bool(deps) and all(k in current and current[k]==v for k,v in deps.items())\n',
 [('valid_pass({"passed":True,"dependencies":{"x":"1"}},{"x":"2"})',False),('valid_pass({"passed":True,"dependencies":{"x":"1"}},{"x":"1"})',True),('valid_pass({"passed":True,"dependencies":{}},{})',False),('valid_pass({"passed":False,"dependencies":{"x":"1"}},{"x":"1"})',False),('valid_pass({"passed":True,"dependencies":{"x":None}},{})',False)]),
('bounded-backoff','Implement delay(attempt, base, cap, retry_after=None). attempt is a nonnegative int, base and cap finite nonnegative numbers. Local delay=min(cap,base*2**attempt). Return max(local delay, retry_after) when a finite nonnegative Retry-After is provided. Invalid inputs raise ValueError. Avoid overflow for large attempt.',
 'def delay(attempt,base,cap,retry_after=None):\n    return min(cap,base*attempt)\n',
 'import math\ndef delay(attempt,base,cap,retry_after=None):\n    if type(attempt) is not int or attempt<0 or not all(math.isfinite(x) and x>=0 for x in [base,cap]): raise ValueError("invalid input")\n    if retry_after is not None and (not math.isfinite(retry_after) or retry_after<0): raise ValueError("invalid Retry-After")\n    local=0 if base==0 else min(cap,base*2.0**min(attempt,1023))\n    return max(local,retry_after or 0)\n',
 [('delay(0,1,30)',1),('delay(3,1,30)',8),('delay(6,1,30,60)',60),('delay(10000,0,30)',0),('delay(1,2,3)',3)]),
('archive-pagination','Implement page(text,start,chars) using Unicode character offsets, with integer start>=0 and chars>=1. start beyond len(text) raises ValueError. Return dict with text substring, start, end, and eof=(end==len(text)). Preserve all characters exactly.',
 'def page(text,start,chars):\n    return {"text":text[start:start+chars],"start":start,"end":start+chars,"eof":False}\n',
 'def page(text,start,chars):\n    if type(start) is not int or type(chars) is not int or not 0<=start<=len(text) or chars<1: raise ValueError("invalid window")\n    end=min(len(text),start+chars)\n    return {"text":text[start:end],"start":start,"end":end,"eof":end==len(text)}\n',
 [('page("a中😀z",1,2)',{'text':'中😀','start':1,'end':3,'eof':False}),('page("abc",2,9)',{'text':'c','start':2,'end':3,'eof':True}),('page("abc",3,1)',{'text':'','start':3,'end':3,'eof':True}),('page("",0,1)',{'text':'','start':0,'end':0,'eof':True}),('page("abc",0,1)',{'text':'a','start':0,'end':1,'eof':False})]),
]


def oracle_test_source(cases) -> str:
    """The generator and local oracle validator use the exact same known code."""
    test='import unittest\nfrom module import *\nclass Contract(unittest.TestCase):\n'
    for i,(expr,expected) in enumerate(cases):
        test+=f'    def test_{i}(self):\n        self.assertEqual({expr}, {expected!r})\n'
    return test+'if __name__ == "__main__": unittest.main()\n'


def generate_tasks(out:Path, image='python:3.11-slim'):
    if out.exists():raise ValueError('Development task destination already exists; never overwrite a frozen dataset')
    if not image or any(ch in image for ch in '\r\n \t'):raise ValueError('Use a single explicit Docker image reference')
    out.mkdir(parents=True);manifest=[]
    for name,instruction,broken,solution,cases in TASKS:
        root=out/name
        atomic_write(root/'instruction.md',instruction+'\n\nFix /app/module.py using the standard library only. No network access or extra dependencies are necessary. Do not change the public API.\n')
        atomic_write(root/'environment/app/module.py',broken)
        atomic_write(root/'environment/Dockerfile',f'FROM {image}\nWORKDIR /app\nCOPY app/ /app/\n')
        atomic_write(root/'task.toml',f'''schema_version = "1.4"
[task]
name = "rever-owned/{name}"
version = "0.1.0"
description = "Owned SEARCH-only plumbing canary, not an independent benchmark"
authors = [{{name = "ReVer-Pi research kit"}}]
keywords = ["owned", "search-only", "python"]
[agent]
timeout_sec = 300
[verifier]
timeout_sec = 30
[environment]
cpus = 1
memory_mb = 1024
storage_mb = 2048
network_mode = "no-network"
''')
        test=oracle_test_source(cases)
        atomic_write(root/'tests/check.py',test)
        atomic_write(root/'tests/test.sh','#!/bin/sh\nset -u\nmkdir -p /logs/verifier\nif PYTHONPATH=/app python /tests/check.py; then printf "1" > /logs/verifier/reward.txt; else printf "0" > /logs/verifier/reward.txt; fi\n')
        # Reference solution is controller-side; copied ONLY to Oracle environments by Harbor.
        atomic_write(root/'solution/solve.sh',"#!/bin/sh\nset -eu\ncat > /app/module.py <<'REVER_OWNED_SOLUTION'\n"+solution+'REVER_OWNED_SOLUTION\n')
        atomic_write(root/'LICENSE.txt','CC0-1.0. These original synthetic smoke tasks are dedicated to the public domain.\n')
        manifest.append({'id':'owned-'+name,'path':name,'split':'search','source_group':'rever-owned-smoke-family-v1',
             'provenance':{'template':'rever-owned-smoke-family-v1','generator':'reverpi.devtasks','benchmark':''},'license':'CC0-1.0','synthetic':True})
    atomic_write(out/'tasks.jsonl','\n'.join(canonical(r) for r in manifest)+'\n')
    atomic_write(out/'DATA_CARD.json',canonical({'split':'search','independent_benchmark':False,'task_count':len(manifest),
        'source_groups':1,'image':image,'image_is_digest_pinned':'@sha256:' in image,'requires_harbor_docker_acceptance':True,
        'note':'Pin a compatible image digest and host architecture for reproducibility. This collection is for plumbing, not a claim of research generalization.'}))
    return {'manifest':str(out/'tasks.jsonl'),'tasks':len(manifest),'split':'search','independent_benchmark':False}


def validate_oracles(out:Path):
    """Execute ONLY this generator's own known Python files locally, not arbitrary datasets."""
    rows=[]
    for name,_,broken,solution,cases in TASKS:
        tests=oracle_test_source(cases).encode('utf-8')
        path=out/name/'tests/check.py'
        if path.is_symlink() or path.read_bytes()!=tests:
            raise ValueError('Owned oracle file differs from generator; refusing to execute it')
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);atomic_write(p/'check.py',tests)
            record={'task':name}
            for label,code in [('broken',broken),('oracle',solution)]:
                atomic_write(p/'module.py',code)
                shutil.rmtree(p/'__pycache__',ignore_errors=True)
                result=subprocess.run([sys.executable,'-B',str(p/'check.py')],cwd=p,capture_output=True,timeout=15,env={'PATH':os.environ.get('PATH',''),'PYTHONPATH':str(p),'PYTHONDONTWRITEBYTECODE':'1'})
                record[label+'_exit_code']=result.returncode
                record[label+'_log']=result.stderr.decode('utf8','replace')[-6000:]
            record['valid']=record['broken_exit_code']!=0 and record['oracle_exit_code']==0;rows.append(record)
    report={'all_valid':all(r['valid'] for r in rows),'tasks':rows,'mode':'local_owned_Python_contracts_only','docker_or_harbor_executed':False}
    atomic_write(out/'oracle_validation.json',canonical(report));return report
