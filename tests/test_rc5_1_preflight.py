"""No new live-model calls: regression tests for the failed RC5 campaign contracts."""
from pathlib import Path
import json
import sqlite3
import copy
import httpx
import pytest

from reverpi.config import Provider, Budget
from reverpi.errors import LabError
from reverpi.ledger import Ledger
from reverpi.protocols import Message
from reverpi.transport import APIClient
from reverpi.util import canonical, digest, bytes_digest
from reverpi.preflight import (validate_provider_environment, validate_gold_mapping,
    certify_gold, validate_gold_certificate, budget_screen)
from reverpi.lean import (init_campaign, prepare_dev, prepare_campaign_review,
    campaign_preflight, run_slot)
from reverpi.intervention_matrix import execute_matrix
from reverpi.lean_review import prepare_review, run_review, validate_review
from test_rc4_matrix import prep

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def live(provider):
    return provider.model_copy(update={'mock':False,'model':'deepseek-flash','concurrency':1,
                                      'api_key_env':'REVER_TEST_ONLY_SECRET'})

@pytest.mark.parametrize('value',[None,'',' ','a\nb','a\rb','secret\tbad','é','a\x00b'])
def test_bad_credentials_are_safe_preflight_error(live,monkeypatch,value):
    if value is None:monkeypatch.delenv(live.api_key_env,raising=False)
    elif '\x00' in value:
        monkeypatch.setattr('reverpi.preflight.os.environ', {live.api_key_env:value})
    else:monkeypatch.setenv(live.api_key_env,value)
    with pytest.raises(LabError) as e:validate_provider_environment(live)
    assert e.value.kind=='configuration_preflight' and not e.value.ambiguous
    assert e.value.message == 'Missing or invalid credential environment variable: ' + live.api_key_env

@pytest.mark.parametrize('value',[None,'','x\r\ny','非ASCII'])
def test_bad_extra_headers_preflight(live,monkeypatch,value):
    monkeypatch.setenv(live.api_key_env,'valid-key')
    p=live.model_copy(update={'extra_headers_env':{'X-Project':'REVER_TEST_HEADER'}})
    if value is None:monkeypatch.delenv('REVER_TEST_HEADER',raising=False)
    else:monkeypatch.setenv('REVER_TEST_HEADER',value)
    with pytest.raises(LabError):validate_provider_environment(p)

def test_missing_ca_redacts_path(live,monkeypatch):
    monkeypatch.setenv(live.api_key_env,'valid-key')
    monkeypatch.setenv('REVER_CA_TEST','/missing/private/ca.pem')
    with pytest.raises(LabError) as e:validate_provider_environment(live.model_copy(update={'ca_bundle_env':'REVER_CA_TEST'}))
    assert '/missing' not in e.value.message

def test_mock_needs_no_credentials(provider,monkeypatch):
    monkeypatch.delenv(provider.api_key_env,raising=False)
    assert validate_provider_environment(provider)['mock']

