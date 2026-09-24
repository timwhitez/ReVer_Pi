"""RC7.1 offline regressions. These do not certify model task performance."""
from pathlib import Path
from dataclasses import asdict
import json
import sys
import pytest
import httpx
from pydantic import ValidationError
from reverpi.config import OnlineProjectionConfig,CompressionConfig,StudyConfig,Provider,Budget
from reverpi.online_projection import ProjectionStore,placeholder
from reverpi.paired_prefix import PairedPlan,paired_tools
from reverpi.paired_metrics import counts,budgeted_outcomes,price_delta
from reverpi.memory import Archive
from reverpi.errors import LabError
from reverpi.util import digest
from test_rc6_online import conversation,age,prepare

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))


def test_interface_default_and_opt_in():
    assert OnlineProjectionConfig().recovery_interface=='legacy'
    with pytest.raises(ValidationError):OnlineProjectionConfig(recovery_interface='anything')


def test_legacy_placeholder_is_byte_compatible():
    text='hello界'*3000
    assert placeholder(text,digest(text),'read',1024)==placeholder(text,digest(text),'read',1024,'legacy')
    assert 'search_evidence' not in placeholder(text,digest(text),'read',1024)


def test_split_placeholder_clear_navigation_not_gold():
    text='first\n'+'z'*12000+'\nlast'
    got=placeholder(text,digest(text),'read',1024,'split_v1')
    assert 'search_evidence(query, chars)' in got and 'Never send query to recover_evidence' in got
    assert len(got.encode())<len(text.encode())
    with pytest.raises(ValueError):placeholder(text,digest(text),'read',1024,'bad')


@pytest.mark.parametrize('tool',['search_evidence','recover_evidence','revalidate_evidence'])
def test_split_all_recovery_results_protected(tmp_path,tool):
    cfg=OnlineProjectionConfig(mode='apply',recovery_interface='split_v1')
    s=ProjectionStore(tmp_path/'p.sqlite',cfg);a=Archive(tmp_path/'a.sqlite',CompressionConfig())
    m,meta=conversation(tool=tool);age(s,a,m,meta);r=prepare(s,a,m,meta,op='third')
    assert r['applied_count']==0 and r['observations'][0]['reason']=='recovery_or_current_verification'


def test_split_uses_same_eligibility_rules(tmp_path):
    cfg=OnlineProjectionConfig(mode='apply',recovery_interface='split_v1')
    s=ProjectionStore(tmp_path/'p.sqlite',cfg);a=Archive(tmp_path/'a.sqlite',CompressionConfig())
    m,meta=conversation();age(s,a,m,meta);r=prepare(s,a,m,meta,op='third')
    assert r['applied_count']==1 and r['observations'][0]['full_sends_before']==2
    assert 'search_evidence' in r['sent_messages'][3]['content']
    assert m[3].content==r['source_messages'][3]['content']


def test_cannot_mutate_interface_inside_database(tmp_path):
    cfg=OnlineProjectionConfig(mode='observe')
    ProjectionStore(tmp_path/'p.sqlite',cfg)
    with pytest.raises(LabError,match='projection_config_changed'):
        ProjectionStore(tmp_path/'p.sqlite',cfg.model_copy(update={'recovery_interface':'split_v1'}))


def test_legacy_runtime_rejects_mixed_arguments_without_spending_recovery_quota(tmp_path):
    a=Archive(tmp_path/'a.sqlite',CompressionConfig());h=a.put('cell','evidence')
    with pytest.raises(LabError,match='recovery_arguments'):a.recover('cell','bad',handle=h,query='evidence')
    with a.connect() as db:assert db.execute('select count(*) from recovery').fetchone()[0]==0
    assert a.recover('cell','ok',handle=h)['text']=='evidence'


@pytest.mark.parametrize('mode',['legacy','split_v1'])
@pytest.mark.asyncio
async def test_gateway_advertises_frozen_interface(tmp_path,provider,mode):
    from reverpi.gateway import create_app
    app=create_app(provider,StudyConfig(methods=['mask'],online_projection={'mode':'observe','recovery_interface':mode}),tmp_path,transport=httpx.MockTransport(lambda r:None))
    token=app.state.sessions.create('one','mask')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost') as c:
        response=await c.get('/session',headers={'Authorization':'Bearer '+token})
    assert response.status_code==200
    assert response.json()['recovery'].get('interface','legacy')==mode


def test_tool_set_frozen_for_entire_pair(provider):
    p=provider.model_copy(update={'effort':'low','concurrency':1,'max_output_tokens':65536})
    p.retry=p.retry.model_copy(update={'max_attempts':1,'retry_ambiguous':False})
    base=dict(source_sha256='0'*64,provider=p,budget=Budget(),seed='abcdefgh')
    a=PairedPlan(**base);b=PairedPlan(**base,policy={'mode':'observe','recovery_interface':'split_v1'})
    assert paired_tools(a)==['read','recover_evidence']
    assert paired_tools(b)==['read','recover_evidence','search_evidence']
    assert digest(a.model_dump())!=digest(b.model_dump())


@pytest.mark.parametrize('value',[-1,True,1.5,None,'3'])
def test_invalid_token_counts(value):
    with pytest.raises(ValueError):counts([{'input_tokens':value,'cached_input_tokens':0,'output_tokens':1}])


