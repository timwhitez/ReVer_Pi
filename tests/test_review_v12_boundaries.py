"""v1.2 counterexamples: all Providers and task engines remain explicit doubles."""
import asyncio
import json
import gc
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
import pytest
from reverpi.config import Provider, StudyConfig, CompressionConfig, Budget
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.ledger import Ledger
from reverpi.memory import Archive, Compressor, Record
from reverpi.protocols import Message
from reverpi.transport import APIClient
from reverpi.util import strict_json_loads, source_manifest
from test_transport import raw

@pytest.mark.parametrize('text', ['1e999', '-1e999', '{"x":1e400}', '[1e400]'])
def test_exponent_overflow_is_invalid_json_number(text):
    with pytest.raises(ValueError): strict_json_loads(text)

@pytest.mark.parametrize('content_type', [None, 'application/json', 'application/json; charset=utf-8'])
@pytest.mark.asyncio
async def test_duplicate_json_is_rejected_even_without_media_type(tmp_path, provider, content_type):
    app=create_app(provider, StudyConfig(), tmp_path/'g')
    token=app.state.sessions.create('s', 'mask')
    h={'Authorization':'Bearer '+token}
    if content_type: h['Content-Type']=content_type
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
            response=await c.post('/complete',headers=h,content=b'{"op":"a","op":"b","messages":[{"role":"user","content":"hello"}]}')
            assert response.status_code==422
        assert app.state.ledger.totals()['attempts']==0
    finally: await app.state.client.close()

@pytest.mark.asyncio
async def test_revocation_at_response_completion_does_not_deliver(tmp_path, provider):
    app=None
    async def handler(req):
        app.state.sessions.disable('s')
        return httpx.Response(200,json=raw(provider))
    app=create_app(provider, StudyConfig(), tmp_path/'g',transport=httpx.MockTransport(handler))
    token=app.state.sessions.create('s','mask')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
            response=await c.post('/complete',headers={'Authorization':'Bearer '+token},json={'op':'a','messages':[{'role':'user','content':'x'}]})
            assert response.status_code!=200
            assert response.json()['error']['kind']=='session_revoked'
        assert app.state.ledger.totals()['known_tokens']==18
    finally: await app.state.client.close()

@pytest.mark.asyncio
async def test_api_cookies_never_cross_cells(provider, ledger):
    seen=[]
    def handler(req):
        seen.append(req.headers.get('cookie'))
        return httpx.Response(200,json=raw(provider),headers={'Set-Cookie':'hidden_state=taskA; Path=/'})
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handler)) as c:
        await c.complete([Message('user','A')],op='a',cell='A')
        await c.complete([Message('user','B')],op='b',cell='B')
        assert seen==[None,None]
        assert not list(c.http.cookies.jar)

@pytest.mark.parametrize('header',['Cookie','cookie','Connection','Transfer-Encoding','Accept-Encoding'])
def test_unsafe_extra_headers_cannot_change_transport_contract(header):
    with pytest.raises(ValueError): Provider(extra_headers_env={header:'TEST_HEADER'})

def test_reservation_cannot_charge_another_cell(ledger):
    ledger.claim('op','sha','original')
    with pytest.raises(LabError): ledger.reserve('op','different','test',100,0)
    assert ledger.totals()['attempts']==0

def test_shared_rate_gate_is_atomic_across_clients(tmp_path, provider, monkeypatch):
    """Independent event loops simulate the previous check/reserve TOCTOU race."""
    p=provider.model_copy(update={'requests_per_minute':1,'retry':provider.retry.model_copy(update={'total_seconds':.25})})
    budget=Budget(max_total_tokens=1000000,per_cell_tokens=1000000)
    ledger=Ledger(tmp_path/'shared.sqlite', budget)
    barrier=threading.Barrier(2)
    old=Ledger.throttle_delay
    def split_check(self,*a,**kw):
        delay=old(self,*a,**kw)
        if delay==0: barrier.wait(timeout=3)
        return delay
    monkeypatch.setattr(Ledger,'throttle_delay',split_check)
    def job(i):
        async def run():
            async with APIClient(p,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw(p)))) as client:
                try:
                    await client.complete([Message('user','x')],op=f'op{i}',cell=f'cell{i}')
                    return 'complete'
                except LabError as e: return e.kind
        return asyncio.run(run())
    with ThreadPoolExecutor(2) as pool: outcomes=list(pool.map(job,[1,2]))
    assert outcomes.count('complete')==1
    assert ledger.totals()['attempts']==1

@pytest.mark.asyncio
async def test_finished_operation_locks_do_not_accumulate(provider, ledger):
    async with APIClient(provider,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw(provider)))) as c:
        for i in range(30): await c.complete([Message('user','x')],op=f'op{i}',cell='c')
        gc.collect()
        assert len(c.op_locks)<=1

@pytest.mark.asyncio
async def test_shared_compressor_does_not_share_capacity_state(tmp_path):
    cfg=CompressionConfig(memory_bytes=1024,recent_records=0)
    class Pausing(Compressor):
        async def _generate(self,records,cell,previous,method,target_bytes=None):
            ready.set();await resume.wait()
            return 'summary:'+'x'*700, []
    ready=asyncio.Event();resume=asyncio.Event()
    c=Pausing(cfg,Archive(tmp_path/'a',cfg))
    a=asyncio.create_task(c.compress([Record(id='n',kind='note',text='history')],'summary',cell='a'))
    await ready.wait()
    # A second call fails with a large header while the first is awaiting its LLM.
    with pytest.raises(LabError):
        await c.compress([Record(id='g',kind='goal',text='x'*700,updates={'tree':'v'*500})],'rever_lite',cell='b')
    resume.set()
    result=await a
    assert result.bytes<cfg.memory_bytes

@pytest.mark.parametrize('rel',['pi/tsconfig.json','scripts/runtime.txt'])
def test_implementation_identity_covers_native_config_and_runtime_assets(tmp_path,rel):
    p=tmp_path/rel;p.parent.mkdir(parents=True);p.write_text('a')
    before=source_manifest(tmp_path);p.write_text('b')
    assert before!=source_manifest(tmp_path)
