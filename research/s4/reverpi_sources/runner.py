"""Actual Pi -> frozen gateway -> one central ledger on read-only source snapshots.

No credentials accepted for Mock. Paid use requires an independent operator
file matching an explicit frozen request and token envelope. No automatic retry
or resume of an unfinished run. This is not a security sandbox or coding agent
product benchmark. Gold is only read by a separate scoring command.
"""
from __future__ import annotations
import asyncio,contextlib,hashlib,os,sys
from pathlib import Path
import httpx
from reverpi.acceptance import RPC
from reverpi.config import Budget,Provider,StudyConfig
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.launcher import pi_paths,command,isolated_env,settings
from reverpi.ledger import Ledger
from reverpi.paired_prefix import paired_tools,require
from reverpi.preflight import validate_provider_environment
from reverpi.processes import stop_process_group
from reverpi.util import atomic_write,canonical,digest,source_manifest,strict_json_loads,atomic_create
from native_revalidation_smoke import server
from native_compaction_smoke import wire_view,reply
from .contracts import ROOT,S4,SourcePlan,tree_manifest,research_manifest,load_task,render_prompt,verify_identity,new_output,read_file
from .backend import SourceBackend

@contextlib.contextmanager
def paired_server(app):
    with server(app) as url:
        try:yield url
        finally:app.state.sessions.disable('paired')

class SourceActor:
    """Scripted mechanics only. Never reads controller gold; final values are null."""
    def __init__(self,provider,files,task,profile):
        self.p=provider;self.keys=task['answer_keys'];self.profile=profile
        candidates=[n for n in files if n.endswith('.py')]
        self.names=(sorted(candidates,key=lambda n:(len(files[n]),n))[:1] if profile=='quick'
                    else sorted(candidates,key=lambda n:(-len(files[n]),n))[:5])
    def __call__(self,request):
        require(request.url.host=='127.0.0.1','Mock request is not loopback')
        body=strict_json_loads(request.content);require(body['model']=='mock-reasoner','Mock model identity')
        rows,names=wire_view(body,self.p.protocol)
        require(set(names)=={'read','search_evidence','recover_evidence'},'Unexpected actor tool surface')
        if self.p.protocol=='chat_completions':
            calls=[dict(id=c['id'],name=c['function']['name'],arguments=strict_json_loads(c['function']['arguments'])) for m in rows for c in m.get('tool_calls',[])]
            results={m['tool_call_id']:m['content'] for m in rows if m.get('role')=='tool'}
        else:
            calls=[dict(id=m['call_id'],name=m['name'],arguments=strict_json_loads(m['arguments'])) for m in rows if m.get('type')=='function_call']
            results={m['call_id']:m['output'] for m in rows if m.get('type')=='function_call_output'}
        reads=[c for c in calls if c['name']=='read'];retrieval=[c for c in calls if c['name']!='read'];call=None
        if len(reads)<len(self.names):
            i=len(reads);call={'id':f's4_read_{i}','name':'read','arguments':{'path':self.names[i]}}
        elif self.profile=='walk' and not retrieval:
            call={'id':'s4_search','name':'search_evidence','arguments':{'query':'class ','chars':800}}
        elif self.profile=='walk' and retrieval[-1]['name']=='search_evidence':
            v=strict_json_loads(results[retrieval[-1]['id']]);matches=v.get('matches',[])
            if matches:call={'id':'s4_exact','name':'recover_evidence','arguments':{'handle':matches[0]['handle'],'start':matches[0].get('start',0),'chars':100}}
        final=canonical({k:None for k in self.keys})
        return httpx.Response(200,json=reply(self.p.protocol,len(calls),call,final if call is None else ''))

