import asyncio
import io
import json
import sys
import types
import importlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from reverpi.acceptance import RPC,check_acceptance
from reverpi.benchmark import MatrixConfig,TaskRef,tree_digest,read_tasks,audit_tasks,make_plan,validate_plan,harbor_command,read_official_result
from reverpi.config import Provider,StudyConfig,Budget
from reverpi.ledger import Ledger
from reverpi.devtasks import generate_tasks,validate_oracles
from reverpi.launcher import settings,isolated_env,command
from reverpi.util import canonical,digest,bytes_digest,source_manifest
from reverpi.errors import LabError

class Writer:
    def __init__(self):self.data=b''
    def write(self,b):self.data+=b
    async def drain(self):pass

def rpc_with(events):
    reader=asyncio.StreamReader();reader.feed_data(b''.join((canonical(e)+'\n').encode() for e in events));reader.feed_eof()
    return RPC(SimpleNamespace(stdin=Writer(),stdout=reader),io.BytesIO(),deadline=1)

@pytest.mark.asyncio
async def test_rpc_prompt_waits_for_agent_end_after_acceptance():
    r=rpc_with([{'type':'response','id':'rpc-1','command':'prompt','success':True}, {'type':'message_end','message':{'role':'assistant','stopReason':'stop'}},{'type':'agent_end'}])
    _,events=await r.request('prompt',message='x')
    assert events[-1]['type']=='agent_end' and len(events)==3

@pytest.mark.asyncio
async def test_rpc_unicode_separators_not_record_boundaries():
    r=rpc_with([{'type':'response','id':'rpc-1','command':'get_state','success':True,'data':{'text':'a\u2028b\u2029c'}}])
    data,_=await r.request('get_state');assert data['text']=='a\u2028b\u2029c'

@pytest.mark.asyncio
@pytest.mark.parametrize('event,kind',[
({'type':'response','id':'rpc-1','command':'prompt','success':False},'rpc_command'),
({'type':'response','id':'wrong','command':'prompt','success':True},'rpc_correlation'),
({'type':'extension_error'},'pi_extension'),
({'type':'message_end','message':{'role':'assistant','stopReason':'error'}},'pi_actor'),
])
async def test_rpc_fails_closed_on_wrong_or_failed_events(event,kind):
    with pytest.raises(LabError) as err:await rpc_with([event]).request('prompt',message='x')
    assert err.value.kind==kind

@pytest.mark.asyncio
async def test_rpc_acceptance_not_completed_on_early_eof():
    with pytest.raises(LabError,match='stopped'):await rpc_with([{'type':'response','id':'rpc-1','command':'prompt','success':True}]).request('prompt',message='x')

@pytest.mark.asyncio
async def test_rpc_log_quota():
    r=rpc_with([{'type':'note','x':'long text'}]);r.max_bytes=1
    with pytest.raises(LabError,match='quota'):await r.request('get_state')

@pytest.mark.parametrize('field,value',[('mock',True),('eligible_for_native_gate',False),('source_sha','changed'),('provider_sha','changed')])
def test_native_acceptance_rejects_mismatched_or_mock_evidence(field,value):
    r={'mock':False,'eligible_for_native_gate':True,'source_sha':'code','provider_sha':'p','methods':[{'method':'mask','accepted':True}]}
    with pytest.raises(LabError):check_acceptance({**r,field:value},'code','p',['mask'])

def test_native_acceptance_requires_all_methods_not_accuracy():
    r={'mock':False,'eligible_for_native_gate':True,'source_sha':'code','provider_sha':'p','methods':[{'method':'mask','accepted':True,'nonce_accuracy_diagnostic':False}]}
    check_acceptance(r,'code','p',['mask'])
    with pytest.raises(LabError):check_acceptance(r,'code','p',['rever_lite'])

def test_owned_tasks_fail_before_fix_pass_after_fix(tmp_path):
    out=tmp_path/'tasks';generate_tasks(out)
    assert validate_oracles(out)['all_valid']
    rows=read_tasks(out/'tasks.jsonl');assert len(rows)==6 and all(r.split=='search' and r.synthetic for r in rows)
    assert all(not (Path(r.path)/'environment/tests').exists() for r in rows)
    assert not audit_tasks(rows)['conflicts']

def test_native_task_overlap_ignores_shared_license_but_not_repository(tmp_path):
    generate_tasks(tmp_path/'tasks');r=read_tasks(tmp_path/'tasks/tasks.jsonl')
    a=r[0].model_copy(update={'split':'search','source_group':'a','provenance':{'repository':'https://github.com/x/a','license':'MIT'}})
    b=r[1].model_copy(update={'split':'dev','source_group':'b','provenance':{'repository':'https://github.com/x/b','license':'MIT'}})
    assert not audit_tasks([a,b])['conflicts']
    b.provenance={'repository':'https://github.com/x/a.git'};assert audit_tasks([a,b])['conflicts']

def test_task_hash_includes_hidden_verifier_and_rejects_symlinks(tmp_path):
    p=tmp_path/'task';p.mkdir();(p/'test').write_text('one');h=tree_digest(p);(p/'test').write_text('two');assert h!=tree_digest(p)
    (p/'link').symlink_to(p/'test')
    with pytest.raises(ValueError,match='symlink'):tree_digest(p)

