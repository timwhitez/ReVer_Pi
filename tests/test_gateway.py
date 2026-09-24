import asyncio
import json
from pathlib import Path
import httpx
import pytest
from reverpi.config import StudyConfig,CompressionConfig
from reverpi.gateway import create_app,Sessions
from reverpi.memory import Record
from reverpi.util import canonical
from test_transport import raw


@pytest.fixture
def app(provider,tmp_path):
    app=create_app(provider,StudyConfig(methods=['pi_original','pi_native','archive','rever_lite','summary'],compression=CompressionConfig(recent_records=1)),tmp_path/'run')
    yield app


def auth(app,sid='one',method='rever_lite'):
    token=app.state.sessions.create(sid,method)
    return {'Authorization':'Bearer '+token}


@pytest.mark.asyncio
async def test_health_auth_sanitized_and_method_allowlist(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.get('/health')).json()=={'ok':True,'schema':1}
        assert (await c.get('/session')).status_code==401
        headers=auth(app)
        r=await c.get('/session',headers=headers)
        assert r.status_code==200 and r.json()['effort']=='low'
        assert 'api_key' not in r.text
        h=auth(app,'other','tail')
        assert (await c.get('/session',headers=h)).json()['error']['kind']=='method_not_in_plan'
    await app.state.client.close()


@pytest.mark.asyncio
async def test_complete_cache_effort_schema(app):
    h=auth(app)
    body={'op':'call1','messages':[{'role':'user','content':'hello'}],'effort':'low'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post('/complete',headers=h,json=body)
        assert r.status_code==200 and r.json()['text']=='{"ok":true}'
        assert (await c.post('/complete',headers=h,json=body)).json()==r.json()
        bad=await c.post('/complete',headers=h,json={**body,'effort':'high'})
        assert bad.json()['error']['kind']=='effort_changed'
        bad=await c.post('/complete',headers=h,json={**body,'SECRET_EXTRA':'DO_NOT_ECHO_ME'})
        assert bad.status_code==422 and 'DO_NOT_ECHO_ME' not in bad.text
        bad=await c.post('/complete',headers=h,json={**body,'op':'../evil'})
        assert bad.status_code==422
    assert app.state.ledger.totals()['attempts']==1
    await app.state.client.close()


@pytest.mark.asyncio
async def test_compaction_cas_idempotence_and_storage(app):
    h=auth(app)
    record={'id':'r1','kind':'goal','text':'Keep this goal.'}
    body={'op':'c1','expected_revision':0,'records':[record]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        first=await c.post('/compact',headers=h,json=body)
        assert first.status_code==200 and first.json()['revision']==1
        assert (await c.post('/compact',headers=h,json=body)).json()==first.json()
        r=await c.post('/compact',headers=h,json={**body,'op':'c2'})
        assert r.status_code==409 and r.json()['error']['kind']=='stale_revision'
        r=await c.post('/compact',headers=h,json={**body,'records':[{'id':'different','kind':'goal','text':'changed'}]})
        assert r.status_code==409
        info=(await c.get('/session',headers=h)).json()
        assert info['revision']==1 and 'Keep this goal.' in info['memory']['text']
    await app.state.client.close()


@pytest.mark.asyncio
async def test_compaction_failure_retains_prior(provider,tmp_path):
    app=create_app(provider,StudyConfig(methods=['rever_lite'],compression=CompressionConfig(memory_bytes=512,recent_records=1)),tmp_path/'g')
    h=auth(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        first=await c.post('/compact',headers=h,json={'op':'a','expected_revision':0,'records':[{'id':'g','kind':'goal','text':'a goal'}]})
        assert first.status_code==200
        bad=await c.post('/compact',headers=h,json={'op':'b','expected_revision':1,'records':[{'id':'h','kind':'constraint','text':'x'*1000}]})
        assert bad.status_code==422
        state=(await c.get('/session',headers=h)).json()
        assert state['revision']==1 and state['memory']==first.json()['memory']
    await app.state.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('method',['pi_original','pi_native'])
async def test_native_does_not_silently_use_custom_compactor(app,method):
    h=auth(app,method,method)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post('/compact',headers=h,json={'op':'a','expected_revision':0,'records':[{'id':'g','kind':'goal','text':'x'}]})
        assert r.json()['error']['kind']=='native_boundary'
    await app.state.client.close()


@pytest.mark.asyncio
async def test_session_revoked_and_cross_cell_archive(app):
    h1=auth(app,'one');h2=auth(app,'two')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post('/compact',headers=h1,json={'op':'a','expected_revision':0,'records':[{'id':'g','kind':'goal','text':'private evidence'}]})
        handle=r.json()['memory']['details']['handles']['g']
        own=await c.post('/recover',headers=h1,json={'op':'a','handle':handle})
        assert own.status_code==200
        bad=await c.post('/recover',headers=h2,json={'op':'a','handle':handle})
        assert bad.json()['error']['kind']=='archive_not_found'
        app.state.sessions.disable('one')
        assert (await c.get('/session',headers=h1)).status_code==401
    await app.state.client.close()


@pytest.mark.asyncio
async def test_body_stream_limit(provider,tmp_path):
    app=create_app(provider.model_copy(update={'max_response_bytes':1024}),StudyConfig(),tmp_path/'g')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post('/complete',content=b'x'*2000)
        assert r.status_code==413
    await app.state.client.close()


@pytest.mark.asyncio
async def test_revoke_inflight_cancels_but_keeps_reservation(provider,tmp_path):
    started=asyncio.Event();cancelled=asyncio.Event()
    async def delayed(req):
        started.set()
        try:await asyncio.sleep(10)
        except asyncio.CancelledError:cancelled.set();raise
        return httpx.Response(200,json=raw(provider))
    app=create_app(provider,StudyConfig(methods=['rever_lite']),tmp_path/'g',transport=httpx.MockTransport(delayed));h=auth(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        request=asyncio.create_task(c.post('/complete',headers=h,json={'op':'x','messages':[{'role':'user','content':'x'}]}))
        await started.wait();app.state.sessions.disable('one')
        r=await asyncio.wait_for(request,2)
        assert r.json()['error']['kind']=='session_revoked' and cancelled.is_set()
    assert app.state.ledger.totals()['unknown_attempts']==1
    await app.state.client.close()


@pytest.mark.asyncio
async def test_complete_early_response_headers_streaming(provider,tmp_path):
    """early_response_headers: headers go out before the provider resolves; body carries
    the completion JSON, and in-flight LabErrors ride inside a 200 body as {"error": ...}."""
    app=create_app(provider,StudyConfig(methods=['rever_lite'],early_response_headers=True),tmp_path/'g_early')
    h=auth(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post('/complete',headers=h,json={'op':'a','messages':[{'role':'user','content':'hi'}]})
        assert r.status_code==200
        body=r.json()
        assert body['text'] and 'error' not in body
    # An in-flight upstream failure surfaces after headers: 200 + {"error": ...} body.
    async def failing(req):
        return httpx.Response(500,json={'error':{'message':'boom'}})
    app2=create_app(provider,StudyConfig(methods=['rever_lite'],early_response_headers=True),tmp_path/'g_early2',transport=httpx.MockTransport(failing))
    h2=auth(app2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),base_url='http://test') as c:
        r2=await c.post('/complete',headers=h2,json={'op':'b','messages':[{'role':'user','content':'hi'}]})
        assert r2.status_code==200
        assert r2.json()['error']['kind'] in ('upstream_transient','upstream_5xx')
    await app2.state.client.close()
    await app.state.client.close()