async def phase_run(out: Path, phase: str, plan: SourcePlan, files: dict, prompt: str, task: dict, identities: dict):
    folder = out/phase; folder.mkdir(mode=0o700)
    for name in ('home', 'agent-config'):
        (folder/name).mkdir(mode=0o700)
    st = settings(plan.provider.effort, plan.provider.max_output_tokens, plan.provider.context_window)
    atomic_write(folder/'agent-config/settings.json', canonical(st))
    actor = SourceActor(plan.provider, files, task, plan.scripted_profile) if plan.provider.mock else None
    backend = SourceBackend(root=ROOT, run=out, phase=phase, plan=plan, workspace=out/'workspace',
                            expected_workspace=identities, actor_transport=httpx.MockTransport(actor) if actor else None)
    mode = 'apply' if phase == 'projected' else 'observe'
    cfg = StudyConfig(name='rc7-readonly-paired', methods=['mask'], budget=plan.budget,
                      online_projection=plan.policy.model_copy(update={'mode': mode}),
                      compression={'recovery_search_mode': 'match'}, early_response_headers=True, response_heartbeat_seconds=.1)
    app = create_app(plan.provider, cfg, folder/'gateway', completion_backend=backend)
    token = app.state.sessions.create('paired', 'mask')
    atomic_write(folder/'study.json', canonical(cfg.model_dump()))
    node, cli, nodever, piver = pi_paths(ROOT)
    row = {'phase': phase, 'state': 'running', 'node': nodever, 'pi': piver, 'model': plan.provider.model,
           'mock': plan.provider.mock, 'manual_compact_rpc': False, 'tools': paired_tools(plan)}
    atomic_write(folder/'phase.json', canonical(row))
    proc = None; drain_task = None
    try:
        with paired_server(app) as url:
            argv = command(node, cli, ROOT/'pi/src/index.ts', folder/'session.jsonl', folder/'unused', plan.provider.model, 'low')
            argv[argv.index('json')] = 'rpc'; argv = argv[:-2]
            argv += ['--tools', ','.join(paired_tools(plan)), '--extension', str(S4/'pi/readonly.ts')]
            env = isolated_env(folder, url, token, 30, plan.max_prefix_requests+plan.max_suffix_requests+3)
            env.update(REVER_PAIRED_WORKSPACE=str(out/'workspace'), REVER_PAIRED_FILES=canonical(list(files)), PI_TELEMETRY='0')
            for k in ('LD_PRELOAD', 'REVER_OFFLINE_GUARD_LOG'):
                if k in os.environ: env[k] = os.environ[k]
            proc = await asyncio.create_subprocess_exec(*argv, cwd=out/'workspace', env=env,
                       stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                       start_new_session=True, limit=8*1024**2)
            with (folder/'rpc.jsonl').open('xb') as log, (folder/'stderr.log').open('xb') as errors:
                async def drain():
                    count = 0
                    while chunk := await proc.stderr.read(65536):
                        count += len(chunk); require(count <= 4*1024**2, 'Pi stderr limit')
                        errors.write(chunk); errors.flush()
                drain_task = asyncio.create_task(drain())
                rpc = RPC(proc, log, deadline=plan.wall_seconds_per_phase)
                await rpc.request('prompt', message=prompt)
                await stop_process_group(proc)
                await drain_task
            if phase == 'capture' and backend.stopped_kind == 'paired_boundary':
                row['state'] = 'boundary_captured'
            elif backend.stopped_kind:
                row.update(state='stopped', reason=backend.stopped_kind)
            else:
                assistants = []
                for line in (folder/'session.jsonl').read_text().splitlines():
                    entry = strict_json_loads(line)
                    m = entry.get('message', {})
                    if m.get('role') == 'assistant': assistants.append(m)
                require(assistants and assistants[-1].get('stopReason') not in {'error', 'aborted', 'length', 'toolUse'},
                        'No complete final assistant answer', 'agent_not_complete')
                answer = ''.join(x.get('text', '') for x in assistants[-1].get('content', []) if x.get('type') == 'text')
                row.update(state='completed', answer=answer, answer_scored=False)
    except (Exception, asyncio.CancelledError) as err:
        if (phase == 'capture' and backend.stopped_kind == 'paired_boundary'
                and isinstance(err, LabError) and err.kind == 'pi_actor' and (out/'boundary.json').is_file()):
            row.update(state='boundary_captured', reason='intentional_preclaim_pause')
        else:
            row.update(state='stopped', reason=backend.stopped_kind or (err.kind if isinstance(err, LabError) else type(err).__name__))
        # Never hide cancellation/unknown outcome with an automatic new phase.
        if isinstance(err, asyncio.CancelledError): raise
    finally:
        app.state.sessions.disable('paired')
        await stop_process_group(proc)
        await backend.aclose()
        if drain_task is not None: await asyncio.gather(drain_task, return_exceptions=True)
        row.update(session_revoked=bool(app.state.sessions.get('paired')['disabled']),
                   experiment_dispatches=backend.ledger.totals(phase)['attempts'], completed_responses=backend.paid_count,
                   prefix_replays=backend.replayed_count,
                   central_cost=backend.ledger.totals(phase), gateway_cost=app.state.ledger.totals())
        row['fixture_unchanged'] = tree_manifest(out/'workspace') == identities
        if not row['fixture_unchanged']: row.update(state='stopped', reason='workspace_changed')
        atomic_write(folder/'phase.json', canonical(row))
    return row


def authorization_intent(plan):
    value=plan.model_dump();value.pop('paid_authorized');value.pop('authorization_digest')
    return digest(value)

