"""Adversarial regressions added during the second delivery review.

These are authored tests, not independent model/subagent judgments.
"""
import asyncio
import copy
import json
from pathlib import Path
import httpx
import pytest
from reverpi.config import Provider, Budget, StudyConfig, CompressionConfig, Prices
from reverpi.errors import LabError
from reverpi.ledger import Ledger
from reverpi.memory import Archive, Compressor, Record, _validity
from reverpi.protocols import Message, build_request, parse_completion, normalize_usage
from reverpi.transport import APIClient, sse_events, consume_sse
from reverpi.data import make_fixtures
from reverpi.study import run_study
from reverpi.util import canonical
from test_transport import raw, ByteStream, event


def test_known_invalid_dependency_dominates_another_unknown_dependency():
    r = Record(id='e', kind='verification', text='old pass', dependencies={'tree':'A', 'env':'1'})
    assert _validity(r, {'tree':'B'}) == 'stale'


@pytest.mark.asyncio
async def test_old_replayed_update_cannot_roll_back_recursive_version(tmp_path):
    cfg = CompressionConfig(recent_records=0)
    c = Compressor(cfg, Archive(tmp_path/'a',cfg))
    a = Record(id='a', kind='edit', text='A', updates={'tree':'A'})
    b = Record(id='b', kind='edit', text='B', updates={'tree':'B'})
    prev = await c.compress([a,b], 'rever_lite', cell='x')
    new = Record(id='new', kind='verification', text='pass on B', dependencies={'tree':'B'})
    nxt = await c.compress([a,new], 'rever_lite', cell='x', previous=prev)
    assert nxt.details['current_versions']['tree'] == 'B'
    assert nxt.details['validity']['new'] == 'snapshot_matches_not_a_new_test'


@pytest.mark.asyncio
async def test_stopped_study_does_not_score_unattempted_cells_as_failures(tmp_path, provider):
    make_fixtures(tmp_path/'data',2)
    cfg=StudyConfig(methods=['full','mask'])
    out=tmp_path/'run'; out.mkdir()
    ledger=Ledger(out/'ledger.sqlite',cfg.budget); ledger.stop('operator_stop')
    cfgpath=tmp_path/'c.yaml'; cfgpath.write_text('split: search\n')
    await run_study(Path(__file__).resolve().parents[1],provider,cfg,tmp_path/'data/public.jsonl',
                    tmp_path/'data/gold.jsonl',out,config_path=cfgpath)
    rows=json.loads((out/'results.json').read_text())
    assert all(r['status']=='pending' and r['success'] is None for r in rows)
    assert ledger.totals()['attempts']==0


@pytest.mark.asyncio
async def test_identity_omission_is_infrastructure_unknown_not_method_failure(tmp_path,provider):
    make_fixtures(tmp_path/'data',1);cfg=StudyConfig(methods=['full'])
    def handler(req):
        r=raw(provider,text='ok'); r.pop('model'); return httpx.Response(200,json=r)
    await run_study(Path(__file__).resolve().parents[1],provider,cfg,tmp_path/'data/public.jsonl',
                    tmp_path/'data/gold.jsonl',tmp_path/'run',config_path=tmp_path/'unused',transport=httpx.MockTransport(handler))
    row=json.loads((tmp_path/'run/results.json').read_text())[0]
    assert row['status']=='infrastructure_error' and row['success'] is None
    assert row['cost']['known_tokens']==18


@pytest.mark.parametrize('bad', [
    {'choices':[None]},
    {'choices':[{'message':None,'finish_reason':'stop'}]},
    {'choices':[{'message':{'role':'user','content':'x'},'finish_reason':'stop'}]},
    {'choices':[{'message':{'role':'assistant','content':'x'},'finish_reason':'stop','index':1}]},
    {'choices':[{'message':{'role':'assistant','content':False},'finish_reason':'stop'}]},
])
def test_chat_schema_always_fails_with_structured_protocol_error(provider,bad):
    r=raw(provider);r.update(bad)
    with pytest.raises(LabError):parse_completion(provider,r)


