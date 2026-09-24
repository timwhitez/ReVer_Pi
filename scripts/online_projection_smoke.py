#!/usr/bin/env python3
"""Real Pi, one prompt and uninterrupted tool loop; scripted actor, ZERO model API.

Automatic request projection is tested, not native threshold compaction, learned
selection, real model behavior, dollar savings, or benchmark ability. No manual
compact RPC, hidden provider probe, SDK fallback or real model route is accepted.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import httpx
from reverpi.acceptance import RPC
from reverpi.config import Provider,StudyConfig,Budget
from reverpi.gateway import create_app
from reverpi.launcher import pi_paths,command,isolated_env,settings
from reverpi.processes import stop_process_group
from reverpi.util import canonical,atomic_write,digest,source_manifest,strict_json_loads
from native_revalidation_smoke import server
from native_compaction_smoke import reply,wire_view,text

CONDITIONS=('pi_original','tools_observe','online_apply')
PROTOCOLS=('chat_completions','responses')
MARKER='HISTORICAL_MARKER_9baf4477219db8809eec246607c77954'
PROMPT='Inspect page_00.txt through page_04.txt in order, then report the historical marker from the first page. This is an owned interface fixture.'


def require(value,message):
    if not value:raise ValueError(message)


def make_pages(workspace:Path,pressure:str):
    for i in range(5):
        n=8 if pressure=='short' else 170
        lines=[f'{i:02d}:{j:03d}: '+hashlib.sha256(f'owned-{i}-{j}'.encode()).hexdigest()+'\n' for j in range(n)]
        if i==0:lines.insert(n//2,MARKER+'\n')
        (workspace/f'page_{i:02d}.txt').write_text(''.join(lines),encoding='utf-8')


class Actor:
    def __init__(self,protocol:str,case:Path):self.protocol=protocol;self.case=case;self.requests=[]
    def __call__(self,request:httpx.Request):
        require(request.url.host=='127.0.0.1','Scripted upstream must be loopback')
        body=strict_json_loads(request.content);require(body.get('model')=='mock-reasoner','Unexpected model')
        rows,names=wire_view(body,self.protocol);index=len(self.requests)
        require(index<12,'Scripted fixture exceeded fixed request count')
        calls=[]
        if self.protocol=='chat_completions':
            for row in rows:
                for c in row.get('tool_calls',[]):
                    calls.append({'id':c['id'],'name':c['function']['name'],'arguments':strict_json_loads(c['function']['arguments'])})
            results={r['tool_call_id']:r['content'] for r in rows if r.get('role')=='tool'}
        else:
            calls=[{'id':r['call_id'],'name':r['name'],'arguments':strict_json_loads(r['arguments'])} for r in rows if r.get('type')=='function_call']
            results={r['call_id']:r['output'] for r in rows if r.get('type')=='function_call_output'}
        read_calls=[c for c in calls if c['name']=='read'];recovery=[c for c in calls if c['name']=='recover_evidence']
        call=None;phase='finish';answer='SCRIPTED_CONTINUED'
        if len(read_calls)<5:
            phase='read';call={'id':f'owned_call_{index}','name':'read','arguments':{'path':f'page_{len(read_calls):02d}.txt'}}
        elif MARKER not in canonical(rows) and not recovery:
            require('recover_evidence' in names,'Removed observation is not recoverable')
            phase='search';call={'id':f'owned_call_{index}','name':'recover_evidence','arguments':{'query':'HISTORICAL_MARKER_','chars':1000}}
        elif recovery and 'query' in recovery[-1]['arguments']:
            result=strict_json_loads(results[recovery[-1]['id']]);matches=result.get('matches',[])
            match=next((m for m in matches if MARKER in m.get('excerpt','')),None)
            require(match is not None,'Archive literal search did not locate the expected owned marker')
            phase='recover';call={'id':f'owned_call_{index}','name':'recover_evidence','arguments':{'handle':match['handle'],'start':match['match_start'],'chars':len(MARKER)}}
        elif recovery:
            result=strict_json_loads(results[recovery[-1]['id']]);require(result.get('text')==MARKER,'Exact recovery failed')
        name=f'wire_{index:03d}.json';atomic_write(self.case/name,canonical(body))
        self.requests.append({'index':index,'path':name,'body_sha256':digest(body),'phase':phase,'tool_names':names,
                              'marker_in_wire':MARKER in canonical(rows),'wire_bytes':len(canonical(rows).encode())})
        atomic_write(self.case/'requests.json',canonical(self.requests))
        return httpx.Response(200,json=reply(self.protocol,index,call,answer))


async def case_run(out:Path,protocol:str,condition:str,pressure:str):
    case=out/f'{protocol}__{condition}__{pressure}';case.mkdir(mode=0o700)
    for folder in ('workspace','home','agent-config'):(case/folder).mkdir(mode=0o700)
    make_pages(case/'workspace',pressure)
    st=settings('low',65536,131072);atomic_write(case/'agent-config/settings.json',canonical(st))
    node,cli,nv,pv=pi_paths(ROOT)
    mode={'pi_original':'off','tools_observe':'observe','online_apply':'apply'}[condition]
    method='pi_original' if condition=='pi_original' else 'mask'
    provider=Provider(name='rc6-scripted-online',mock=True,model='mock-reasoner',base_url='http://127.0.0.1:1/v1',
        protocol=protocol,effort='low',max_output_tokens=65536,requests_per_minute=100000,tokens_per_minute=100000000,
        concurrency=1,retry={'total_seconds':60,'base_seconds':0,'cap_seconds':0})
    study=StudyConfig(name='rc6-owned-online-contract',methods=[method],online_projection={'mode':mode},
        compression={'recovery_search_mode':'match'},early_response_headers=True,response_heartbeat_seconds=.1,
        budget=Budget(max_total_tokens=5_000_000,per_cell_tokens=2_000_000))
    atomic_write(case/'study.json',canonical(study.model_dump()));atomic_write(case/'provider.json',canonical(provider.model_dump()))
    actor=Actor(protocol,case);app=create_app(provider,study,case/'gateway',transport=httpx.MockTransport(actor))
    sid='owned_online';token=app.state.sessions.create(sid,method);proc=None;stderr_task=None
    row={'condition':condition,'protocol':protocol,'pressure':pressure,'case_dir':case.name,'status':'running',
         'mode':mode,'settings':st,'node':nv,'pi':pv,'real_model_calls':0,'scripted_actor':True,
         'manual_compact_requested':False,'native_gate_eligible':False,'quality_measured':False,
         'native_compaction_commit':False,'prompt_sha':digest(PROMPT)}
    try:
        with server(app) as url:
            argv=command(node,cli,ROOT/'pi/src/index.ts',case/'session.jsonl',case/'unused.txt',provider.model,'low')
            argv[argv.index('json')]='rpc';argv=argv[:-2]
            env=isolated_env(case,url,token,12,14)
            for key in ('LD_PRELOAD','REVER_OFFLINE_GUARD_LOG'):
                if key in os.environ:env[key]=os.environ[key]
            proc=await asyncio.create_subprocess_exec(*argv,cwd=case/'workspace',env=env,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                start_new_session=True,limit=8*1024**2)
            with (case/'rpc.jsonl').open('xb') as log,(case/'stderr.log').open('xb') as errorlog:
                async def drain():
                    total=0
                    while chunk:=await proc.stderr.read(65536):
                        total+=len(chunk);require(total<4*1024**2,'stderr exceeded limit');errorlog.write(chunk);errorlog.flush()
                stderr_task=asyncio.create_task(drain());rpc=RPC(proc,log,deadline=90)
                await rpc.request('prompt',message=PROMPT)
                await stop_process_group(proc);await stderr_task
            row['request_count']=len(actor.requests);row['requests']=actor.requests
            require(len(actor.requests)==(8 if condition=='online_apply' and pressure=='long' else 6),'Unexpected scripted request count')
            require(MARKER in (case/'session.jsonl').read_text(),'Original Pi transcript lost the historical content')
            with app.state.sessions.db() as db:
                state=dict(db.execute('select * from sessions').fetchone())
                require(state['revision']==0 and db.execute('select count(*) from compact_ops').fetchone()[0]==0,'Projection was confused with native/custom compaction')
            events=[]
            if app.state.projection:
                from reverpi.util import unseal_cache
                with app.state.projection.db() as db:
                    events=[{'seq':r['seq'],'state':r['state'],'record':unseal_cache(r['record'])} for r in db.execute('select * from projection_events order by seq')]
                require(len(events)==len(actor.requests) and all(x['state']=='complete' for x in events),'Incomplete projection evidence')
            atomic_write(case/'projection_events.json',canonical(events))
            row['applied_requests']=sum(x['record']['applied_count']>0 for x in events)
            row['applied_observations']=sum(x['record']['applied_count'] for x in events)
            require((row['applied_requests']>0) is (condition=='online_apply' and pressure=='long'),'Wrong online activation control')
            row['status']='passed'
    except BaseException as error:
        row.update(status='failed',error_type=type(error).__name__)
        raise
    finally:
        # Failure to record cleanup must never replace the original trial error.
        try:app.state.sessions.disable(sid)
        except Exception as error:row['revocation_error']=type(error).__name__
        if proc is not None:await stop_process_group(proc)
        if stderr_task is not None:await asyncio.gather(stderr_task,return_exceptions=True)
        row['session_revoked']=bool(app.state.sessions.get(sid)['disabled'])
        if not row['session_revoked']:row['status']='failed'
        row['gateway_stopped']=True
        atomic_write(case/'report.json',canonical(row))
    return row


async def run(out:Path,protocols:list[str]):
    require(not out.exists() and not out.is_symlink(),'Use a new output directory')
    require(not out.resolve().is_relative_to(ROOT),'Artifacts must be outside implementation')
    out.mkdir(parents=True,mode=0o700)
    report={'schema':1,'kind':'owned_automatic_online_projection_v1','status':'running','rows':[],
        'protocols':protocols,'real_model_calls':0,'scripted_actor':True,'native_gate_eligible':False,
        'quality_measured':False,'token_savings_measured':False,'source_sha':digest(source_manifest(ROOT)),
        'online_request_boundary_tested':True,'native_threshold_compaction_tested':False}
    try:
        for protocol in protocols:
            for pressure in ('short','long'):
                for condition in CONDITIONS:
                    row=await case_run(out,protocol,condition,pressure);report['rows'].append(row)
                    require(row['status']=='passed','Case did not satisfy its contract')
                    atomic_write(out/'report.json',canonical(report))
    except BaseException as error:
        report.update(status='failed',error_type=type(error).__name__)
        atomic_write(out/'report.json',canonical(report));raise
    report['status']='passed';atomic_write(out/'report.json',canonical(report));return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--protocol',choices=['both',*PROTOCOLS],default='both');a=p.parse_args()
    protocols=list(PROTOCOLS) if a.protocol=='both' else [a.protocol]
    result=asyncio.run(run(a.out,protocols));print(canonical({'status':result['status'],'cells':len(result['rows']),'real_model_calls':0}));return 0
if __name__=='__main__':raise SystemExit(main())
