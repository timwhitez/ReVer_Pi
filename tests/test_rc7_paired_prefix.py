from __future__ import annotations
import copy
import importlib.util
import json
from pathlib import Path
import sys
import hashlib

import httpx
import pytest

from reverpi.config import Provider, Budget, OnlineProjectionConfig
from reverpi.errors import LabError
from reverpi.paired_prefix import PairedPlan, PairedBackend, signature, fixture, workspace_manifest, RecordingTransport
from reverpi.protocols import Message, Completion
from reverpi.util import canonical, digest, source_manifest, seal_cache, atomic_write

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import paired_prefix_canary as runner


def provider(**kw):
    d = dict(mock=True, model='mock-reasoner', base_url='http://127.0.0.1:1/v1', max_output_tokens=65536,
             effort='low', concurrency=1, retry={'max_attempts':1,'total_seconds':60})
    d.update(kw)
    return Provider(**d)


def plan(**kw):
    d = dict(source_sha256=digest(source_manifest(ROOT)), provider=provider(),
             budget=Budget(max_total_tokens=4_000_000,per_cell_tokens=2_000_000), seed='test-seed-rc7')
    d.update(kw)
    return PairedPlan(**d)


@pytest.mark.parametrize('value', [['full'], ['full','full'], ['projected','projected'], [], ['projected','full','full']])
def test_bad_branch_inventory(value):
    with pytest.raises(ValueError): plan(branch_order=value)


@pytest.mark.parametrize('change', [dict(effort='medium'),dict(concurrency=2),dict(max_output_tokens=4096),
    dict(context_window=262144),dict(stream=True),dict(supports_native_compaction=True),
    dict(retry={'max_attempts':2}),dict(retry={'max_attempts':1,'retry_ambiguous':True}),
    dict(extra_headers_env={'X-User':'MY_HEADER'}),dict(ca_bundle_env='MY_CA')])
def test_contract_cannot_silently_change(change):
    with pytest.raises(ValueError): plan(provider=provider(**change))


@pytest.mark.parametrize('mode',['apply','off'])
def test_capture_cannot_be_projected(mode):
    with pytest.raises(ValueError): plan(policy=OnlineProjectionConfig(mode=mode))


@pytest.mark.parametrize('model',['deepseek-flash','gpt-6-luna'])
def test_supported_forks_are_explicit(model):
    p = plan(provider=provider(mock=False,model=model,base_url='https://example.invalid/v1'))
    assert p.paid_authorized is False


def test_other_model_rejected():
    with pytest.raises(ValueError): plan(provider=provider(mock=False,model='other'))


def test_signature_ignores_only_random_gateway_op():
    body = {'op':'one','messages':[{'role':'user','content':'x'}],'tools':[], 'observation_meta':[],
            'effort':'low','max_output_tokens':65536}
    b = copy.deepcopy(body); b['op']='two'
    assert signature(body) == signature(b)
    for key in ('tools','messages','observation_meta','effort','max_output_tokens'):
        b = copy.deepcopy(body); b[key]=None
        assert signature(body) != signature(b)
    with pytest.raises(LabError): signature({**body,'temperature':0})
    b = signature(body); b['messages'][0]['content']='mutated'
    assert body['messages'][0]['content']=='x'


@pytest.mark.parametrize('pressure',['short','long'])
def test_fixture_reproducible_and_gold_not_in_prompt(pressure):
    files, prompt, marker = fixture('sample-seed',pressure)
    assert (files,prompt,marker)==fixture('sample-seed',pressure)
    assert marker not in prompt
    assert sum(marker in t for t in files.values())==1
    assert len(files)==5
    assert all((len(t.encode())>10240)==(pressure=='long') for t in files.values())
    assert marker != fixture('different-seed',pressure)[2]


def test_workspace_symlink_rejected(tmp_path):
    (tmp_path/'file').write_text('ok')
    assert workspace_manifest(tmp_path)=={'file':hashlib.sha256(b'ok').hexdigest()}
    (tmp_path/'alias').symlink_to(tmp_path/'file')
    with pytest.raises(LabError): workspace_manifest(tmp_path)


def test_workspace_directory_rejected(tmp_path):
    (tmp_path/'sub').mkdir()
    with pytest.raises(LabError): workspace_manifest(tmp_path)


def test_existing_output_never_replayed(tmp_path):
    with pytest.raises(LabError): runner.new_destination(tmp_path)
    with pytest.raises(LabError): runner.new_destination(ROOT/'new-run')


