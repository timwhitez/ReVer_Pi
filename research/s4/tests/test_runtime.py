from pathlib import Path
import hashlib,json,copy
import pytest
from reverpi.errors import LabError
from reverpi.util import digest,canonical
from reverpi_sources.contracts import S4,SourcePlan,load_task
from reverpi_sources.runner import prepare,preflight,authorization_intent,check_authorization

@pytest.mark.parametrize('protocol',['responses','chat_completions'])
@pytest.mark.parametrize('profile',['quick','walk'])
def test_prepare_offline(tmp_path,protocol,profile):
    p=prepare(tmp_path/'new','cache-keys',protocol,profile)
    assert not p.paid_authorized and p.provider.mock and p.scripted_profile==profile
    assert p.source_sha256=='d8ed50cc4bf88117293f3ce2cb41a79787b4343be351db29e8a938abc108d811'

@pytest.mark.parametrize('bad',['../controller/x','/tmp/x','cache-keys/../../x'])
def test_task_id_escape(tmp_path,bad):
    with pytest.raises(LabError):prepare(tmp_path/'new',bad,'responses')

def test_live_not_authorized(tmp_path):
    p=prepare(tmp_path/'new','cache-keys','responses')
    v=p.model_dump();v['provider'].update(mock=False,model='deepseek-flash');v['scripted_profile']='none'
    live=SourcePlan.model_validate(v);task=load_task(S4/'data/tasks/cache-keys.json')
    with pytest.raises(LabError,match='new explicit approval'):preflight(live,task)

def test_approval_binds_intent(tmp_path):
    p=prepare(tmp_path/'new','cache-keys','responses')
    v=p.model_dump();v['provider'].update(mock=False,model='deepseek-flash');v['scripted_profile']='none'
    live=SourcePlan.model_validate(v)
    body={'schema':'reverpi.s4.authorization.v1','approved':True,'intent_sha256':authorization_intent(live),'max_requests':live.budget.max_attempts,'max_accounted_tokens':live.budget.max_total_tokens,'operator_statement':'SYNTHETIC TEST APPROVAL - NEVER LIVE'}
    a=tmp_path/'test-approval.json';a.write_text(canonical(body));v.update(paid_authorized=True,authorization_digest=hashlib.sha256(a.read_bytes()).hexdigest());bound=SourcePlan.model_validate(v)
    assert check_authorization(bound,a)==body
    w=bound.model_dump();w['max_suffix_requests']+=1
    with pytest.raises(LabError,match='another plan'):check_authorization(SourcePlan.model_validate(w),a)

def test_research_drift_before_out(tmp_path):
    p=prepare(tmp_path/'new','cache-keys','responses');v=p.model_dump();v['research_sha256']='0'*64
    with pytest.raises(LabError,match='executor drift'):preflight(SourcePlan.model_validate(v),load_task(S4/'data/tasks/cache-keys.json'))

def test_order_balanced_within_source(tmp_path):
    a=prepare(tmp_path/'a','cache-keys','responses');b=prepare(tmp_path/'b','cache-behavior','responses')
    assert a.branch_order==list(reversed(b.branch_order))

def test_gold_drift_stops_before_any_request(tmp_path):
    p=prepare(tmp_path/'new','cache-keys','responses');v=p.model_dump();v['gold_sha256']='0'*64
    with pytest.raises(LabError,match='Gold file digest'):preflight(SourcePlan.model_validate(v),load_task(S4/'data/tasks/cache-keys.json'))
