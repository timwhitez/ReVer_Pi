#!/usr/bin/env python3
"""Explicit-boundary, zero-model Pi compaction/continuation diagnostic.

Fixed mock transport ONLY, actual Pi + loopback gateway + native read tool.
Keeps 131072/65536/20000 default window/output/recent settings. An operator
requests manual compaction at a predeclared checkpoint, NOT an autonomous policy.
Never creates live/native/G4 approval. Never overwrites an existing output.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import httpx
from reverpi.acceptance import RPC
from reverpi.canary_recovery import select_probe, verify_recovery
from reverpi.config import Provider, StudyConfig, Budget
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.launcher import pi_paths, command, isolated_env, settings
from reverpi.processes import stop_process_group
from reverpi.util import atomic_write, canonical, digest, source_manifest, strict_json_loads
from native_revalidation_smoke import server

CONDITIONS=('pi_original','tools_no_compact','mask_checkpoint')
PROTOCOLS=('chat_completions','responses')
PRESSURES=('short','long','oversized_recent')
MARKER='HISTORICAL_MARKER_73548e4bab1641b796eb0af06f733914'

def require(value:bool,message:str)->None:
    if not value: raise ValueError(message)

def text(content)->str:
    if isinstance(content,str):return content
    require(isinstance(content,list),'Expected text content')
    return ''.join(p.get('text','') for p in content if isinstance(p,dict))

def wire_view(body:dict,protocol:str)->tuple[list[dict],list[str]]:
    rows=body['messages'] if protocol=='chat_completions' else body['input']
    tools=body.get('tools',[])
    names=[t['function']['name'] if protocol=='chat_completions' else t['name'] for t in tools]
    return rows,sorted(names)

def reply(protocol:str,index:int,call:dict|None,answer:str)->dict:
    usage={'input_tokens':1024,'output_tokens':16}
    if protocol=='chat_completions':
        m={'role':'assistant','content':answer}
        if call:m['tool_calls']=[{'id':call['id'],'type':'function','function':{'name':call['name'],'arguments':canonical(call['arguments'])}}]
        return {'id':f'scripted-{index}','model':'mock-reasoner','choices':[{'index':0,'message':m,
            'finish_reason':'tool_calls' if call else 'stop'}],
            'usage':{'prompt_tokens':1024,'completion_tokens':16}}
    output=([{'type':'function_call','id':f'fc_{index}','call_id':call['id'],'name':call['name'],
        'arguments':canonical(call['arguments']),'status':'completed'}] if call else
        [{'type':'message','id':f'msg_{index}','role':'assistant','status':'completed',
          'content':[{'type':'output_text','text':answer}]}])
    return {'id':f'scripted-{index}','model':'mock-reasoner','status':'completed','output':output,'usage':usage}

class ScriptedActor:
    def __init__(self,protocol:str,case:Path):
        self.protocol=protocol;self.case=case;self.requests=[]
    def __call__(self,request:httpx.Request)->httpx.Response:
        body=strict_json_loads(request.content);rows,names=wire_view(body,self.protocol)
        require(body.get('model')=='mock-reasoner','Unexpected model; no live route allowed')
        require(request.url.host=='127.0.0.1','Unexpected upstream host')
        users=[(i,m) for i,m in enumerate(rows) if m.get('role')=='user']
        require(bool(users),'Missing script phase')
        i,last=users[-1];prompt=text(last['content'])
        tail=rows[i+1:];tool_done=any(x.get('role')=='tool' or x.get('type')=='function_call_output' for x in tail)
        index=len(self.requests);call=None
        if prompt.startswith('OBSERVE '):
            filename=prompt.removeprefix('OBSERVE ').strip()
            require(bool(re.fullmatch(r'page_\d{2}\.txt',filename)),'Invalid fixture filename')
            if not tool_done:call={'id':f'call_{index}','name':'read','arguments':{'path':filename}}
            answer='OBSERVED; final follow-up remains pending.'
            phase='history'
        elif prompt=='FOLLOWUP: confirm continuation using the current context.':
            phase='followup';answer='CONTINUED'
        elif prompt.startswith('RESTORE '):
            args=strict_json_loads(prompt.removeprefix('RESTORE '))
            require('recover_evidence' in names,'Recovery unavailable in selected condition')
            if not tool_done:call={'id':f'call_{index}','name':'recover_evidence','arguments':args}
            phase='recovery';answer='RECOVERED'
        else:raise ValueError('Unrecognized phase; no implicit model fallback')
        name=f'wire_{index:03}.json';atomic_write(self.case/name,canonical(body))
        row={'index':index,'phase':phase,'body_path':name,'body_sha256':digest(body),'tool_names':names,
            'wire_content_bytes':len(canonical(rows).encode()),'marker_in_wire':MARKER in canonical(rows)}
        self.requests.append(row)
        atomic_write(self.case/'requests.json',canonical(self.requests))
        return httpx.Response(200,json=reply(self.protocol,index,call,answer))

def make_fixture(workspace:Path,pressure:str)->list[str]:
    require(pressure in PRESSURES,'Unknown fixture')
    # Several real read-tool results; each page stays below Pi's normal read cap.
    count={'short':1,'long':16,'oversized_recent':8}[pressure]
    lines={'short':4,'long':100,'oversized_recent':240}[pressure]
    names=[]
    for index in range(count):
        name=f'page_{index:02}.txt';names.append(name)
        chunks=[f'{index:02}:{j:03}: {hashlib.sha256(f"{index}:{j}".encode()).hexdigest()}\n' for j in range(lines)]
        if index==0:chunks.insert(len(chunks)//2,MARKER+'\n')
        (workspace/name).write_text(''.join(chunks),encoding='utf-8')
    return names

async def inspect_boundary(node:str,case:Path)->dict:
    proc=await asyncio.create_subprocess_exec(node,str(ROOT/'scripts/inspect_compaction_boundary.mjs'),
        str(case/'session.jsonl'),str(case/'agent-config/settings.json'),stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,start_new_session=True)
    try:
        stdout,stderr=await asyncio.wait_for(proc.communicate(),30)
        require(proc.returncode==0,'Read-only pinned preparation inspector failed: '+stderr.decode()[:200])
        return strict_json_loads(stdout)
    finally:await stop_process_group(proc)

async def case_run(root:Path,out:Path,protocol:str,condition:str,pressure:str)->dict:
    case=out/f'{protocol}__{condition}__{pressure}';case.mkdir(mode=0o700)
    for name in ('workspace','home','agent-config'):(case/name).mkdir(mode=0o700)
    node,cli,nv,pv=pi_paths(root)
    st=settings('low',65536,131072)
    atomic_write(case/'agent-config/settings.json',canonical(st))
    names=make_fixture(case/'workspace',pressure)
    actor=ScriptedActor(protocol,case)
    provider=Provider(name='native-compaction-offline-only',protocol=protocol,mock=True,
        model='mock-reasoner',base_url='http://127.0.0.1:1/v1',effort='low',max_output_tokens=65536,
        requests_per_minute=100000,tokens_per_minute=100000000,concurrency=1,
        retry={'total_seconds':60,'base_seconds':0,'cap_seconds':0})
    method='pi_original' if condition=='pi_original' else 'mask'
    cfg=StudyConfig(name='native-compaction-contract-only',methods=[method],
        budget=Budget(max_total_tokens=10_000_000,per_cell_tokens=5_000_000),
        early_response_headers=True,response_heartbeat_seconds=.1)
    app=create_app(provider,cfg,case/'gateway',transport=httpx.MockTransport(actor))
    sid='offline_boundary';token=app.state.sessions.create(sid,method)
    row={'condition':condition,'protocol':protocol,'pressure':pressure,'method':method,
        'case_dir':case.name,'status':'running','settings':st,'node':nv,'pi':pv,
        'actor_is_scripted':True,'paid_model_calls':0,'native_gate_eligible':False,
        'automatic_policy_verified':False,'diagnostic_intervention':'explicit RPC compact at predefined checkpoint'}
    proc=None;stderr_task=None
    try:
        with server(app) as url:
            argv=command(node,cli,root/'pi/src/index.ts',case/'session.jsonl',provider.model,'low',mode='rpc')
            env=isolated_env(case,url,token,40,40)
            # Preserve the wrapper's anti-accidental-egress guard in the Pi child.
            for key in ('LD_PRELOAD','REVER_OFFLINE_GUARD_LOG'):
                if key in os.environ:env[key]=os.environ[key]
            proc=await asyncio.create_subprocess_exec(*argv,cwd=case/'workspace',env=env,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                start_new_session=True,limit=8*1024**2)
            with (case/'rpc.jsonl').open('xb') as logfile,(case/'stderr.log').open('xb') as err:
                async def drain():
                    count=0
                    while data:=await proc.stderr.read(65536):
                        count+=len(data);require(count<4*1024**2,'Bounded stderr exceeded');err.write(data);err.flush()
                stderr_task=asyncio.create_task(drain());rpc=RPC(proc,logfile,deadline=60)
                for name in names:await rpc.request('prompt',message='OBSERVE '+name)
                boundary=await inspect_boundary(node,case);atomic_write(case/'boundary.json',canonical(boundary))
                row['boundary']=boundary
                before_session=(case/'session.jsonl').read_bytes()
                before_session_sha=hashlib.sha256(before_session).hexdigest()
                (case/'checkpoint_session.jsonl').write_bytes(before_session)
                row['checkpoint_session_sha256']=before_session_sha
                require(boundary['legal_preparation'] is (pressure!='short'),'Fixture did not produce expected preparation contract')
                require(boundary['usage_gate_crossed'] is False,'Scripted usage unexpectedly triggered automatic compaction')
                should_intervene=condition=='mask_checkpoint' and boundary['legal_preparation']
                if should_intervene:
                    try:
                        result,_=await rpc.request('compact')
                    except LabError as exc:
                        if pressure!='oversized_recent':raise
                        require(exc.kind=='rpc_command','Unexpected native rejection')
                        state=app.state.sessions.get(sid)
                        require(state['revision']==0 and state['memory'] is None,'Capacity rejection changed memory')
                        require(hashlib.sha256((case/'session.jsonl').read_bytes()).hexdigest()==before_session_sha,'Capacity rejection rewrote original history')
                        with app.state.sessions.db() as db:
                            operations=db.execute('SELECT state FROM compact_ops WHERE session=?',(sid,)).fetchall()
                        require(len(operations)==1 and operations[0][0]=='failed','Failed compaction operation not retained')
                        require('compression_capacity' in (case/'rpc.jsonl').read_text(),'Wrong rejection cause')
                        row.update(outcome='expected_capacity_rejection',revision_at_followup=0,intervention_invoked=True,
                            followup_dispatched=False,original_history_unchanged=True,requests=actor.requests,
                            mock_accounting=app.state.ledger.totals(),status='passed')
                        return row
                    require(pressure!='oversized_recent','Expected capacity rejection did not occur')
                    require(isinstance(result,dict) and bool(result.get('summary')),'No native compaction result')
                    atomic_write(case/'compact_result.json',canonical(result))
                state=app.state.sessions.get(sid)
                require(state['revision']==int(should_intervene),'Unexpected gateway revision')
                row['revision_at_followup']=state['revision'];row['intervention_invoked']=should_intervene
                row['calls_before_followup']=len(actor.requests)
                _,events=await rpc.request('prompt',message='FOLLOWUP: confirm continuation using the current context.')
                follow=[x for x in actor.requests if x['phase']=='followup']
                require(len(follow)==1,'Missing/duplicate follow-up dispatch')
                row['followup']=follow[0]
                require(follow[0]['marker_in_wire'] is (not should_intervene),'Old evidence visibility did not match intervention')
                if should_intervene:
                    memory=strict_json_loads(state['memory']);body=strict_json_loads((case/follow[0]['body_path']).read_bytes())
                    msg,_=wire_view(body,protocol)
                    row['memory_in_followup']=any(memory['text'] in text(m.get('content',[])) for m in msg if m.get('role')=='user')
                    require(row['memory_in_followup'],'Committed memory was not sent to the next provider request')
                    probe=select_probe(case/'gateway/archive.sqlite',sid,MARKER,6000)
                    _,recovery=await rpc.request('prompt',message='RESTORE '+canonical({'handle':probe.handle,'start':probe.start,'chars':probe.chars}))
                    row['recovery']=verify_recovery(recovery,probe)
                require(any(e.get('type')=='message_end' and e.get('message',{}).get('role')=='assistant' for e in events), 'No post-compaction assistant')
                row['post_compaction_response_verified']=True
                if stderr_task.done():stderr_task.result()
            row['mock_accounting']=app.state.ledger.totals();row['requests']=actor.requests
            row['status']='passed';row['outcome']='continuation_verified';row['followup_dispatched']=True
    except BaseException as exc:
        row.update(status='failed',error_type=type(exc).__name__,error_message=str(exc)[:1000]);raise
    finally:
        app.state.sessions.disable(sid)
        await stop_process_group(proc)
        if stderr_task:
            if not stderr_task.done():stderr_task.cancel()
            try:await stderr_task
            except asyncio.CancelledError:pass
        row['gateway_stopped']=True;row['session_revoked']=True
        atomic_write(case/'report.json',canonical(row))
    return row

async def run(out:Path,protocols:tuple[str,...]=PROTOCOLS)->dict:
    require(not out.exists(),'Fresh output required; no automatic replay')
    require(out.resolve()!=ROOT and not out.resolve().is_relative_to(ROOT),'Output outside source required')
    out.mkdir(parents=True,mode=0o700)
    source=digest(source_manifest(ROOT));rows=[]
    report={'schema':1,'kind':'explicit_boundary_compaction_contract','source_sha256':source,
        'conditions':list(CONDITIONS),'protocols':list(protocols),'rows':rows,'paid_model_calls':0,
        'scripted_actor':True,'manual_checkpoint_not_default_agent_policy':True,
        'benchmark_or_quality_claim':False,'native_gate_eligible':False,'status':'running'}
    try:
        for protocol in protocols:
            for pressure in PRESSURES:
                for condition in (('mask_checkpoint',) if pressure=='oversized_recent' else CONDITIONS):
                    row=await case_run(ROOT,out,protocol,condition,pressure);rows.append(row)
                    atomic_write(out/'report.json',canonical(report))
        for protocol in protocols:
            for pressure in ('short','long'):
                group={x['condition']:x for x in rows if x['protocol']==protocol and x['pressure']==pressure}
                require(group['tools_no_compact']['followup']['tool_names']==group['mask_checkpoint']['followup']['tool_names'],'Tool matched control differs')
                if pressure=='long':
                    require(group['mask_checkpoint']['followup']['wire_content_bytes']<group['tools_no_compact']['followup']['wire_content_bytes'],'Post-compaction representation did not shrink')
        require(digest(source_manifest(ROOT))==source,'Source changed during verification')
        report['status']='passed'
    except BaseException as exc:
        report.update(status='failed',error_type=type(exc).__name__,error_message=str(exc)[:1000]);raise
    finally:atomic_write(out/'report.json',canonical(report))
    return report

def main()->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--protocol',choices=['both',*PROTOCOLS],default='both')
    args=p.parse_args()
    if os.environ.get('REVER_OFFLINE_VERIFICATION')!='1' or not os.environ.get('LD_PRELOAD'):
        p.error('Run through run_local_contract.py so the loopback-only guard is active')
    protocols=PROTOCOLS if args.protocol=='both' else (args.protocol,)
    result=asyncio.run(run(args.out,protocols));print(canonical({'status':result['status'],'cells':len(result['rows']),'paid_model_calls':0}))
    return 0
if __name__=='__main__':raise SystemExit(main())