def test_live_preflight_no_permission_no_provider_probe(monkeypatch):
    p=plan(provider=provider(mock=False,model='deepseek-flash',base_url='https://example.invalid/v1'))
    monkeypatch.setattr(runner,'validate_provider_environment',lambda p: (_ for _ in ()).throw(AssertionError('must not probe')))
    with pytest.raises(LabError):runner.preflight(p,allow_paid=False,acknowledge_local=False)


def test_live_missing_credentials_fail_before_files(monkeypatch,tmp_path):
    p=plan(provider=provider(mock=False,model='deepseek-flash',base_url='https://example.invalid/v1'),paid_authorized=True)
    monkeypatch.delenv('REVER_API_KEY',raising=False)
    monkeypatch.delenv('REVER_OFFLINE_VERIFICATION',raising=False)
    with pytest.raises(LabError):runner.preflight(p,allow_paid=True,acknowledge_local=True)
    assert not list(tmp_path.iterdir())


def test_offline_verification_rejects_live(monkeypatch):
    p=plan(provider=provider(mock=False,model='deepseek-flash'),paid_authorized=True)
    monkeypatch.setenv('REVER_OFFLINE_VERIFICATION','1')
    with pytest.raises(LabError):runner.preflight(p,allow_paid=True,acknowledge_local=True)


def test_wrong_source_rejected():
    p=plan(source_sha256='0'*64)
    with pytest.raises(LabError):runner.preflight(p,allow_paid=False,acknowledge_local=False)


@pytest.mark.asyncio
async def test_recorder_records_bodies_not_auth(tmp_path):
    p=provider()
    transport=RecordingTransport(httpx.MockTransport(lambda req:httpx.Response(200,json={'ok':True})),tmp_path/'wire',p)
    async with httpx.AsyncClient(transport=transport) as client:
        r=await client.post('http://127.0.0.1/test',json={'model':'mock-reasoner'},headers={'Authorization':'Bearer DO_NOT_WRITE'})
        assert r.json()=={'ok':True}
    assert all(b'DO_NOT_WRITE' not in f.read_bytes() for f in (tmp_path/'wire').iterdir())
    row=json.loads((tmp_path/'wire/index.json').read_text())[0]
    assert row['body_complete'] is True
    assert hashlib.sha256((tmp_path/'wire'/row['response_file']).read_bytes()).hexdigest()==row['response_sha256']


@pytest.mark.asyncio
async def test_recorder_response_limit_is_failure(tmp_path):
    p=provider(max_response_bytes=1024)
    t=RecordingTransport(httpx.MockTransport(lambda req:httpx.Response(200,content=b'x'*2048)),tmp_path/'wire',p)
    async with httpx.AsyncClient(transport=t) as c:
        with pytest.raises(LabError):await c.post('http://127.0.0.1/test',json={'model':'mock-reasoner'})
    row=json.loads((tmp_path/'wire/index.json').read_text())[0]
    assert row['body_complete'] is False and row['response_bytes']==0


@pytest.mark.asyncio
async def test_recorder_model_mismatch_blocks_transport(tmp_path):
    def inner(req): raise AssertionError('must not dispatch')
    t=RecordingTransport(httpx.MockTransport(inner),tmp_path/'wire',provider())
    async with httpx.AsyncClient(transport=t) as c:
        with pytest.raises(LabError):await c.post('http://127.0.0.1/test',json={'model':'wrong'})


class Body:
    def __init__(self, value):
        self.value=value
        for k,v in value.items():setattr(self,k,v)
    def model_dump(self):return copy.deepcopy(self.value)


def minimal_body(op='one'):
    return Body(dict(op=op,messages=[{'role':'user','content':'probe'}],tools=[
        {'name':'read','description':'read','parameters':{}},
        {'name':'recover_evidence','description':'recover','parameters':{}}],
        observation_meta=[],effort='low',max_output_tokens=65536))


def prepared(body, eligible=0, mode='observe'):
    return {'mode':mode,'eligible_count':eligible,'applied_count':eligible if mode=='apply' else 0,
            'source_messages':[asdict(Message.from_dict(m)) for m in body.messages],
            'sent_messages':[asdict(Message.from_dict(m)) for m in body.messages]}
from dataclasses import asdict


def backend_setup(tmp_path,phase='capture'):
    p=plan();(tmp_path/'workspace').mkdir();(tmp_path/'workspace/a').write_text('x')
    (tmp_path/phase).mkdir()
    if phase!='capture':
        b=minimal_body();r=Completion(text='done',calls=[],reasoning=None,response_items=None,usage={},model='mock-reasoner',response_id='x',stop='stop',raw={}).to_dict()
        atomic_write(tmp_path/'tape.json',seal_cache({'schema':1,'entries':[{'source':signature(b.model_dump()),'sent':[asdict(Message.from_dict(m)) for m in b.messages],'response':r,'central_op':'x'}]}))
        atomic_write(tmp_path/'boundary.json',seal_cache({'schema':1,'source':signature(b.model_dump())}))
    def inner(req):raise AssertionError('test must not hit upstream')
    return PairedBackend(root=ROOT,run=tmp_path,phase=phase,plan=p,workspace=tmp_path/'workspace',expected_workspace=workspace_manifest(tmp_path/'workspace'),actor_transport=httpx.MockTransport(inner))