def test_count_cache_is_not_double_counted():
    d=counts([{'input_tokens':100,'cached_input_tokens':90,'output_tokens':10}])
    assert d['total_tokens']==110 and d['uncached_input_tokens']==10
    with pytest.raises(ValueError):counts([{'input_tokens':1,'cached_input_tokens':2,'output_tokens':0}])


def test_frozen_budget_outcome_is_observable_without_inventing_answer():
    out=budgeted_outcomes([{'phase':'full','state':'completed','completed_requests':6,'marker_exact':True},
        {'phase':'projected','state':'stopped','reason':'suffix_cap','completed_requests':6,'marker_exact':None}],6)
    assert [x['completion_by_frozen_cap'] for x in out['arms']]==[1,0]
    assert out['arms'][1]['observed_final_answer_exact'] is None
    assert out['arms'][1]['unrestricted_completion'] is None
    assert out['capped_cost_is_completed_task_cost'] is False


def test_infrastructure_stop_is_not_silently_scored_zero():
    out=budgeted_outcomes([{'phase':'projected','state':'stopped','reason':'transport_ambiguous','completed_requests':3}],6)
    assert out['arms'][0]['completion_by_frozen_cap'] is None


@pytest.mark.parametrize('row',[
 {'phase':'full','state':'completed','completed_requests':6,'marker_exact':None},
 {'phase':'full','state':'completed','completed_requests':7,'marker_exact':True},
 {'phase':'projected','state':'stopped','completed_requests':6,'reason':'suffix_cap','marker_exact':False},
 {'phase':'../bad','state':'stopped','completed_requests':6},
])
def test_bad_budgeted_outcome_contract(row):
    with pytest.raises(ValueError):budgeted_outcomes([row],6)


def test_duplicate_arm_rejected():
    p={'phase':'full','state':'completed','completed_requests':1,'marker_exact':True}
    with pytest.raises(ValueError):budgeted_outcomes([p,p],6)


def test_price_delta_remains_symbolic():
    f=counts([dict(input_tokens=15360,cached_input_tokens=14848,output_tokens=33)])
    p=counts([dict(input_tokens=8789,cached_input_tokens=0,output_tokens=33)])
    d=price_delta(f,p)
    assert (d['uncached_input'],d['cached_input'],d['output'])==(8277,-14848,0)
    assert d['usd'] is None and d['completed_task_saving'] is None


def test_actual_rejected_call_was_legal_under_exposed_schema():
    import jsonschema
    schema={'type':'object','properties':{'handle':{'type':'string'},'query':{'type':'string'},'chars':{'type':'integer','minimum':1,'maximum':6000}},'additionalProperties':False}
    args={'handle':'a'*64,'query':'historical_marker','chars':1000}
    jsonschema.validate(args,schema)
    split={'type':'object','properties':{'handle':{'type':'string'},'chars':{'type':'integer','minimum':1,'maximum':6000}},'required':['handle'],'additionalProperties':False}
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(args,split)


def test_search_audit_accepts_real_archive_receipt(tmp_path):
    from reverpi.memory import Archive
    from reverpi.config import CompressionConfig
    from reverpi.util import digest
    from verify_paired_prefix import check_search_receipt
    content='prefix '*50+'NEEDLE'+ ' tail '*50
    archive=Archive(tmp_path/'a.sqlite',CompressionConfig(recovery_search_mode='match'))
    h=archive.put('n',content)
    args={'query':'needle','chars':1000}
    v=archive.recover('n','q',**args)
    assert check_search_receipt(args,v,{h:content},'match')==1


@pytest.mark.parametrize('field,value',[
    ('excerpt','forged'),('start',0),('match_start',0),('total_chars',1),('end',0),('handle','a'*64)])
def test_search_audit_rejects_changed_receipts(tmp_path,field,value):
    from reverpi.memory import Archive
    from reverpi.config import CompressionConfig
    from verify_paired_prefix import check_search_receipt
    content='prefix '*50+'needle'+ ' tail '*50
    archive=Archive(tmp_path/'a.sqlite',CompressionConfig(recovery_search_mode='match'))
    h=archive.put('n',content);args={'query':'needle','chars':1000};v=archive.recover('n','q',**args)
    v['matches'][0][field]=value
    with pytest.raises(ValueError):check_search_receipt(args,v,{h:content},'match')


def test_search_audit_uses_ascii_not_unicode_lowercase():
    from verify_paired_prefix import check_search_receipt
    with pytest.raises(ValueError,match='literal query'):
        check_search_receipt({'query':'ä'},{'matches':[{'handle':'a','excerpt':'Ä'}]}, {'a':'Ä'},'head')


def test_search_audit_does_not_claim_full_final_archive_inventory():
    from verify_paired_prefix import check_search_receipt
    assert check_search_receipt({'query':'needle'}, {'matches':[]}, {'later':'needle'},'head')==0


def test_search_audit_small_configured_default(tmp_path):
    from verify_paired_prefix import check_search_receipt
    text='prefix '*60+'needle'+' tail '*60
    archive=Archive(tmp_path/'a.sqlite',CompressionConfig(recovery_search_mode='match',recovery_chars=128))
    h=archive.put('n',text);value=archive.recover('n','q',query='needle',chars=128)
    assert check_search_receipt({'query':'needle'},value,{h:text},'match',128)==1
