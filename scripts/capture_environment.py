#!/usr/bin/env python3
"""Run local native checks and freeze the resolved environment BEFORE reviews.

Does not install dependencies, call Providers or certify a Docker/Pi live rollout.
The resulting code identity changes: regenerate reviews/worker after capturing.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from reverpi.util import atomic_write,canonical,source_manifest,digest

def invoke(argv,cwd=ROOT,timeout=300):
    result=subprocess.run(argv,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=timeout,check=False)
    if result.returncode:raise RuntimeError('Local check failed: '+repr(argv)+'\n'+result.stdout[-10000:])
    return result.stdout

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--native',action='store_true',required=True);parser.parse_args()
    if sys.version_info<(3,12):raise RuntimeError('Native Harbor requires Python >=3.12')
    if importlib.metadata.version('harbor')!='0.22.0':raise RuntimeError('Install exactly Harbor 0.22.0')
    from reverpi.launcher import pi_paths
    _,_,node_version,pi_version=pi_paths(ROOT)
    if not (ROOT/'pi/package-lock.json').is_file():raise RuntimeError('Resolve npm dependencies and retain package-lock.json first')
    checks={}
    for name,argv,cwd in [
        ('python_tests',[sys.executable,'-m','pytest','-q'],ROOT),
        ('pi_upstream_typecheck',['npm','run','typecheck'],ROOT/'pi'),
        ('pi_contract_tests',['npm','test'],ROOT/'pi'),
        ('harbor_help',['harbor','run','--help'],ROOT),
    ]:
        text=invoke(argv,cwd);path=ROOT/'reports/environment_checks'/f'{name}.log';atomic_write(path,text)
        checks[name]={'passed':True,'output_sha256':hashlib.sha256(text.encode()).hexdigest()}
    freeze=invoke([sys.executable,'-m','pip','freeze','--all'])
    atomic_write(ROOT/'reports/environment_checks/pip-freeze.txt',freeze)
    base=source_manifest(ROOT);base.pop('environment.lock.json',None)
    report={'schema':1,'python':platform.python_version(),'platform':platform.platform(),'machine':platform.machine(),
        'node':node_version,'pi':pi_version,'harbor':importlib.metadata.version('harbor'),'native_checks_passed':True,
        'live_native_acceptance_performed':False,'full_resolved_pip_freeze':freeze.splitlines(),
        'npm_lock_sha256':hashlib.sha256((ROOT/'pi/package-lock.json').read_bytes()).hexdigest(),
        'checks':checks,'code_without_environment_sha':digest(base),
        'note':'Local tests/typecheck only. Provider probe + real native-acceptance remain separate mandatory gates.'}
    atomic_write(ROOT/'environment.lock.json',canonical(report));print(json.dumps({'saved':'environment.lock.json','next':'review/probe/worker/gateway/native-acceptance; never change this lock inside a frozen experiment'},indent=2))
if __name__=='__main__':main()
