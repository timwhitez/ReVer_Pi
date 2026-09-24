"""Small deterministic checks for the RC6 auditor, installation and new fixture path."""
import copy
from dataclasses import asdict
import hashlib
from pathlib import Path
import sys
import pytest
from reverpi.config import OnlineProjectionConfig,CompressionConfig
from reverpi.online_projection import ProjectionStore
from reverpi.util import digest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from remaining_local_tasks import stage_cold_install
from prepare_rc6 import prepare
from online_projection_audit import check_record
from test_rc6_online import conversation
from reverpi.memory import Archive


def test_cold_stage_includes_exact_python_sibling(tmp_path):
    root=Path(__file__).resolve().parents[1]
    before=(root/'pi/package-lock.json').read_bytes();stage,sha=stage_cold_install(root,tmp_path)
    assert (stage/'package-lock.json').read_bytes()==before
    runner=stage.parent/'src/reverpi/revalidation.py'
    assert runner.read_bytes()==(root/'src/reverpi/revalidation.py').read_bytes()
    assert sha==hashlib.sha256(runner.read_bytes()).hexdigest()
    assert not (stage/'node_modules').exists()
    with pytest.raises(FileExistsError):stage_cold_install(root,tmp_path)


def test_fresh_owned_names_and_controls(tmp_path):
    out=tmp_path/'new';r=prepare(out)
    assert r['status']=='passed' and len(r['tasks'])==6 and not r['paid_authorized']
    import json,tomllib
    proposal=json.loads((out/'LIVE_PROPOSAL_NOT_AUTHORIZED.json').read_text())
    assert proposal['planned_paid_units']==0 and proposal['token_budget_authorized']==0
    for item in r['tasks']:
        t=tomllib.loads((out/item['task_toml']).read_text())
        assert t['task']['name'].startswith('rever-owned/') and t['environment']['network_mode']=='no-network'
    assert len(r['configs'])==3
    with pytest.raises(ValueError):prepare(out)


@pytest.mark.parametrize('suffix',['','-wal','-shm','-journal'])
def test_new_sqlite_family_rejects_symlinks(tmp_path,suffix):
    external=tmp_path/'external';external.write_text('do not touch')
    target=tmp_path/'x.sqlite';Path(str(target)+suffix).symlink_to(external)
    with pytest.raises(ValueError):ProjectionStore(target,OnlineProjectionConfig(mode='apply'))
    assert external.read_text()=='do not touch'


def test_projection_parent_symlink(tmp_path):
    real=tmp_path/'real';real.mkdir();(tmp_path/'alias').symlink_to(real,target_is_directory=True)
    with pytest.raises(ValueError):ProjectionStore(tmp_path/'alias/x.sqlite',OnlineProjectionConfig(mode='apply'))
    assert not (real/'x.sqlite').exists()


def test_late_sidecar_rejected(tmp_path):
    store=ProjectionStore(tmp_path/'x.sqlite',OnlineProjectionConfig(mode='apply'))
    external=tmp_path/'external';external.write_text('untouched');Path(str(store.path)+'-wal').symlink_to(external)
    with pytest.raises(ValueError):
        with store.db():pass
    assert external.read_text()=='untouched'


def audit_input(tmp_path):
    m,meta=conversation();archive=Archive(tmp_path/'archive',CompressionConfig())
    store=ProjectionStore(tmp_path/'p.sqlite',OnlineProjectionConfig(mode='apply'))
    for i in range(2):store.prepare('ns',f'o{i}',digest(i),m,meta,archive);store.complete('ns',f'o{i}')
    record=store.prepare('ns','o2',digest(2),m,meta,archive)
    original={x.call_id:{'text':x.content,'tool_name':d.tool_name,'is_error':d.is_error} for x,d in zip([x for x in m if x.role=='tool'],meta)}
    blobs={digest(x['text']):x['text'] for x in original.values()}
    full={k:2 for k in original}
    return record,full,set(),original,blobs


def test_reference_audit_accepts_valid_transition(tmp_path):
    args=audit_input(tmp_path);check_record(*args)