@pytest.mark.parametrize('config',[{'methods':['full']},{'methods':['mask','mask']},{'methods':['madeup']},{'wall_seconds':100,'trial_deadline_seconds':80},{'allow_agent_hosts':['https://host']}])
def test_invalid_native_matrix_configuration(config):
    with pytest.raises(ValueError):MatrixConfig(**config)

def test_freeze_native_plan_detects_code_data_and_grid_edits(tmp_path,provider):
    root=tmp_path/'code';(root/'src').mkdir(parents=True);(root/'src/core.py').write_text('x=1')
    generate_tasks(tmp_path/'tasks');path=tmp_path/'plan.json';cfg=MatrixConfig(methods=['mask','rever_lite'])
    sc=StudyConfig(methods=cfg.methods)
    result=make_plan(root,tmp_path/'tasks/tasks.jsonl',cfg,provider,sc,path)
    assert result['cells']==12
    plan=validate_plan(root,path);changed=json.loads(path.read_text());changed['grid'][0]['method']='tail';path.write_text(canonical(changed))
    with pytest.raises(LabError,match='grid'):validate_plan(root,path)
    path.write_text(canonical(plan));(root/'src/core.py').write_text('x=2')
    with pytest.raises(LabError,match='source'):validate_plan(root,path)

def test_native_gate_refuses_mock_and_missing_certificates(tmp_path,provider):
    generate_tasks(tmp_path/'tasks');m=tmp_path/'tasks/tasks.jsonl';rows=[json.loads(l) for l in m.read_text().splitlines()]
    for r in rows:r['split']='gate'
    m.write_text('\n'.join(canonical(r) for r in rows))
    with pytest.raises(LabError,match='Formal'):make_plan(tmp_path,m,MatrixConfig(split='gate',methods=['mask']),provider,StudyConfig(methods=['mask']),tmp_path/'plan')

def test_harbor_command_one_attempt_zero_task_retry(tmp_path):
    row={'cell':'c1','path':str(tmp_path/'task'),'method':'mask'}
    plan={'provider':{'model':'reasoner'},'matrix':MatrixConfig().model_dump()}
    args=harbor_command('harbor',row,plan,tmp_path,'http://gateway:8765',tmp_path/'worker',tmp_path/'jobs')
    assert args[args.index('--max-retries')+1]=='0' and args[args.index('-k')+1]=='1'
    assert 'cell_id=c1' in args and 'reverpi.harbor_agent:ReVerPiAgent' in args

def trial_result(cell='cell',reward=1):
    return {'task_name':'task','trial_name':'trial','task_checksum':'sha','config':{'agent':{'kwargs':{'cell_id':cell}}},'verifier_result':{'rewards':{'reward':reward}},'exception_info':None}

def test_official_reward_not_guessed_or_replaced_by_exit_status(tmp_path):
    p=tmp_path/'trial';p.mkdir();f=p/'result.json';f.write_text(canonical(trial_result(reward=0)))
    out=read_official_result(tmp_path,{'cell':'cell'},'reward',1.0);assert out['success'] is False and out['execution_success'] is True
    f.write_text(canonical(trial_result(reward=None)))
    with pytest.raises(LabError):read_official_result(tmp_path,{'cell':'cell'},'reward',1.0)
    f.write_text(canonical(trial_result(cell='other')))
    with pytest.raises(LabError):read_official_result(tmp_path,{'cell':'cell'},'reward',1.0)

def test_multiple_official_results_are_ambiguous(tmp_path):
    for n in ['a','b']:(tmp_path/n).mkdir();(tmp_path/n/'result.json').write_text(canonical(trial_result()))
    with pytest.raises(LabError):read_official_result(tmp_path,{'cell':'cell'},'reward',1.0)

def test_native_settings_bounded_and_retry_disabled():
    s=settings('low',1024,8192);assert s['retry']['enabled'] is False and s['retry']['provider']['maxRetries']==0
    assert s['compaction']['reserveTokens']+s['compaction']['keepRecentTokens']<8192
    with pytest.raises(ValueError):settings('low',4096,4096)

def test_native_env_does_not_forward_provider_or_unrelated_secrets(tmp_path,monkeypatch):
    monkeypatch.setenv('REVER_API_KEY','secret');monkeypatch.setenv('AWS_SECRET_ACCESS_KEY','s')
    env=isolated_env(tmp_path,'http://127.0.0.1:8765','scoped',20,10)
    assert 'REVER_API_KEY' not in env and 'AWS_SECRET_ACCESS_KEY' not in env
    assert env['REVER_SESSION_TOKEN']=='scoped'

def test_ledger_stop_and_cell_attempt_cap(tmp_path):
    ledger=Ledger(tmp_path/'l',Budget(per_cell_attempts=1));ledger.claim('a','sha','cell');aid=ledger.reserve('a','cell','p',10,0);ledger.settle(aid,tokens=0,usd=0)
    ledger.claim('b','s','cell')
    with pytest.raises(LabError,match='budget'):ledger.reserve('b','cell','p',10,0)
    ledger.stop('provider_reconfigure');assert ledger.stop_reason()=='provider_reconfigure'
    ledger.claim('c','s','another')
    with pytest.raises(LabError,match='stopped'):ledger.reserve('c','another','p',10,0)