def test_good_credentials_never_emitted(live,monkeypatch):
    monkeypatch.setenv(live.api_key_env,'valid-key-private')
    assert 'valid-key-private' not in canonical(validate_provider_environment(live))

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_missing_credential_no_claim_no_attempt_no_network(live,monkeypatch,tmp_path,protocol):
    monkeypatch.delenv(live.api_key_env,raising=False)
    ledger=Ledger(tmp_path/'ledger.sqlite',Budget())
    calls=[]
    def forbidden(req):calls.append(req);raise AssertionError('Network must not be used')
    p=live.model_copy(update={'protocol':protocol})
    async with APIClient(p,ledger,transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(LabError) as e:
            await client.complete([Message('user','local test')],op='must-not-claim',cell='x')
    assert e.value.kind=='configuration_preflight' and not calls
    with ledger.db() as db:
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0

@pytest.mark.parametrize('answers',[
    {'wrong':['yes']},{'q':[]},{'q':'yes'},{'q':[1]}, {'q':['yes'],'extra':['no']},
])
def test_wrong_gold_rejected_before_any_execution(answers):
    raw=canonical({'answers':answers}).encode()
    with pytest.raises(ValueError):validate_gold_mapping(raw,bytes_digest(raw),['q'])

def test_duplicate_gold_keys_rejected():
    raw=b'{"answers":{"q":["a"],"q":["b"]}}'
    with pytest.raises(ValueError):validate_gold_mapping(raw,bytes_digest(raw),['q'])

def test_gold_hash_mismatch_rejected():
    with pytest.raises(ValueError,match='commitment'):validate_gold_mapping(b'{"answers":{"q":["x"]}}','f'*64,['q'])

@pytest.mark.asyncio
async def test_gold_certificate_contains_no_answers(tmp_path,provider):
    matrix,h=await prep(tmp_path/'base',provider,repeats=1)
    plan=json.loads((matrix/'plan.json').read_text())
    parent=json.loads((matrix/'parent.json').read_text())
    gold={'answers':{q['id']:['sensitive-answer-marker'] for q in parent['checkpoint']['questions']}}
    g=tmp_path/'gold.json';g.write_text(canonical(gold));gs=bytes_digest(g.read_bytes())
    receipt=tmp_path/'cert.json'
    certify_gold(matrix/'parent.json',plan['parent_sha256'],g,gs,receipt)
    text=receipt.read_text();assert 'sensitive-answer-marker' not in text
    obj=json.loads(text);validate_gold_certificate(obj,parent,plan['parent_sha256'],gs)
    with pytest.raises(FileExistsError):certify_gold(matrix/'parent.json',plan['parent_sha256'],g,gs,receipt)
    obj['answer_values_included']=True
    with pytest.raises(LabError):validate_gold_certificate(obj,parent,plan['parent_sha256'],gs)

@pytest.mark.asyncio
async def test_missing_credential_matrix_is_not_poisoned(tmp_path,provider,monkeypatch):
    # Offline preparation needs no secret; live dispatch fails before ledger/arms/workspaces.
    p=provider.model_copy(update={'mock':False,'api_key_env':'REVER_TEST_ONLY_SECRET'})
    matrix,h=await prep(tmp_path/'base',p,repeats=1)
    monkeypatch.delenv(p.api_key_env,raising=False)
    before={str(x.relative_to(matrix)):x.read_bytes() for x in matrix.rglob('*') if x.is_file()}
    with pytest.raises(LabError) as e:
        await execute_matrix(matrix,h,allow_paid=True,acknowledge_unsandboxed=True)
    after={str(x.relative_to(matrix)):x.read_bytes() for x in matrix.rglob('*') if x.is_file()}
    assert e.value.kind=='configuration_preflight' and before==after
    assert not (matrix/'ledger.sqlite').exists() and not (matrix/'arms').exists()

@pytest.mark.asyncio
async def test_missing_credential_review_does_not_make_ledger(tmp_path,live,monkeypatch):
    monkeypatch.delenv(live.api_key_env,raising=False)
    out=tmp_path/'review'
    result=prepare_review(ROOT,out,live,Budget(),[{'file':'src/reverpi/__init__.py'}])
    with pytest.raises(LabError):await run_review(ROOT,out,result['plan_sha256'],allow_paid=True)
    assert not (out/'ledger.sqlite').exists() and not (out/'report.json').exists()

@pytest.mark.asyncio
async def test_budget_screen_blocks_original_four_arm_shape(tmp_path,provider):
    matrix,h=await prep(tmp_path/'base',provider,repeats=1)
    p=json.loads((matrix/'plan.json').read_text())
    p['provider']['max_output_tokens']=65536
    p['budget']['max_total_tokens']=90000;p['budget']['per_cell_tokens']=90000
    screen=budget_screen(matrix,p)
    assert screen['planned_units']==4
    assert screen['declared_output_only_stress_tokens']>90000
    assert not screen['stress_screen_passed'] and screen['paid_calls']==0
    assert not screen['completion_funding_certified']

@pytest.mark.asyncio
async def test_new_campaign_needs_explicit_authority(tmp_path,live,monkeypatch):
    monkeypatch.setenv(live.api_key_env,'good-key')
    out=tmp_path/'campaign';r=init_campaign(ROOT,out,live)
    prepare_campaign_review(ROOT,out,r['campaign_sha256'],[{'file':'src/reverpi/__init__.py'}])
    report=campaign_preflight(ROOT,out,r['campaign_sha256'],'review')
    assert 'no_new_paid_campaign_authorization' in report['blockers']
    with pytest.raises(LabError):await run_slot(ROOT,out,r['campaign_sha256'],'review',allow_paid=True)
    assert not (out/'review/ledger.sqlite').exists()

def test_review_nested_contract_remains_invalid():
    with pytest.raises(ValueError):validate_review('{"contract":{"verdict":"pass","overflow":false,"findings":[]}}',{'rows':[]})

@pytest.mark.asyncio
async def test_live_prepare_requires_gold_certificate(tmp_path,provider,live):
    matrix,h=await prep(tmp_path/'base',provider,repeats=1)
    m=json.loads((matrix/'plan.json').read_text())
    out=tmp_path/'campaign';r=init_campaign(ROOT,out,live)
    with pytest.raises(LabError) as e:
        prepare_dev(ROOT,out,r['campaign_sha256'],'dev-01',parent=matrix/'parent.json',
            parent_sha=m['parent_sha256'],workspace=matrix/'template',registry=matrix/'registry.json',
            registry_sha=m['registry_sha256'],gold_sha='f'*64,source_note='offline regression')
    assert e.value.kind=='evaluation_preflight' and not (out/'dev-01').exists()

@pytest.mark.asyncio
async def test_live_style_end_to_end_uses_certificate_but_not_gold(tmp_path,provider,live,monkeypatch):
    from test_transport import raw
    monkeypatch.setenv(live.api_key_env,'unit-test-key')
    matrix,h=await prep(tmp_path/'base',provider,repeats=1)
    old=json.loads((matrix/'plan.json').read_text())
    parent=json.loads((matrix/'parent.json').read_text())
    gold=tmp_path/'controller-gold.json'
    gold.write_text(canonical({'answers':{q['id']:['not-in-reader-payload'] for q in parent['checkpoint']['questions']}}))
    gs=bytes_digest(gold.read_bytes());cert=tmp_path/'controller-cert.json'
    certify_gold(matrix/'parent.json',old['parent_sha256'],gold,gs,cert)
    out=tmp_path/'new-campaign'
    r=init_campaign(ROOT,out,live,dev_tokens=400000,paid_execution_authorized=True)
    prepared=prepare_dev(ROOT,out,r['campaign_sha256'],'dev-01',parent=matrix/'parent.json',
        parent_sha=old['parent_sha256'],workspace=matrix/'template',registry=matrix/'registry.json',
        registry_sha=old['registry_sha256'],gold_sha=gs,source_note='mock-transport-only fixture',gold_certificate=cert)
    check=campaign_preflight(ROOT,out,r['campaign_sha256'],'dev-01',expected_binding_sha=prepared['slot_binding_sha256'])
    assert check['ready_for_paid_dispatch'] and check['network_requests']==0
    calls=[]
    def reply(req):
        assert b'not-in-reader-payload' not in req.content
        calls.append(req)
        answer={'answers':{q['id']:'unknown' for q in parent['checkpoint']['questions']}}
        return httpx.Response(200,json=raw(live,canonical(answer)))
    report=await run_slot(ROOT,out,r['campaign_sha256'],'dev-01',allow_paid=True,
                         acknowledge_unsandboxed=True,transport=httpx.MockTransport(reply),
                         expected_binding_sha=prepared['slot_binding_sha256'])
    assert report['visited_units']==4 and len(calls)==4
    again=await run_slot(ROOT,out,r['campaign_sha256'],'dev-01',allow_paid=True,
                         acknowledge_unsandboxed=True,transport=httpx.MockTransport(reply),
                         expected_binding_sha=prepared['slot_binding_sha256'])
    assert len(calls)==4 and all(x['reused_commit'] for x in again['units'])

@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'per_cell_tokens':10000},{'per_cell_attempts':1},{'max_attempts':3}])
async def test_budget_screen_honours_cell_and_attempt_caps(tmp_path,provider,change):
    matrix,h=await prep(tmp_path/'base',provider,repeats=1)
    plan=json.loads((matrix/'plan.json').read_text())
    plan['budget'].update(max_total_tokens=10_000_000,per_cell_tokens=1_000_000)
    plan['budget'].update(change)
    report=budget_screen(matrix,plan)
    assert not report['stress_screen_passed']
