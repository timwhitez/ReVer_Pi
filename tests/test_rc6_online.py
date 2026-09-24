"""Contract tests for the opt-in, no-extra-model online observation candidate."""
from dataclasses import asdict, replace
import asyncio
import copy
import json
import random
import sqlite3
from pathlib import Path
import httpx
import pytest
from pydantic import ValidationError
from reverpi.config import StudyConfig, OnlineProjectionConfig, CompressionConfig, Budget
from reverpi.gateway import create_app
from reverpi.errors import LabError
from reverpi.memory import Archive
from reverpi.online_projection import ProjectionStore, ObservationMeta, placeholder, validate_observations, MARKER
from reverpi.protocols import Message, Completion, build_request
from reverpi.util import digest, canonical


def conversation(content=None, tool='read', error=False, opaque=False):
    if content is None: content='head\n'+'abcdef中😀\n'*1600+'\ntail'
    calls=[{'id':'old','name':tool,'arguments':{'path':'history.txt'}}]
    reasoning='reasoning-unchanged'
    items=None
    if opaque:
        reasoning=None
        items=[{'type':'reasoning','id':'opaque-r','encrypted_content':'PRESERVE_EXACT'},
               {'type':'function_call','id':'opaque-f','call_id':'old','name':tool,'arguments':'{"path":"history.txt"}'}]
    messages=[Message('system','Complete the task. No extra obligation.'),Message('user','An invariant 用户'),
              Message('assistant','',calls,reasoning=reasoning,response_items=items),Message('tool',content,call_id='old'),
              Message('assistant','', [{'id':'new','name':'read','arguments':{'path':'recent.txt'}}]),
              Message('tool','recent state',call_id='new')]
    meta=[ObservationMeta(call_id='old',tool_name=tool,content_sha=digest(content),is_error=error),
          ObservationMeta(call_id='new',tool_name='read',content_sha=digest('recent state'),is_error=False)]
    return messages,meta


def store(tmp_path,mode='apply',**kw):
    cfg=OnlineProjectionConfig(mode=mode,**kw)
    return ProjectionStore(tmp_path/'projection.sqlite',cfg),Archive(tmp_path/'archive.sqlite',CompressionConfig(recovery_calls=10))


def prepare(s,a,m,meta,op='p',namespace='cell'):
    return s.prepare(namespace,op,digest({'messages':[asdict(x) for x in m],'op':op}),m,meta,a)


def age(s,a,m,meta):
    for i in range(2):
        record=prepare(s,a,m,meta,op=f'p{i}');assert record['applied_count']==0
        s.complete('cell',f'p{i}')


@pytest.mark.parametrize('mode',['observe','apply'])
def test_first_two_full_exposures_then_boundary_projection(tmp_path,mode):
    s,a=store(tmp_path,mode);m,meta=conversation();before=copy.deepcopy(m)
    age(s,a,m,meta);r=prepare(s,a,m,meta,op='next')
    assert r['eligible_count']==1 and r['applied_count']==int(mode=='apply')
    assert m==before
    for i in [0,1,2,4,5]:assert r['source_messages'][i]==r['sent_messages'][i]
    assert (r['sent_utf8_bytes'] < r['source_utf8_bytes']) is (mode=='apply')
    assert r['native_compaction_commit'] is False and r['provider_tokens_measured'] is False
    rec=a.recover('cell','read',handle=meta[0].content_sha,start=12,chars=100)
    assert rec['text']==m[3].content[12:112]


def test_projection_durable_monotone_and_duplicate_exactly_once(tmp_path):
    s,a=store(tmp_path);m,meta=conversation();age(s,a,m,meta)
    before=prepare(s,a,m,meta,op='next');s.complete('cell','next');s.complete('cell','next')
    resumed=ProjectionStore(s.path,s.config)
    assert prepare(resumed,a,m,meta,op='next')==before
    # Even if a native boundary removes later observations, a projected old result
    # never alternates back to raw text just because it now ranks as recent.
    r=prepare(resumed,a,m[:4],meta[:1],op='later')
    assert r['applied_count']==1 and r['sent_messages'][3]['content']==before['sent_messages'][3]['content']
    with s.db() as db:
        row=db.execute("SELECT * FROM observations WHERE call_id='old'").fetchone()
        assert row['full_sends']==2 and row['packed']==1


@pytest.mark.parametrize('kind',['missing','duplicate','extra','hash','name'])
def test_metadata_contract_before_projection(tmp_path,kind):
    s,a=store(tmp_path);m,meta=conversation()
    if kind=='missing':meta=None
    elif kind=='duplicate':meta=meta+[meta[0]]
    elif kind=='extra':meta=meta+[meta[0].model_copy(update={'call_id':'absent'})]
    elif kind=='hash':meta[0]=meta[0].model_copy(update={'content_sha':'0'*64})
    else:meta[0]=meta[0].model_copy(update={'tool_name':'different'})
    with pytest.raises(LabError,match='projection_metadata'):prepare(s,a,m,meta)
    with s.db() as db:assert db.execute('select count(*) from projection_events').fetchone()[0]==0