def check_authorization(plan,path):
    require(path is not None,'Independent approval file required')
    raw=path.read_bytes();require(hashlib.sha256(raw).hexdigest()==plan.authorization_digest,'Approval file digest mismatch')
    a=strict_json_loads(raw)
    require(type(a) is dict and set(a)=={'schema','approved','intent_sha256','max_requests','max_accounted_tokens','operator_statement'},'Invalid approval schema')
    require(a['schema']=='reverpi.s4.authorization.v1' and a['approved'] is True,'Paid work not approved')
    require(a['intent_sha256']==authorization_intent(plan),'Approval is for another plan')
    require(type(a['max_requests']) is int and a['max_requests']==plan.budget.max_attempts and type(a['max_accounted_tokens']) is int and a['max_accounted_tokens']==plan.budget.max_total_tokens,'Approval budget mismatch')
    require(type(a['operator_statement']) is str and len(a['operator_statement'])>=10,'Missing operator statement')
    return a

def preflight(plan,task,*,allow_paid=False,acknowledge_local=False,authorization=None):
    verify_identity(plan,task)
    from .contracts import read_gold
    read_gold(S4/'data/controller'/f"{task['task_id']}.gold.json",task,plan.gold_sha256)
    if not plan.provider.mock:
        require(allow_paid and plan.paid_authorized and acknowledge_local,'Real provider requires new explicit approval and readonly-environment acknowledgement')
        require(not os.environ.get('REVER_OFFLINE_VERIFICATION'),'No live requests during offline validation')
        check_authorization(plan,authorization)
        validate_provider_environment(plan.provider)
        # Necessary worst-case output-only pressure screen, never a cost guarantee.
        reserve=plan.provider.max_output_tokens
        require(plan.budget.max_total_tokens >= (plan.max_prefix_requests+2*plan.max_suffix_requests)*reserve,'Output-only whole-plan pressure exceeds declared budget')
        require(plan.budget.per_cell_tokens >= max(plan.max_prefix_requests,plan.max_suffix_requests)*reserve,'Output-only stage pressure exceeds declared budget')
        require(plan.budget.max_attempts>=plan.max_prefix_requests+2*plan.max_suffix_requests and plan.budget.per_cell_attempts>=max(plan.max_prefix_requests,plan.max_suffix_requests),'Request budget cannot cover declared stages')
    pi_paths(ROOT)

async def run(plan_path,plan_sha,out,*,allow_paid=False,acknowledge_local=False,authorization=None):
    value=strict_json_loads(plan_path.read_bytes());require(digest(value)==plan_sha,'Independent plan hash mismatch')
    plan=SourcePlan.model_validate(value);task=load_task(S4/'data/tasks'/f'{plan.task_id}.json')
    preflight(plan,task,allow_paid=allow_paid,acknowledge_local=acknowledge_local,authorization=authorization)
    original=S4/'data/sources'/task['source_id']
    require(tree_manifest(original)==task['files'],'Installed source snapshot differs from task')
    out=new_output(out);out.mkdir(parents=True,mode=0o700)
    atomic_write(out/'PLAN.json',canonical(value));atomic_write(out/'TASK.json',canonical(task))
    atomic_write(out/'EXECUTOR.json',canonical({'core':source_manifest(ROOT),'research':research_manifest()}))
    (out/'workspace').mkdir(mode=0o700)
    files={}
    for name in task['files']:
        data=read_file(original,name);atomic_write(out/'workspace'/name,data,mode=0o444)
        files[name]=data.decode('utf-8')
    identities=tree_manifest(out/'workspace');prompt=render_prompt(task)
    atomic_write(out/'PUBLIC.json',canonical({'task_sha256':task['task_sha256'],'snapshot_sha256':task['snapshot_sha256'],'prompt':prompt,'gold_in_actor_workspace':False}))
    summary={'schema':1,'release':'Research-S4','status':'running','plan_sha':plan_sha,'source_sha256':plan.source_sha256,'research_sha256':plan.research_sha256,
             'provider_mock':plan.provider.mock,'real_model_calls':0 if plan.provider.mock else None,'paired_effect_identified':False,
             'quality_or_superiority_established':False,'scripted_actor_only':plan.provider.mock,'read_only_replay_not_os_snapshot':True,
             'natural_coverage_population':'inspected_development_source_not_population_sample','phases':[]}
    atomic_write(out/'summary.json',canonical(summary))
    try:
        capture=await phase_run(out,'capture',plan,files,prompt,task,identities);summary['phases'].append(capture)
        atomic_write(out/'summary.json',canonical(summary))
        if capture['state']!='boundary_captured':summary['status']='no_eligible_prefix' if capture['state']=='completed' else 'stopped'
        else:
            for arm in plan.branch_order:
                row=await phase_run(out,arm,plan,files,prompt,task,identities);summary['phases'].append(row)
                atomic_write(out/'summary.json',canonical(summary))
                if row['state']!='completed':summary['status']='stopped';break
            else:summary['status']='paired_complete'
    except (Exception,asyncio.CancelledError) as exc:
        summary.update(status='stopped',error_type=type(exc).__name__)
        if isinstance(exc,asyncio.CancelledError):raise
    finally:
        summary['central_cost']=Ledger(out/'accounting.sqlite',plan.budget).totals()
        summary['source_unchanged']=digest(source_manifest(ROOT))==plan.source_sha256 and digest(research_manifest())==plan.research_sha256
        summary['fixture_unchanged']=tree_manifest(out/'workspace')==identities
        if not summary['source_unchanged'] or not summary['fixture_unchanged']:summary['status']='stopped'
        summary['common_prefix_cost']=next((r['central_cost'] for r in summary['phases'] if r['phase']=='capture'),{})
        summary['accounting_note']='Experiment counts prefix once; each logical arm must include common prefix. Mock usage is synthetic; partial cost is not completion cost.'
        atomic_write(out/'summary.json',canonical(summary))
    return summary