@pytest.mark.asyncio
async def test_boundary_stops_before_operation_claim(tmp_path):
    b=backend_setup(tmp_path);body=minimal_body()
    try:
        with pytest.raises(LabError,match='captured') as e:
            await b(state={'id':'paired'},body=body,sent=[Message.from_dict(m) for m in body.messages],prepared=prepared(body,1))
        assert e.value.kind=='paired_boundary'
        assert b.ledger.totals()['attempts']==0
        with b.ledger.db() as db:assert db.execute('select count(*) from operations').fetchone()[0]==0
        assert (tmp_path/'boundary.json').exists()
    finally:await b.aclose()


@pytest.mark.asyncio
async def test_prefix_exact_replay_has_no_dispatch(tmp_path):
    b=backend_setup(tmp_path,'full');body=minimal_body('different-op')
    try:
        r=await b(state={'id':'paired'},body=body,sent=[Message.from_dict(m) for m in body.messages],prepared=prepared(body))
        assert r.text=='done' and b.replayed_count==1
        assert b.ledger.totals()['attempts']==0 and b.recorder.rows==[]
    finally:await b.aclose()


@pytest.mark.asyncio
async def test_prefix_mismatch_never_falls_back_live(tmp_path):
    b=backend_setup(tmp_path,'full');body=minimal_body();body.value['messages'][0]['content']='other'
    try:
        with pytest.raises(LabError) as e:
            await b(state={'id':'paired'},body=body,sent=[Message.from_dict(m) for m in body.messages],prepared=prepared(body))
        assert e.value.kind=='prefix_mismatch'
        assert b.ledger.totals()['attempts']==0
    finally:await b.aclose()


@pytest.mark.asyncio
async def test_workspace_change_blocks_capture(tmp_path):
    b=backend_setup(tmp_path);(tmp_path/'workspace/a').write_text('change');body=minimal_body()
    try:
        with pytest.raises(LabError):await b(state={'id':'paired'},body=body,sent=[],prepared=prepared(body))
        assert b.ledger.totals()['attempts']==0
    finally:await b.aclose()


@pytest.mark.parametrize('encoding',['gzip','deflate'])
def test_bounded_response_decoding(encoding):
    import gzip,zlib
    from reverpi.paired_prefix import decode_recorded_body
    data=b'{"response":"ok"}'
    packed=gzip.compress(data) if encoding=='gzip' else zlib.compress(data)
    assert decode_recorded_body(packed,encoding,100)==data
    with pytest.raises(LabError):decode_recorded_body(packed,encoding,3)
    with pytest.raises(LabError):decode_recorded_body(packed[:-3],encoding,100)
    with pytest.raises(LabError):decode_recorded_body(packed+packed,encoding,100)


def test_unknown_encoding_rejected():
    from reverpi.paired_prefix import decode_recorded_body
    with pytest.raises(LabError):decode_recorded_body(b'anything','br',100)


@pytest.mark.parametrize('raised', [False, True])
def test_permission_revoked_before_gateway_shutdown(monkeypatch, raised):
    import contextlib
    from types import SimpleNamespace
    calls = []
    @contextlib.contextmanager
    def fake_server(app):
        calls.append('start')
        try: yield 'http://127.0.0.1:1'
        finally: calls.append('shutdown')
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(disable=lambda sid:calls.append('revoke:'+sid))))
    monkeypatch.setattr(runner, 'server', fake_server)
    try:
        with runner.paired_server(app):
            calls.append('request')
            if raised: raise RuntimeError('timeout')
    except RuntimeError:
        assert raised
    assert calls == ['start','request','revoke:paired','shutdown']


@pytest.mark.parametrize('names', [[], ['capture','../other'], ['capture','full','../../escape'], ['capture','full','full'], ['projected'], ['capture','projected','full','extra']])
def test_phase_paths_cannot_escape_or_reorder(names):
    from verify_paired_prefix import validate_phase_order
    with pytest.raises(ValueError):
        validate_phase_order([{'phase':n} for n in names], ['full','projected'])


def test_phase_prefix_can_stop_without_discarding_an_arm():
    from verify_paired_prefix import validate_phase_order
    order=['projected','full']
    assert validate_phase_order([{'phase':'capture'},{'phase':'projected'}],order)==['capture','projected']