@pytest.mark.parametrize('change',['counter','reason','assistant','archive','receipt','coverage','hash'])
def test_reference_auditor_rejects_resealed_changes(tmp_path,change):
    r,full,packed,raw,blobs=audit_input(tmp_path)
    if change=='counter':r['observations'][0]['full_sends_before']=40
    elif change=='reason':r['observations'][0]['reason']='recent_result'
    elif change=='assistant':
        r['sent_messages'][2]['content']='changed assistant';r['sent_sha']=digest(r['sent_messages']);r['sent_utf8_bytes']=len(__import__('reverpi.util',fromlist=['canonical']).canonical(r['sent_messages']).encode())
    elif change=='archive':blobs.clear()
    elif change=='receipt':
        next(x for x in r['sent_messages'] if x['role']=='tool')['content']='fake'
        r['sent_sha']=digest(r['sent_messages']);r['sent_utf8_bytes']=len(__import__('reverpi.util',fromlist=['canonical']).canonical(r['sent_messages']).encode())
    elif change=='coverage':r['observations'].pop()
    elif change=='hash':r['source_sha']='0'*64
    with pytest.raises(ValueError):check_record(r,full,packed,raw,blobs)


@pytest.mark.parametrize('field,value',[('model','other-model'),('effort','high'),('concurrency',2)])
def test_live_online_profile_rejected_before_files(tmp_path,field,value):
    from reverpi.config import Provider,StudyConfig
    from reverpi.gateway import create_app
    from reverpi.errors import LabError
    fields={'name':'invalid-online','model':'deepseek-flash','effort':'low','concurrency':1,'mock':False,
            'base_url':'https://provider.invalid/v1'};fields[field]=value
    run=tmp_path/'unused'
    with pytest.raises(LabError) as e:create_app(Provider(**fields),StudyConfig(methods=['mask'],online_projection={'mode':'apply'}),run)
    assert e.value.kind=='online_profile' and not run.exists()


def test_luna_online_profile_allowed_only_at_low_single_concurrency(tmp_path):
    from reverpi.config import Provider,StudyConfig
    from reverpi.gateway import create_app
    provider=Provider(name='luna-pilot',model='gpt-6-luna',effort='low',concurrency=1,mock=False,
                      protocol='responses',base_url='https://provider.invalid/v1')
    app=create_app(provider,StudyConfig(methods=['mask'],online_projection={'mode':'apply'}),tmp_path/'run')
    assert app.state.projection is not None


@pytest.mark.parametrize('value',['false','true',0,1,None])
def test_error_status_not_coerced(value):
    from reverpi.online_projection import ObservationMeta
    with pytest.raises(ValueError):ObservationMeta(call_id='x',tool_name='read',content_sha='0'*64,is_error=value)


def summary_contract():
    from online_projection_tamper import CASES
    sha='0'*64
    old={'status':'offline_profile_passed','executable_profile_passed':True,'source_unchanged':True,
         'source_sha256':sha,'unexpected_nonloopback_calls':0,'steps':[{'status':'passed'}]}
    audit={'status':'passed','cells':12,'source_sha':sha,'real_model_quality_verified':False,'scripted_actor_only':True}
    negative={'status':'passed','source_sha':sha,'cases':[{'case':x,'rejected':True} for x in CASES]}
    prepared={'status':'passed','tasks':[{}]*6,'source_sha':sha,'paid_authorized':False}
    return old,audit,negative,prepared,sha


def test_exact_legacy_summary_status():
    from verify_rc6 import validate_summaries
    validate_summaries(*summary_contract())


@pytest.mark.parametrize('change',['legacy_status','legacy_flag','legacy_egress','source','quality','missing_negative','negative_not_rejected','paid','missing_task'])
def test_false_green_summary_rejected(change):
    from verify_rc6 import validate_summaries
    old,audit,negative,prepared,sha=summary_contract()
    if change=='legacy_status':old['status']='passed'
    elif change=='legacy_flag':old['executable_profile_passed']=False
    elif change=='legacy_egress':old['unexpected_nonloopback_calls']=1
    elif change=='source':audit['source_sha']='1'*64
    elif change=='quality':audit['real_model_quality_verified']=True
    elif change=='missing_negative':negative['cases'].pop()
    elif change=='negative_not_rejected':negative['cases'][0]['rejected']=False
    elif change=='paid':prepared['paid_authorized']=True
    elif change=='missing_task':prepared['tasks'].pop()
    with pytest.raises(ValueError):validate_summaries(old,audit,negative,prepared,sha)