def prepare(out,task_id,protocol,profile='walk',provider_path=None,budget_path=None,seed='s4-development-v1',prefix_cap=12,suffix_cap=12):
    # Every path is constrained before joining the task registry.
    from .contracts import relative,read_gold
    relative(task_id);require('/' not in task_id,'Task id cannot contain a slash')
    task=load_task(S4/'data/tasks'/f'{task_id}.json')
    golden=S4/'data/controller'/f'{task_id}.gold.json';gold_sha=hashlib.sha256(golden.read_bytes()).hexdigest()
    read_gold(golden,task,gold_sha) # preflight only, never passed to actor
    if provider_path is None:
        provider=Provider(name='s4-scripted',protocol=protocol,mock=True,model='mock-reasoner',base_url='http://127.0.0.1:1/v1',effort='low',concurrency=1,max_output_tokens=65536,
                          requests_per_minute=100000,tokens_per_minute=100000000,retry={'max_attempts':1,'total_seconds':60,'base_seconds':0,'cap_seconds':0})
        budget=Budget(max_total_tokens=8_000_000,per_cell_tokens=3_000_000,max_attempts=prefix_cap+2*suffix_cap,per_cell_attempts=max(prefix_cap,suffix_cap))
        require(profile in {'quick','walk'},'Choose an explicit scripted profile')
    else:
        require(budget_path is not None,'No inherited budget; an explicit operator budget is required')
        provider=Provider.model_validate(strict_json_loads(provider_path.read_bytes()));require(not provider.mock,'Use ordinary Mock prepare for mock provider')
        require(protocol==provider.protocol,'Protocol must equal explicit provider')
        budget=Budget.model_validate(strict_json_loads(budget_path.read_bytes()));profile='none'
    siblings=sorted(load_task(p)['task_id'] for p in (S4/'data/tasks').glob('*.json') if load_task(p)['source_group']==task['source_group'])
    parity=int(hashlib.sha256((seed+'\0'+task['source_group']).encode()).hexdigest(),16)%2
    order=['full','projected'] if (parity+siblings.index(task_id))%2==0 else ['projected','full']
    plan=SourcePlan(source_sha256=digest(source_manifest(ROOT)),research_sha256=digest(research_manifest()),
                    provider=provider,budget=budget,policy={'mode':'observe','recovery_interface':'split_v1'},
                    seed=seed,branch_order=order,max_prefix_requests=prefix_cap,max_suffix_requests=suffix_cap,wall_seconds_per_phase=240 if provider.mock else 3600,
                    task_sha256=task['task_sha256'],snapshot_sha256=task['snapshot_sha256'],gold_sha256=gold_sha,task_id=task_id,source_group=task['source_group'],scripted_profile=profile)
    out=new_output(out);out.mkdir(parents=True,mode=0o700)
    atomic_write(out/'PLAN.json',canonical(plan.model_dump()));atomic_write(out/'PLAN.sha256',digest(plan.model_dump())+'\n')
    atomic_write(out/'NOT_AUTHORIZED.json',canonical({'paid_authorized':False,'intent_sha256':authorization_intent(plan),'required_new_approval':not provider.mock,'proposed_max_requests':prefix_cap+2*suffix_cap,'proposed_accounted_tokens':budget.max_total_tokens,'upstream_physical_limit_verified':False,'output_only_screen_is_sufficient':False}))
    return plan