@pytest.mark.parametrize('item', [
    None,
    {'type':'message','role':'user','content':[{'type':'output_text','text':'ok'}]},
    {'type':'message','role':'assistant','status':'incomplete','content':[{'type':'output_text','text':'partial'}]},
    {'type':'message','content':[None]},
    {'type':'message','content':[{'type':'output_text','text':3}]},
    {'type':'function_call','name':'f','call_id':'c','arguments':'{}','status':'in_progress'},
])
def test_responses_bad_or_incomplete_items_are_never_committed(provider,item):
    p=provider.model_copy(update={'protocol':'responses'})
    r=raw(p);r['output']=[item]
    with pytest.raises(LabError):parse_completion(p,r)


def test_chat_stop_cannot_execute_tool_calls(provider):
    r=raw(provider)
    r['choices'][0]['message']['tool_calls']=[{'type':'function','id':'c','function':{'name':'f','arguments':'{}'}}]
    with pytest.raises(LabError):parse_completion(provider,r)


def test_duplicate_tool_argument_keys_are_not_silently_overwritten(provider):
    r=raw(provider,finish='tool_calls')
    r['choices'][0]['message']['tool_calls']=[{'type':'function','id':'c','function':{'name':'f','arguments':'{"x":1,"x":2}'}}]
    with pytest.raises(LabError):parse_completion(provider,r)


def test_responses_tool_optional_fields_remain_non_strict(provider):
    p=provider.model_copy(update={'protocol':'responses'})
    _,body=build_request(p,[Message('user','x')],[{'name':'f','description':'x','parameters':{'type':'object','properties':{'optional':{'type':'string'}}}}])
    assert body['tools'][0].get('strict') is False


def test_opaque_replay_cannot_disagree_with_visible_tool_call(provider):
    p=provider.model_copy(update={'protocol':'responses'})
    msg=Message('assistant','',calls=[{'id':'c','name':'f','arguments':{'path':'new'}}],response_items=[{'type':'function_call','call_id':'c','name':'f','arguments':'{"path":"old"}'}])
    with pytest.raises(LabError):build_request(p,[msg,Message('tool','ok',call_id='c')])


@pytest.mark.parametrize('value',[17, True, -1, '18'])
def test_conflicting_total_usage_is_unknown_not_underbilled(value):
    assert normalize_usage({'input_tokens':11,'output_tokens':7,'total_tokens':value},Prices())[0] is None


@pytest.mark.parametrize('separator',['\n','\r\n','\r'])
@pytest.mark.asyncio
async def test_sse_bom_and_all_spec_line_endings(separator):
    b=('\ufeffdata: {"ok":true}'+separator+separator+'data: [DONE]'+separator+separator).encode()
    async def chunks():
        for x in b:yield bytes([x])
    assert [x async for x in sse_events(chunks(),4096)] == [{'ok':True},'[DONE]']


@pytest.mark.asyncio
async def test_chat_stream_identity_drift_is_rejected(provider):
    b=event({'id':'a','model':provider.model,'choices':[{'index':0,'delta':{'content':'one'}}]})
    b+=event({'id':'b','model':provider.model,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})+b'data: [DONE]\n\n'
    with pytest.raises(LabError):await consume_sse(provider,httpx.Response(200,stream=ByteStream(b)))


@pytest.mark.asyncio
async def test_model_drift_latches_a_run_stop(provider,ledger):
    p=provider.model_copy(update={'expected_response_model':'expected'})
    async with APIClient(p,ledger,transport=httpx.MockTransport(lambda req:httpx.Response(200,json=raw(p)))) as client:
        with pytest.raises(LabError):await client.complete([Message('user','x')],op='x',cell='c')
    assert ledger.stop_reason() == 'model_drift'


def test_known_tokens_unknown_currency_can_be_reconciled(tmp_path):
    l=Ledger(tmp_path/'l',Budget());l.claim('o','s','c');a=l.reserve('o','c','p',100,1.0)
    l.settle(a,tokens=10,usd=None)
    l.reconcile(a,10,.2,'provider invoice line 1')
    assert l.totals()['currency_is_fully_known'] and l.totals()['known_usd']==pytest.approx(.2)
