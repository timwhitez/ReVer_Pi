#!/usr/bin/env python3
"""Zero-provider-cost integration: installed Pi -> real gateway -> Python verifier.

The actor is a deterministic scripted HTTP transport, not a reasoning model.
This validates wiring only. It never grants native/quality/external certification.
Uses a temporary disposable workspace; neither Pi nor this launcher is a sandbox.
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import httpx
import uvicorn
from reverpi.config import Provider, StudyConfig, Budget
from reverpi.gateway import create_app
from reverpi.launcher import launch
from reverpi.util import canonical, atomic_write, digest, source_manifest

@contextlib.contextmanager
def server(app):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    srv=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    worker=threading.Thread(target=lambda:srv.run(sockets=[sock]),daemon=True);worker.start()
    deadline=time.monotonic()+8
    while not srv.started and worker.is_alive() and time.monotonic()<deadline:time.sleep(.01)
    try:
        if not srv.started:raise RuntimeError('Loopback gateway did not start')
        yield f'http://127.0.0.1:{port}'
    finally:
        srv.should_exit=True;worker.join(8);sock.close()
        if worker.is_alive():raise RuntimeError('Loopback gateway thread did not terminate')

async def run(root:Path,out:Path):
    out=out.resolve()
    if out.exists():raise ValueError('Use a fresh output directory; smoke runs are never overwritten')
    out.mkdir(parents=True,mode=0o700)
    requests=[];receipts=[]
    provider=Provider(name='scripted-native-contract',mock=True,base_url='http://127.0.0.1:1/v1',model='mock-reasoner',
        requests_per_minute=100000,tokens_per_minute=100000000,retry={'total_seconds':30,'base_seconds':0,'cap_seconds':0})
    def supplier(request):
        body=json.loads(request.content);requests.append({'messages':len(body['messages']),
            'tool_names':[t['function']['name'] for t in body.get('tools',[])]})
        assert 'revalidate_evidence' in requests[-1]['tool_names']
        tools=[m for m in body['messages'] if m['role']=='tool']
        if not tools:
            msg={'role':'assistant','content':None,'tool_calls':[{'id':'call_current_check','type':'function',
                'function':{'name':'revalidate_evidence','arguments':'{"verifier_id":"check"}'}}]};finish='tool_calls'
        else:
            receipt=json.loads(tools[-1]['content']);assert receipt['schema']=='reverpi.verification-receipt.v1'
            receipts.append(receipt)
            msg={'role':'assistant','content':'CURRENT_CHECK='+str(receipt['passed']).lower()};finish='stop'
        return httpx.Response(200,json={'id':'scripted-contract','model':provider.model,
            'choices':[{'index':0,'message':msg,'finish_reason':finish}],
            'usage':{'prompt_tokens':11,'completion_tokens':7}})
    cfg=StudyConfig(methods=['pi_original','mask'],early_response_headers=True,response_heartbeat_seconds=.1,
                    budget=Budget(max_total_tokens=1000000,per_cell_tokens=200000))
    app=create_app(provider,cfg,out/'gateway',transport=httpx.MockTransport(supplier))
    results=[]
    with tempfile.TemporaryDirectory(prefix='rever-native-current-') as disposable:
        base=Path(disposable);workspace=base/'workspace';workspace.mkdir()
        registry=base/'registry.json'
        registry.write_text(canonical({'schema':1,'verifiers':[{'id':'check','argv':[sys.executable,'-B','check.py'],
            'inputs':['check.py'],'timeout_seconds':5}]}))
        registry.chmod(0o400);registry_sha=hashlib.sha256(registry.read_bytes()).hexdigest()
        prompt=base/'prompt.txt';prompt.write_text('Run the registered current-state verifier check and report its result. This is an interface smoke test.')
        with server(app) as url:
            for method in cfg.methods:
                for phase,expected in [('A',True),('B',False)]:
                    (workspace/'check.py').write_text('assert 1 == '+('1' if expected else '2')+'\n')
                    sid=method+'_'+phase;token_file=base/'token.txt'
                    token_file.write_text(app.state.sessions.create(sid,method));token_file.chmod(0o600)
                    before=len(receipts);before_requests=len(requests)
                    status=await launch(root,workspace,prompt,out/sid,url,token_file,wall_seconds=60,max_tools=3,max_turns=4,
                        acknowledge_unsandboxed=True,verifier_registry=registry,verifier_registry_sha=registry_sha,max_revalidations=1)
                    row={'method':method,'phase':phase,'expected_passed':expected,'status':status['status'],
                        'new_receipts':len(receipts)-before,'mock_requests':len(requests)-before_requests,
                        'task_status_sha256':hashlib.sha256((out/sid/'task.json').read_bytes()).hexdigest()}
                    if len(receipts)>before:row['observed_passed']=receipts[-1]['passed'];row['receipt']=receipts[-1]
                    results.append(row)
                    atomic_write(out/'progress.json',canonical(results))
                    if status['status']!='completed' or row['new_receipts']!=1 or row.get('observed_passed') is not expected:
                        raise RuntimeError(f'Native integration failed at {sid}; inspect immutable logs')
        token_file.unlink(missing_ok=True)
    report={'schema':1,'source_sha256':digest(source_manifest(root)),'cells':results,'requests':requests,
        'all_interface_checks_passed':True,'mock_provider':True,'scripted_actor':True,'paid_model_calls':0,
        'mock_usage_not_real_cost':app.state.ledger.totals(),'native_gate_eligible':False,'harbor_path_exercised':False,
        'capability_improvement_established':False,'sandbox_provided':False,'gateway_stopped':True,
        'scope':'Real pinned Pi, main extension, loopback HTTP, real Python checks; no model selection or benchmark claim'}
    atomic_write(out/'report.json',canonical(report));return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True,type=Path)
    a=p.parse_args();r=asyncio.run(run(Path(__file__).resolve().parents[1],a.out))
    print(json.dumps({'cells':len(r['cells']),'all_interface_checks_passed':True,'paid_model_calls':0},indent=2))
if __name__=='__main__':main()
