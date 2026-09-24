"""Cross-layer fail-closed and research-identity tests, not real benchmark trials."""
import asyncio
import copy
import json
from pathlib import Path
import httpx
import pytest
from reverpi.config import Provider, StudyConfig, CompressionConfig
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.protocols import Message
from reverpi.transport import APIClient
from reverpi.memory import Archive, Compressor, Record
from reverpi.benchmark import collect_matrix
from reverpi.review import review_jobs
from reverpi.util import canonical, source_manifest, digest
from test_transport import raw

@pytest.mark.parametrize('status',[400,401,429])
@pytest.mark.asyncio
async def test_error_usage_malformed_keeps_unknown_not_zero(tmp_path,provider,ledger,status):
    p=provider.model_copy(update={'retry':provider.retry.model_copy(update={'max_attempts':1})})
    response={'error':{'code':'rejected'},'usage':{'prompt_tokens':'unknown','completion_tokens':5}}
    async with APIClient(p,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(status,json=response))) as c:
        with pytest.raises(LabError): await c.complete([Message('user','x')],op='op',cell='c')
    assert ledger.totals()['unknown_attempts']==1
    assert json.loads(ledger.attempts()[0]['raw_usage'])==response['usage']

@pytest.mark.parametrize('changes',[{'model':''},{'name':''},{'base_url':'https://example.invalid:invalid/v1'},
    {'base_url':'https://example.invalid:99999/v1'},{'base_url':'https://example.invalid/v1\n'},
    {'extra_headers_env':{'X-Example':'ONE','x-example':'TWO'}}])
def test_invalid_provider_fails_before_operation_claim(changes):
    with pytest.raises(ValueError):Provider(**changes)

@pytest.mark.asyncio
async def test_gateway_invalid_protocol_does_not_mutate_archive(tmp_path,provider):
    app=create_app(provider,StudyConfig(),tmp_path/'g');token=app.state.sessions.create('s','mask')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
            r=await c.post('/complete',headers={'Authorization':'Bearer '+token},json={'op':'op','messages':[{'role':'tool','call_id':'absent','content':'not legal evidence'}]})
            assert r.status_code==422
        archive=Archive(tmp_path/'g/archive.sqlite',StudyConfig().compression)
        assert archive.used('s')==0
    finally:await app.state.client.close()

@pytest.mark.asyncio
async def test_gateway_advertises_actual_recovery_limits(tmp_path,provider):
    cfg=StudyConfig(compression=CompressionConfig(recovery_chars=999,recovery_calls=2))
    app=create_app(provider,cfg,tmp_path/'g');token=app.state.sessions.create('s','mask')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
            j=(await c.get('/session',headers={'Authorization':'Bearer '+token})).json()
        assert j['recovery']=={'max_chars':999,'max_calls':2}
    finally:await app.state.client.close()

@pytest.mark.asyncio
async def test_shared_rever_summary_header_isolation(tmp_path):
    cfg=CompressionConfig(memory_bytes=1500,recent_records=0,hybrid_threshold_bytes=256)
    ready=asyncio.Event();resume=asyncio.Event()
    class Pausing(Compressor):
        async def _generate(self,*args,**kwargs):
            ready.set();await resume.wait()
            return 'x'*1000,[]
    c=Pausing(cfg,Archive(tmp_path/'a',cfg))
    a=asyncio.create_task(c.compress([Record(id='n',kind='note',text='history'*100)],'rever_summary',cell='a'))
    await asyncio.wait_for(ready.wait(),1)
    with pytest.raises(LabError):
        await c.compress([Record(id='g',kind='goal',text='x'*1200,updates={'tree':'v'*500})],'summary',cell='b')
    resume.set()
    result=await a
    assert result.bytes<=cfg.memory_bytes and result.generated

def test_reviewer_supplementary_chunks_obey_byte_limit(tmp_path):
    (tmp_path/'scripts').mkdir();(tmp_path/'scripts/x.py').write_text(('x="'+'a'*90+'"\n')*120)
    _,jobs=review_jobs(tmp_path,2,chunk_bytes=500)
    assert jobs
    assert all(len(j['source'].encode())<=500 for j in jobs)

def test_reviewer_rejects_single_oversize_line_before_dispatch(tmp_path):
    (tmp_path/'src').mkdir();(tmp_path/'src/transport.py').write_text('X="'+'a'*2000+'"\n')
    with pytest.raises(ValueError):review_jobs(tmp_path,3,chunk_bytes=512)


def test_benchmark_collect_rejects_other_plan_ledger(tmp_path,monkeypatch,provider):
    from test_review_v11_processes import matrix_setup
    args=matrix_setup(tmp_path,monkeypatch,provider)
    asyncio.run(__import__('reverpi.benchmark',fromlist=['run_matrix']).run_matrix(*args))
    # Same provider/study/code, but ledger belongs to a different plan.
    from reverpi.ledger import Ledger
    cfg=StudyConfig(methods=['mask','rever_lite']); ledger=Ledger(args[2]/'ledger.sqlite',cfg.budget)
    with ledger.db(True) as db:db.execute("UPDATE meta SET value=? WHERE key='benchmark_plan'",(canonical('a-different-plan'),))
    with pytest.raises(LabError):collect_matrix(args[-1],args[2])

@pytest.mark.parametrize('text',['{"value":1,"value":2}','{"value":1e999}'])
def test_json_object_helper_uses_the_shared_strict_contract(text):
    from reverpi.util import json_object
    with pytest.raises(ValueError):json_object(text)
