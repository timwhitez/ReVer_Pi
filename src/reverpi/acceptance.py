"""Real Pi RPC acceptance gate, separate from quality/benchmark scores.

Requires locally installed pinned Pi and a live gateway. The tiny owned canary
uses a deliberately small keepRecentTokens setting ONLY to exercise compaction;
this setting is never copied into a benchmark. No automatic rerun after failure.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import secrets
import signal
from pathlib import Path
from .config import Budget
from .errors import LabError
from .processes import stop_process_group
from .gateway import Sessions
from .launcher import pi_paths,command,settings,isolated_env
from .ledger import Ledger
from .util import atomic_write,canonical,digest,source_manifest,process_lock


class RPC:
    """One outstanding command, strict LF JSONL, bounded log, correlated response."""
    def __init__(self, proc, log, deadline=600, max_bytes=32*1024**2):
        self.proc,self.log,self.deadline,self.max_bytes=proc,log,deadline,max_bytes
        self.bytes=0;self.counter=0

    async def request(self, kind: str, **payload):
        self.counter+=1;key=f"rpc-{self.counter}"
        self.proc.stdin.write((canonical({"id":key,"type":kind,**payload})+"\n").encode())
        await self.proc.stdin.drain()
        events=[];response=None;ended=False
        async with asyncio.timeout(self.deadline):
            while response is None or (kind=="prompt" and not ended):
                try:line=await self.proc.stdout.readline()
                except (ValueError,asyncio.LimitOverrunError) as exc:
                    raise LabError('rpc_line_limit','Pi event exceeded the bounded JSONL line size') from exc
                if not line:raise LabError('rpc_eof','Pi stopped before the command and its events completed')
                self.bytes+=len(line)
                if self.bytes>self.max_bytes:raise LabError('rpc_log_limit','RPC acceptance log quota exceeded')
                self.log.write(line);self.log.flush()
                try:event=json.loads(line)
                except ValueError as exc:raise LabError('rpc_json','Unexpected non-JSON stdout from Pi') from exc
                if not isinstance(event,dict):raise LabError('rpc_schema','RPC event must be an object')
                if event.get('type')=='response' and event.get('id')==key:
                    if event.get('command')!=kind or event.get('success') is not True:
                        raise LabError('rpc_command','Pi rejected an acceptance command; inspect the private event log')
                    response=event
                elif event.get('type')=='response':
                    raise LabError('rpc_correlation','Unexpected response to a different command')
                if event.get('type')=='extension_error':raise LabError('pi_extension','Pi extension reported an error')
                if event.get('type')=='message_end' and event.get('message',{}).get('role')=='assistant':
                    if event['message'].get('stopReason') in {'error','aborted','length'}:
                        raise LabError('pi_actor','Pi actor did not deliver a complete usable response')
                if event.get('type')=='agent_end':ended=True
                events.append(event)
        return response.get('data',{}),events


def check_acceptance(report, code_sha, provider_sha, methods, *, require_recovery_methods=()):
    """Accuracy of the smoke answer is diagnostic, not a method-selection gate."""
    if not isinstance(report,dict) or report.get('mock') is not False or report.get('eligible_for_native_gate') is not True or report.get('source_sha')!=code_sha or report.get('provider_sha')!=provider_sha:
        raise LabError('native_acceptance_required','Run real Pi acceptance on this exact code and Provider first')
    rows=report.get('methods')
    if not isinstance(rows,list) or any(not isinstance(r,dict) or not isinstance(r.get('method'),str) or type(r.get('accepted')) is not bool for r in rows):
        raise LabError('native_acceptance_required','Malformed native acceptance method records')
    if len({r['method'] for r in rows})!=len(rows):
        raise LabError('native_acceptance_required','Conflicting duplicate native acceptance records')
    ok={r['method'] for r in rows if r['accepted'] is True}
    if set(methods)-ok:raise LabError('native_acceptance_required','Some planned methods have not passed native interface acceptance')
    recovered={r['method'] for r in rows if r.get('recovery_verified') is True
        and type(r.get('recovery_exact_results')) is int and r['recovery_exact_results'] > 0}
    if set(require_recovery_methods)-recovered:
        raise LabError('native_acceptance_required','Required methods lack actual exact recovery tool verification')



def post_compaction_assistant(events: list[dict]) -> dict:
    """Require a new complete assistant event from the post-compaction prompt."""
    assistants = [e.get('message') for e in events if e.get('type') == 'message_end'
                  and isinstance(e.get('message'), dict) and e['message'].get('role') == 'assistant']
    if not assistants or assistants[-1].get('stopReason') in {'error', 'aborted', 'length'}:
        raise LabError('acceptance_messages', 'No new usable assistant event after compaction')
    return assistants[-1]


async def native_acceptance(root:Path, gateway_run:Path, gateway_url:str, out:Path, methods:list[str], *, acknowledge_unsandboxed=False, private_http=False, allow_paid=False, deadline=600, require_recovery=False):
    if not acknowledge_unsandboxed:raise ValueError('Use an isolated disposable host/container and --acknowledge-unsandboxed; Pi is not a security sandbox')
    if os.name!='posix':raise ValueError('Native acceptance currently supports POSIX only')
    if type(require_recovery) is not bool:raise ValueError('require_recovery must be boolean')
    if not methods or len(set(methods))!=len(methods):raise ValueError('Methods must be unique and nonempty')
    if not 10<=deadline<=3600:raise ValueError('Acceptance per-command deadline must be 10..3600 seconds')
    node,cli,node_ver,pi_ver=pi_paths(root)
    import sqlite3
    with contextlib.closing(sqlite3.connect(gateway_run/'ledger.sqlite')) as db:
        meta={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM meta')}
    profiles=[v for k,v in meta.items() if k.startswith('provider:')]
    if len(profiles)!=1:raise ValueError('Expected one immutable gateway Provider')
    profile=profiles[0];code_sha=digest(source_manifest(root))
    if meta.get('gateway_code')!=code_sha:raise LabError('source_changed','Restart a new gateway generation after source changes')
    if set(methods)-set(meta['gateway_study']['methods']):raise ValueError('Method not in gateway allowlist')
    if not profile['mock'] and not allow_paid:raise ValueError('Native acceptance needs --allow-paid for a live Provider')
    ledger=Ledger(gateway_run/'ledger.sqlite',Budget.model_validate(meta['budget']));sessions=Sessions(gateway_run/'sessions.sqlite')
    if out.exists():raise ValueError('Acceptance directory already exists; do not silently replay native side effects')
    out=out.resolve();out.mkdir(parents=True)
    report={'source_sha':code_sha,'provider_sha':digest(profile),'gateway_config_sha':digest(meta['gateway_study']),
            'node':node_ver,'pi':pi_ver,'mock':profile['mock'],'methods':[],
            'scope':'Native Provider/tool/reasoning replay + explicit compaction + post-compaction continuation. Not a benchmark or Harbor certification.',
            'smoke_keep_recent_tokens':128,'eligible_for_native_gate':False,
            'require_recovery':require_recovery,
            'recovery_scope':'Exact archived span via a supplied handle; not autonomous retrieval or method quality.'}
    with process_lock(out/'acceptance.lock'):
        for method in methods:
            sid='accept_'+secrets.token_hex(16);folder=out/method;folder.mkdir()
            workspace=folder/'workspace';workspace.mkdir();(folder/'home').mkdir();(folder/'agent-config').mkdir()
            nonce=secrets.token_hex(12)
            atomic_write(workspace/'probe.txt','secret_nonce='+nonce+'\n'+''.join(f'unrelated_canary_{i}={digest([sid,i])}\n' for i in range(32)))
            st=settings(profile['effort'],profile['max_output_tokens'],profile['context_window'])
            st['compaction']['keepRecentTokens']=128
            atomic_write(folder/'agent-config/settings.json',canonical(st))
            token=sessions.create(sid,method,ttl=min(604800,deadline*6+60))
            row={'method':method,'cell':sid,'accepted':False,
                 'recovery_required':require_recovery and method not in {'pi_original','pi_native'},
                 'recovery_verified':False};proc=None;stderr_task=None
            try:
                # request /session is authenticated in the extension; no supplier key enters this process.
                argv=command(node,cli,root/'pi/src/index.ts',folder/'session.jsonl',folder/'unused.txt',profile['model'],profile['effort'])
                argv[argv.index('json')]='rpc';argv=argv[:-2] # remove --print @prompt
                proc=await asyncio.create_subprocess_exec(*argv,cwd=workspace,env=isolated_env(folder,gateway_url,token,12,12,private_http),
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True,limit=8*1024**2)
                with (folder/'rpc.jsonl').open('wb') as log,(folder/'stderr.log').open('wb') as errlog:
                    os.chmod(folder/'rpc.jsonl',0o600);os.chmod(folder/'stderr.log',0o600)
                    async def drain_stderr():
                        total=0
                        while data:=await proc.stderr.read(65536):
                            total+=len(data)
                            if total>4*1024**2:raise LabError('stderr_quota','Acceptance stderr is too large')
                            errlog.write(data);errlog.flush()
                    stderr_task=asyncio.create_task(drain_stderr())
                    rpc=RPC(proc,log,deadline)
                    _,first=await rpc.request('prompt',message='Use the read tool to read probe.txt. Remember its secret_nonce for the next turn; do not echo it now. Reply only READY after reading. Do not modify files or run commands.')
                    if not any(e.get('type')=='tool_execution_end' and e.get('toolName')=='read' for e in first):
                        raise LabError('acceptance_tool','Native actor did not exercise read-tool replay')
                    compact,_=await rpc.request('compact')
                    if not isinstance(compact,dict) or not isinstance(compact.get('summary'),str) or not compact['summary'].strip():
                        raise LabError('acceptance_compaction','No complete summary returned by real Pi compaction')
                    if method not in {'pi_original','pi_native'} and sessions.get(sid)['revision']<1:
                        raise LabError('acceptance_hook','Custom compaction hook did not commit through the gateway')
                    _,post_events=await rpc.request('prompt',message='Without reading files or running tools, return only the secret_nonce from before compaction; return UNKNOWN when unavailable.')
                    final_assistant=post_compaction_assistant(post_events)
                    text=''.join(b.get('text','') for b in final_assistant.get('content',[]) if b.get('type')=='text')
                    if row['recovery_required']:
                        from .canary_recovery import select_probe, verify_recovery
                        probe=select_probe(gateway_run/'archive.sqlite',sid,nonce,
                            meta['gateway_study']['compression']['recovery_chars'])
                        _,recovery_events=await rpc.request('prompt',message=probe.prompt())
                        row.update(verify_recovery(recovery_events,probe))
                    row.update(accepted=True,read_tool_replay=True,compaction_committed=True,post_compaction_response=True,
                        nonce_accuracy_diagnostic=nonce in text,accuracy_used_for_acceptance=False)
                    if stderr_task.done():stderr_task.result()
            except asyncio.CancelledError:
                row['accepted']=False
                row['error']={'kind':'cancelled'};raise
            except (LabError,TimeoutError,OSError,ValueError) as exc:
                row['accepted']=False
                row['error']=exc.record() if isinstance(exc,LabError) else {'kind':type(exc).__name__}
            finally:
                sessions.disable(sid)
                await stop_process_group(proc)
                if stderr_task:
                    if not stderr_task.done():stderr_task.cancel()
                    try:
                        await stderr_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        row['accepted']=False
                        row['error']=exc.record() if isinstance(exc,LabError) else {'kind':type(exc).__name__}
                row['cost']=ledger.totals(sid);report['methods'].append(row)
                atomic_write(out/'native_acceptance.json',canonical(report))
            if not row['accepted']:break # A failed contract does not justify a large live matrix.
        report['eligible_for_native_gate']=not profile['mock'] and len(report['methods'])==len(methods) and all(r['accepted'] for r in report['methods'])
        report['source_unchanged']=code_sha==digest(source_manifest(root))
        report['eligible_for_native_gate'] &= report['source_unchanged']
        atomic_write(out/'native_acceptance.json',canonical(report))
    return report
