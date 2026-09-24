"""Second-pass integration/counterexample review; no live Pi or Provider required."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from reverpi.config import load,Provider,StudyConfig
from reverpi.errors import LabError
from reverpi.util import canonical,source_manifest
from reverpi.data import make_fixtures,load_jsonl,freeze,source_identity_keys,audit
from reverpi.study import run_study
from reverpi.cli import execute
from reverpi.ledger import Ledger
from reverpi.gateway import create_app
from reverpi.benchmark import read_official_result
from reverpi.transport import consume_sse
from reverpi.protocols import Message,validate_messages,parse_completion
from test_transport import ByteStream,event,raw

ROOT=Path(__file__).resolve().parents[1]


def test_duplicate_budget_yaml_keys_rejected(tmp_path):
    p=tmp_path/'c.yaml';p.write_text('budget:\n  max_attempts: 2\n  max_attempts: 99999\n')
    with pytest.raises(ValueError):load(p,StudyConfig)


def test_duplicate_dataset_json_keys_rejected(tmp_path):
    p=tmp_path/'d.jsonl';p.write_text('{"split":"external","split":"search"}\n')
    with pytest.raises(ValueError):load_jsonl(p)


def test_source_symlink_cannot_escape_frozen_identity(tmp_path):
    (tmp_path/'src').mkdir();(tmp_path/'code.py').write_text('x=1')
    (tmp_path/'src/live.py').symlink_to(tmp_path/'code.py')
    with pytest.raises(ValueError):source_manifest(tmp_path)


def test_github_transport_aliases_share_provenance():
    a=source_identity_keys('a',{'repository':'https://github.com/Team/Repo.git'})
    b=source_identity_keys('b',{'repository':'git@github.com:team/repo.git'})
    c=source_identity_keys('c',{'repository':'github.com/team/repo'})
    assert a & b & c


def test_single_split_audit_does_not_certify_cross_split_separation(tmp_path):
    make_fixtures(tmp_path,1)
    assert audit([tmp_path/'public.jsonl'])['project_disjoint'] is None


def test_external_freeze_requires_development_source_inventory(tmp_path):
    make_fixtures(tmp_path/'d',1);p=tmp_path/'d/public.jsonl';r=load_jsonl(p)[0]
    r.update(split='external',synthetic=False,provenance={'benchmark':'owned-name-not-real-verification'})
    p.write_text(canonical(r)+'\n');c=tmp_path/'c';c.write_text('split: external')
    with pytest.raises(LabError,match='audit_scope'):
        freeze(ROOT,c,p,tmp_path/'d/gold.jsonl',[p],tmp_path/'freeze')


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['remove_cell','change_success','alter_answer'])
async def test_analysis_rechecks_frozen_grid_and_artifacts(tmp_path,provider,mutation):
    make_fixtures(tmp_path/'d',1);out=tmp_path/'r'
    await run_study(ROOT,provider,StudyConfig(methods=['full','mask']),tmp_path/'d/public.jsonl',tmp_path/'d/gold.jsonl',out,config_path=tmp_path/'unused')
    p=out/'results.json';r=json.loads(p.read_text())
    if mutation=='remove_cell':r.pop()
    elif mutation=='change_success':r[0]['success']=not r[0]['success']
    else:
        answer=out/'answers'/f"{r[0]['cell']}.json";answer.write_text('{"tampered":true}')
    p.write_text(canonical(r))
    with pytest.raises(LabError):execute(SimpleNamespace(command='analyze',run=str(out),baseline='full',unseal=False,out=None))


@pytest.mark.asyncio
async def test_analysis_refreshes_reconciled_accounting(tmp_path,provider):
    from reverpi.mock import mock_transport
    transport=mock_transport(provider)
    async def omit_usage(req):
        r=await transport.handle_async_request(req);await r.aread();j=r.json();j.pop('usage',None)
        return httpx.Response(200,json=j)
    make_fixtures(tmp_path/'d',1);out=tmp_path/'r';cfg=StudyConfig(methods=['full'])
    await run_study(ROOT,provider,cfg,tmp_path/'d/public.jsonl',tmp_path/'d/gold.jsonl',out,config_path=tmp_path/'unused',transport=httpx.MockTransport(omit_usage))
    ledger=Ledger(out/'ledger.sqlite',cfg.budget);a=ledger.attempts()[0]
    ledger.reconcile(a['id'],500,0,'external-invoice-test-only')
    result=execute(SimpleNamespace(command='analyze',run=str(out),baseline='full',unseal=False,out=None))
    assert result['table']['full']['known_tokens']==500 and result['table']['full']['unknown_attempts']==0


@pytest.mark.parametrize('malformed',[{'config':None},{'config':{'agent':None}},{'verifier_result':[]},{'verifier_result':{'rewards':{'reward':None}}}])
def test_invalid_official_result_yields_structured_error(tmp_path,malformed):
    result={'trial_name':'t','task_name':'t','config':{'agent':{'kwargs':{'cell_id':'cell'}}},'verifier_result':{'rewards':{'reward':1}}}
    result.update(malformed);(tmp_path/'result.json').write_text(canonical(result))
    with pytest.raises(LabError):read_official_result(tmp_path,{'cell':'cell'},'reward',1.0)


def test_wrong_official_method_is_rejected(tmp_path):
    r={'trial_name':'t','task_name':'t','config':{'agent':{'kwargs':{'cell_id':'cell','method':'rever_lite'}}},'verifier_result':{'rewards':{'reward':1}}}
    (tmp_path/'result.json').write_text(canonical(r))
    with pytest.raises(LabError):read_official_result(tmp_path,{'cell':'cell','method':'mask'},'reward',1.0)


@pytest.mark.asyncio
async def test_gateway_slow_body_has_a_deadline(tmp_path,provider):
    study=StudyConfig().model_copy(update={'gateway_body_seconds':0.02})
    app=create_app(provider,study,tmp_path/'gateway');token=app.state.sessions.create('slow','mask')
    async def slow():
        yield b'{';await asyncio.sleep(.06);yield b'"op":"x","messages":[{"role":"user","content":"x"}]}'
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
            r=await c.post('/complete',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},content=slow())
            assert r.status_code==408
        assert app.state.ledger.totals()['attempts']==0
    finally:await app.state.client.close()


@pytest.mark.asyncio
async def test_gateway_duplicate_keys_do_not_dispatch(tmp_path,provider):
    app=create_app(provider,StudyConfig(),tmp_path/'g');token=app.state.sessions.create('dup','mask')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
            r=await c.post('/complete',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},content=b'{"op":"a","op":"b","messages":[{"role":"user","content":"x"}]}')
            assert r.status_code==422
        assert app.state.ledger.totals()['attempts']==0
    finally:await app.state.client.close()


@pytest.mark.asyncio
async def test_failed_responses_event_cannot_embed_a_completed_response(provider):
    p=provider.model_copy(update={'protocol':'responses'})
    with pytest.raises(LabError):await consume_sse(p,httpx.Response(200,stream=ByteStream(event({'type':'response.failed','response':raw(p)}))))


@pytest.mark.parametrize('m',[Message(role={}),Message(role='tool',call_id=[]),Message(role='user',content='\ud800')])
def test_malformed_canonical_message_is_a_protocol_error(m):
    with pytest.raises(LabError):validate_messages([m])


def test_surrogate_tool_argument_is_not_executable(provider):
    r=raw(provider,finish='tool_calls');r['choices'][0]['message']['tool_calls']=[{'id':'c','type':'function','function':{'name':'f','arguments':'{"path":"\\ud800"}'}}]
    with pytest.raises(LabError):parse_completion(provider,r)