@pytest.mark.parametrize('tool,error',[('read',True),('recover_evidence',False),('revalidate_evidence',False)])
def test_error_recovery_and_current_checks_remain_visible(tmp_path,tool,error):
    s,a=store(tmp_path);m,meta=conversation(tool=tool,error=error);age(s,a,m,meta)
    r=prepare(s,a,m,meta,op='next');assert r['sent_messages']==r['source_messages']
    assert r['eligible_count']==0


@pytest.mark.parametrize('content',['short', MARKER+'x'*12000])
def test_short_results_and_receipts_not_nested(tmp_path,content):
    s,a=store(tmp_path);m,meta=conversation(content);age(s,a,m,meta)
    assert prepare(s,a,m,meta,op='next')['applied_count']==0


def test_protect_current_tool_result_even_if_seen_twice(tmp_path):
    s,a=store(tmp_path);m,meta=conversation();m=m[:4];meta=meta[:1];age(s,a,m,meta)
    assert prepare(s,a,m,meta,op='next')['applied_count']==0


@pytest.mark.parametrize('change',['content','error','tool'])
def test_historical_tool_identity_immutable(tmp_path,change):
    s,a=store(tmp_path);m,meta=conversation();age(s,a,m,meta)
    if change=='content':
        m[3]=replace(m[3],content=m[3].content+'changed');meta[0]=meta[0].model_copy(update={'content_sha':digest(m[3].content)})
    elif change=='error':meta[0]=meta[0].model_copy(update={'is_error':True})
    else:
        m[2].calls[0]['name']='bash';meta[0]=meta[0].model_copy(update={'tool_name':'bash'})
    with pytest.raises(LabError,match='projection_identity_changed'):prepare(s,a,m,meta,op='next')


def test_hash_conflict_unresolved_and_failed_request_cannot_be_bypassed(tmp_path):
    s,a=store(tmp_path);m,meta=conversation();prepare(s,a,m,meta)
    with pytest.raises(LabError,match='idempotency_conflict'):
        s.prepare('cell','p','different',m,meta,a)
    with pytest.raises(LabError,match='operation_in_doubt'):prepare(s,a,m,meta,op='another')
    s.fail('cell','p','transport_ambiguous')
    with pytest.raises(LabError,match='projection_terminal'):prepare(s,a,m,meta)
    with pytest.raises(LabError,match='operation_in_doubt'):prepare(s,a,m,meta,op='another')
    with s.db() as db:assert db.execute('select count(*) from observations').fetchone()[0]==0


def test_separate_cells_have_separate_exposure_and_recovery(tmp_path):
    s,a=store(tmp_path);m,meta=conversation();age(s,a,m,meta)
    assert prepare(s,a,m,meta,op='next')['applied_count']==1
    assert prepare(s,a,m,meta,namespace='other')['applied_count']==0
    with pytest.raises(LabError,match='archive_not_found'):a.recover('not_here','r',handle=meta[0].content_sha)


def test_existing_archive_corruption_stops_before_dispatch(tmp_path):
    s,a=store(tmp_path);m,meta=conversation();age(s,a,m,meta)
    with a.connect() as db:db.execute('update blobs set content=? where handle=?',('wrong',meta[0].content_sha))
    with pytest.raises(LabError,match='archive_corruption'):prepare(s,a,m,meta,op='next')


@pytest.mark.parametrize('bound',[{'max_trace_events':1},{'max_trace_bytes':1024}])
def test_bounded_private_evidence_quota(tmp_path,bound):
    s,a=store(tmp_path,**bound);m,meta=conversation()
    if 'max_trace_events' in bound:prepare(s,a,m,meta);s.complete('cell','p')
    with pytest.raises(LabError,match='projection_quota'):prepare(s,a,m,meta,op='next')


def test_config_change_cannot_reuse_store(tmp_path):
    s,a=store(tmp_path)
    with pytest.raises(LabError,match='projection_config_changed'):
        ProjectionStore(s.path,OnlineProjectionConfig(mode='observe'))


@pytest.mark.parametrize('config',[{'full_exposures':0},{'keep_recent_results':0},{'min_observation_bytes':True},
                                    {'excerpt_bytes':10240},{'mode':'learning'},{'max_trace_events':0}])
def test_invalid_parameters_rejected(config):
    with pytest.raises(ValidationError):OnlineProjectionConfig(**config)


@pytest.mark.parametrize('kw',[{'methods':['summary']},{'split':'gate'},{'split':'external'},
                               {'compression':{'recovery_calls':0}}])
def test_candidate_cannot_silently_migrate_to_gate_or_unreachable_archive(kw):
    with pytest.raises(ValidationError):StudyConfig(**({'methods':['mask'],'online_projection':{'mode':'apply'}}|kw))


