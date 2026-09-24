#!/usr/bin/env python3
"""Collect host-only readiness; optionally try a cold, locked npm install in a copy.

No Provider construction, no model/auth/account probe, no budget authorization,
no Docker image pull/run/removal, and no modification of the project worktree.
Default only inspects local capabilities. --cold-npm-ci authorizes dependency
installation (not model calls) in a NEW disposable directory with an empty HOME.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from verify_offline import Steps, atomic_report, validate_destination
ROOT=Path(__file__).resolve().parents[1]

def clean_host_environment(home:Path)->dict[str,str]:
    env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL','TERM','TZ') if k in os.environ}
    env.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'config'),PYTHONDONTWRITEBYTECODE='1',
        NO_COLOR='1',NPM_CONFIG_USERCONFIG=str(home/'empty.npmrc'),NPM_CONFIG_AUDIT='false',NPM_CONFIG_FUND='false')
    return env

def collect_pins()->dict:
    result={}
    for line in (ROOT/'requirements.lock').read_text().splitlines():
        if '==' not in line or line.lstrip().startswith('#'):continue
        name,expected=line.split('==',1)
        try:actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:actual=None
        result[name]={'expected':expected,'actual':actual,'match':actual==expected}
    return result

def stage_cold_install(root:Path,out:Path)->tuple[Path,str]:
    """Copy exactly the dependency/test tree and its declared Python sibling."""
    stage=out/'locked-install'/'pi';stage.mkdir(parents=True,exist_ok=False)
    for name in ('package.json','package-lock.json','tsconfig.json'):
        shutil.copyfile(root/'pi'/name,stage/name)
    for name in ('src','tests'):
        shutil.copytree(root/'pi'/name,stage/name)
    runner=stage.parent/'src/reverpi/revalidation.py';runner.parent.mkdir(parents=True)
    shutil.copyfile(root/'src/reverpi/revalidation.py',runner)
    return stage,hashlib.sha256(runner.read_bytes()).hexdigest()

def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--cold-npm-ci',action='store_true')
    p.add_argument('--install-timeout',type=int,default=600)
    a=p.parse_args()
    if not 10<=a.install_timeout<=900:p.error('install timeout must be 10..900 seconds')
    out=validate_destination(ROOT,a.out);out.mkdir(parents=True,mode=0o700)
    for d in ('logs','home'):(out/d).mkdir(mode=0o700)
    (out/'home/empty.npmrc').write_text('')
    env=clean_host_environment(out/'home');steps=Steps(out,env)
    pins=collect_pins();node=shutil.which('node');npm=shutil.which('npm');docker=shutil.which('docker')
    node_version=None
    if node:
        r=steps.run('node_version',[node,'--version'],timeout=15)
        if r['status']=='passed':node_version=(out/'logs/node_version.log').read_text().strip()
    match=re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)',node_version or '')
    node_ok=bool(match and tuple(map(int,match.groups()))>=(22,19,0))
    try:harbor=importlib.metadata.version('harbor')
    except importlib.metadata.PackageNotFoundError:harbor=None
    docker_ok=False
    if docker:
        docker_ok=steps.run('docker_version',[docker,'version','--format','{{json .}}'],timeout=20)['status']=='passed'
    installation={'requested':a.cold_npm_ci,'passed':False,'network_purpose':'npm dependency installation only',
        'registry':'https://registry.npmjs.org','lock_sha256':hashlib.sha256((ROOT/'pi/package-lock.json').read_bytes()).hexdigest()}
    if a.cold_npm_ci:
        if not node_ok or not npm:
            installation['status']='blocked_node_or_npm'
        else:
            stage,runner_sha=stage_cold_install(ROOT,out)
            installation['python_runner_sha256']=runner_sha
            row=steps.run('cold_npm_ci',[npm,'ci','--no-audit','--no-fund','--cache',str(out/'empty-npm-cache'),
                '--registry=https://registry.npmjs.org','--fetch-retries=0','--fetch-timeout=10000'],cwd=stage,timeout=a.install_timeout)
            installation['status']='install_failed' if row['status']!='passed' else 'installed_not_yet_checked'
            if row['status']=='passed':
                same=hashlib.sha256((stage/'package-lock.json').read_bytes()).hexdigest()==installation['lock_sha256']
                check=steps.run('cold_typecheck',[node,'node_modules/typescript/bin/tsc','--noEmit'],cwd=stage,timeout=120)
                tests=steps.run('cold_node_tests',[node,'--experimental-strip-types','--test',
                    *[str(f.relative_to(stage)) for f in sorted((stage/'tests').glob('*.test.mjs'))]],cwd=stage,timeout=180)
                installation.update(passed=same and check['status']=='passed' and tests['status']=='passed',lock_unchanged=same)
                installation['status']='passed' if installation['passed'] else 'validation_failed'
    else:installation['status']='not_requested_reuse_existing_evidence_when_identical'
    result={'schema':1,'kind':'remaining_host_readiness','node':node_version,'node_ready':node_ok,
        'python':sys.version.split()[0],'python_pins':pins,'harbor':harbor,'harbor_pinned':harbor=='0.22.0',
        'docker_daemon_reachable':docker_ok,'cold_npm_install':installation,'steps':steps.rows,
        'model_requests_authorized':False,'new_model_calls':0,'model_budget_authorized':0,
        'docker_harbor_integration_tested':False,'supplier_account_probed':False,
        'processes_on_user_server_managed':False,'private_credentials_collected':False}
    result['ready_for_host_only_container_tests']=node_ok and all(v['match'] for v in pins.values()) and harbor=='0.22.0' and docker_ok
    atomic_report(out/'readiness.json',result)
    print(json.dumps({'report':str(out/'readiness.json'),'ready_for_host_only_container_tests':result['ready_for_host_only_container_tests'],
        'model_requests_authorized':False},indent=2))
    # Code 2 means honest missing capability, not a reason to change versions.
    return 0 if result['ready_for_host_only_container_tests'] and (not a.cold_npm_ci or installation['passed']) else 2
if __name__=='__main__':raise SystemExit(main())