def test_utf8_property_and_exact_non_tool_protocol_preservation(tmp_path):
    rng=random.Random(6)
    for i in range(75):
        path=tmp_path/str(i);path.mkdir();s,a=store(path,min_observation_bytes=1024,excerpt_bytes=128)
        content=''.join(rng.choice('abc中😀é\n\t') for _ in range(2000))
        m,meta=conversation(content,opaque=bool(i%2));age(s,a,m,meta);r=prepare(s,a,m,meta,op='next')
        assert r['applied_count']==1
        assert r['sent_utf8_bytes']<r['source_utf8_bytes']
        assert r['sent_messages'][2]==r['source_messages'][2]
        assert r['sent_messages'][3]['content'].encode().decode()==r['sent_messages'][3]['content']
        assert a.recover('cell','r',handle=meta[0].content_sha,start=123,chars=234)['text']==content[123:357]


def test_noop_off_has_no_projection_database(tmp_path,provider):
    app=create_app(provider,StudyConfig(methods=['mask']),tmp_path)
    assert app.state.projection is None and not (tmp_path/'projection.sqlite').exists()
    asyncio.run(app.state.client.close())


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_real_gateway_boundary_and_cache_no_extra_model_request(tmp_path,provider,protocol):
    seen=[]
    def supplier(request):
        body=json.loads(request.content);seen.append(body)
        if protocol=='chat_completions':
            obj={'id':'s','model':'mock-reasoner','choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':10,'completion_tokens':4}}
        else:obj={'id':'s','model':'mock-reasoner','status':'completed','output':[{'type':'message','id':'x','role':'assistant','status':'completed','content':[{'type':'output_text','text':'ok'}]}],
                  'usage':{'input_tokens':10,'output_tokens':4}}
        return httpx.Response(200,json=obj)
    provider=provider.model_copy(update={'protocol':protocol})
    cfg=StudyConfig(methods=['mask','pi_original'],online_projection={'mode':'apply'},early_response_headers=True,
                    budget=Budget(max_total_tokens=1000000,per_cell_tokens=500000))
    app=create_app(provider,cfg,tmp_path/'g',transport=httpx.MockTransport(supplier));token=app.state.sessions.create('cell','mask')
    m,meta=conversation(opaque=protocol=='responses')
    if protocol=='responses':
        m[4].response_items=[{'type':'function_call','id':'fc-new','call_id':'new','name':'read','arguments':'{"path":"recent.txt"}'}]
    body={'messages':[asdict(x) for x in m],'observation_meta':[x.model_dump() for x in meta]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer '+token}) as c:
        try:
            for i in range(3):
                res=await c.post('/complete',json={'op':f'op{i}',**body});assert res.status_code==200 and 'error' not in res.json(),res.text
            cached=await c.post('/complete',json={'op':'op2',**body});assert 'error' not in cached.json()
            assert len(seen)==3
            key='messages' if protocol=='chat_completions' else 'input'
            assert MARKER not in canonical(seen[0][key]) and MARKER in canonical(seen[2][key])
            assert app.state.ledger.totals('cell')['attempts']==3
            with app.state.projection.db() as db:
                assert db.execute("select full_sends from observations where call_id='old'").fetchone()[0]==2
            native=await c.post('/compact',json={'op':'compact','expected_revision':0,'records':[{'id':'g','kind':'goal','text':'x'}]})
            assert native.status_code==422 and native.json()['error']['kind']=='native_boundary'
            before=len(seen)
            bad=await c.post('/complete',json={'op':'bad','messages':body['messages']})
            assert bad.json()['error']['kind']=='projection_metadata' and len(seen)==before
            r=await c.post('/recover',json={'op':'recover','handle':meta[0].content_sha,'start':33,'chars':100})
            assert r.json()['text']==m[3].content[33:133]
            # The original condition remains unprojected under this same gateway config.
            orig=app.state.sessions.create('original','pi_original')
            info=await c.get('/session',headers={'Authorization':'Bearer '+orig})
            assert info.json()['online_projection']['mode']=='off'
            for i in range(3):
                res=await c.post('/complete',headers={'Authorization':'Bearer '+orig},json={'op':f'o{i}','messages':body['messages']})
                assert 'error' not in res.json()
            assert all(MARKER not in canonical(x[key]) for x in seen[-3:])
        finally:await app.state.client.close()


@pytest.mark.asyncio
async def test_bad_metadata_never_claims_a_paid_operation(tmp_path,provider):
    calls=0
    def supplier(request):
        nonlocal calls;calls+=1
        raise RuntimeError('must never dispatch')
    app=create_app(provider,StudyConfig(methods=['mask'],online_projection={'mode':'apply'}),tmp_path,transport=httpx.MockTransport(supplier))
    t=app.state.sessions.create('c','mask');m,meta=conversation()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer '+t}) as c:
        r=await c.post('/complete',json={'op':'x','messages':[asdict(x) for x in m]})
        assert r.status_code==422 and r.json()['error']['kind']=='projection_metadata'
    assert calls==0 and app.state.ledger.totals()['attempts']==0
    await app.state.client.close()
