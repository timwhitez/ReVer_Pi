import json
from pathlib import Path
import httpx
import pytest
from reverpi.config import CompressionConfig
from reverpi.memory import Archive,Compressor,Record,METHODS
from reverpi.transport import APIClient
from reverpi.errors import LabError
from reverpi.util import canonical,digest
from test_transport import raw


def history(noise=600):
    return [Record(id='goal',kind='goal',text='Deliver the patch without losing requirements.'),
            Record(id='rule',kind='constraint',text='Never access external networks.'),
            Record(id='v1',kind='edit',text='Revision A',updates={'tree':'A'}),
            Record(id='oldtest',kind='verification',text='The old revision passed tests.',dependencies={'tree':'A'}),
            Record(id='build',kind='observation',text='progress '*noise,dependencies={'tree':'A'}),
            Record(id='v2',kind='edit',text='Dependencies modified to B',updates={'tree':'B'}),
            Record(id='todo',kind='obligation',text='Current revision B is unverified.',dependencies={'tree':'B'})]


@pytest.mark.asyncio
@pytest.mark.parametrize('method',list(METHODS))
async def test_methods_have_real_implementations(provider,ledger,tmp_path,method):
    cfg=CompressionConfig(memory_bytes=18000,recent_records=1,hybrid_threshold_bytes=1000)
    archive=Archive(tmp_path/'archive.sqlite',cfg)
    async with APIClient(provider,ledger) as api:
        m=await Compressor(cfg,archive,api).compress(history(100),method,cell='c',active_query='Dependencies B tests')
    assert m.method==method and m.bytes==len(m.text.encode())
    assert m.bytes<=cfg.memory_bytes or method=='full'
    assert history()[0].text in m.text and history()[1].text in m.text
    assert m.details['validity']['oldtest']=='stale'
    assert '"tree":"B"' in m.text
    assert not m.semantic_certified
    assert m.persistent_metadata_bytes>0
    assert m.archived_bytes==archive.used('c')


@pytest.mark.asyncio
@pytest.mark.parametrize('method',['tail','mask','archive','lexical','rever_lite','validity_only'])
async def test_deterministic_needs_no_provider(tmp_path,method):
    cfg=CompressionConfig(recent_records=1)
    m=await Compressor(cfg,Archive(tmp_path/'a',cfg)).compress(history(),method,cell='c')
    assert not m.generated


@pytest.mark.asyncio
async def test_rever_priority_capacity_and_staleness(tmp_path):
    cfg=CompressionConfig(memory_bytes=1600,recent_records=1,excerpt_chars=80)
    m=await Compressor(cfg,Archive(tmp_path/'a',cfg)).compress(history(10000),'rever_lite',cell='c')
    assert m.bytes<=1600 and 'stale' in m.text and 'Current revision B is unverified' in m.text
    assert 'new_test' in m.text
    assert 'progress '*10000 not in m.text


@pytest.mark.asyncio
async def test_protected_overflow_not_truncated(tmp_path):
    cfg=CompressionConfig(memory_bytes=512,recent_records=0)
    with pytest.raises(LabError,match='exceeds'):
        await Compressor(cfg,Archive(tmp_path/'a',cfg)).compress([Record(id='g',kind='goal',text='不可以丢失'*100)],'rever_lite',cell='c')


@pytest.mark.asyncio
async def test_recursive_preserves_goals_versions_and_old_memory(tmp_path):
    cfg=CompressionConfig(recent_records=1)
    c=Compressor(cfg,Archive(tmp_path/'a',cfg))
    a=await c.compress(history(),'rever_lite',cell='c')
    b=await c.compress([Record(id='later',kind='observation',text='Current output',dependencies={'tree':'B'})], 'rever_lite',cell='c',previous=a)
    assert history()[0].text in b.text and history()[1].text in b.text
    assert b.details['current_versions']=={'tree':'B'} and b.details['validity']['later']=='snapshot_matches_not_a_new_test'
    assert set(a.record_ids)<=set(b.record_ids)
    assert a.text in b.text


@pytest.mark.asyncio
async def test_same_id_changed_or_method_changed_rejected(tmp_path):
    cfg=CompressionConfig(recent_records=1)
    c=Compressor(cfg,Archive(tmp_path/'a',cfg));a=await c.compress(history(),'rever_lite',cell='c')
    with pytest.raises(LabError,match='reused'):
        await c.compress([Record(id='oldtest',kind='verification',text='Now current tests passed')],'rever_lite',cell='c',previous=a)
    with pytest.raises(LabError,match='switch'):
        await c.compress([Record(id='x',kind='note',text='x')],'archive',cell='c',previous=a)


@pytest.mark.asyncio
async def test_summary_does_not_see_diagnostic_future(provider,ledger,tmp_path):
    cfg=CompressionConfig(recent_records=1)
    captured=[]
    def handler(req):
        captured.append(json.loads(req.content))
        return httpx.Response(200,json=raw(provider,canonical({'summary':'Unverified tree B.','citations':['todo']})))
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handler)) as api:
        m=await Compressor(cfg,Archive(tmp_path/'a',cfg),api).compress(history(),'summary',cell='c',active_query='HIDDEN_FUTURE_SENTINEL')
    assert 'HIDDEN_FUTURE_SENTINEL' not in canonical(captured)
    assert m.generated and not m.semantic_certified and 'max_summary_utf8_bytes' in canonical(captured)


@pytest.mark.asyncio
@pytest.mark.parametrize('answer',['{"summary":"x","citations":["not-supplied"]}','{"summary":"","citations":[]}','{"summary":"x"}','{"summary":"x","citations":[],"extra":true}','not JSON'])
async def test_invalid_summary_never_committed(provider,ledger,tmp_path,answer):
    cfg=CompressionConfig(recent_records=1)
    async with APIClient(provider,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw(provider,answer)))) as api:
        with pytest.raises(LabError,match='Summary JSON'):
            await Compressor(cfg,Archive(tmp_path/'a',cfg),api).compress(history(),'summary',cell='c')
    assert ledger.totals()['known_tokens']==18


@pytest.mark.asyncio
async def test_unknown_dependency_is_not_verified(tmp_path):
    cfg=CompressionConfig(recent_records=0)
    m=await Compressor(cfg,Archive(tmp_path/'a',cfg)).compress([Record(id='x',kind='verification',text='Earlier pass',dependencies={'unknown_file':'ABC'})],'rever_lite',cell='c')
    assert m.details['validity']['x']=='unknown'


@pytest.mark.asyncio
async def test_rever_summary_replaces_selected_text(provider,ledger,tmp_path):
    cfg=CompressionConfig(memory_bytes=9000,recent_records=1,hybrid_threshold_bytes=512)
    def handler(req):return httpx.Response(200,json=raw(provider,canonical({'summary':'A long build observation was archived; it is from A, not current B.','citations':['build']})))
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handler)) as api:
        m=await Compressor(cfg,Archive(tmp_path/'a',cfg),api).compress(history(600),'rever_summary',cell='c')
    assert m.generated and 'build' in m.details['replaced_record_ids']
    assert 'progress '*500 not in m.text and 'stale' in m.text
    assert m.bytes<5000


@pytest.mark.asyncio
async def test_metadata_quota_explicit(tmp_path):
    cfg=CompressionConfig(memory_bytes=18000,max_metadata_bytes=1024,recent_records=0)
    with pytest.raises(LabError,match='bookkeeping'):
        await Compressor(cfg,Archive(tmp_path/'a',cfg)).compress([Record(id=f'n{i}',kind='note',text='x') for i in range(50)],'tail',cell='c')


def test_archive_dedup_utf8_exact_and_isolation(tmp_path):
    a=Archive(tmp_path/'a',CompressionConfig())
    h=a.put('one','中文abcdef')
    assert a.put('one','中文abcdef')==h and a.used('one')==12
    assert a.recover('one','r',handle=h,start=1,chars=3)['text']=='文ab'
    assert a.recover('one','r',handle=h,start=1,chars=3)['text']=='文ab'
    with pytest.raises(LabError,match='cross-cell'):a.recover('two','r',handle=h)
    with pytest.raises(LabError,match='different'):a.recover('one','r',handle=h,start=0)


def test_archive_quota_does_not_evict(tmp_path):
    a=Archive(tmp_path/'a',CompressionConfig(archive_bytes=1024))
    h=a.put('c','x'*1024)
    with pytest.raises(LabError,match='capacity'):a.put('c','y')
    assert a.recover('c','r',handle=h)['text']=='x'*1024


@pytest.mark.parametrize('kwargs',[{}, {'handle':'nope'}, {'handle':'a'*64,'query':'q'}, {'query':''}, {'query':'a','start':True}, {'query':'a','chars':0}, {'query':'a','chars':6001}, {'query':'a','start':-1}])
def test_recovery_invalid_arguments(tmp_path,kwargs):
    a=Archive(tmp_path/'a',CompressionConfig())
    with pytest.raises(LabError):a.recover('c','r',**kwargs)


def test_recovery_quota_and_literal_search(tmp_path):
    a=Archive(tmp_path/'a',CompressionConfig(recovery_calls=1))
    a.put('c','literal % not wildcard');a.put('c','other text')
    r=a.recover('c','same',query='%')
    assert len(r['matches'])==1
    assert a.recover('c','same',query='%')==r
    with pytest.raises(LabError,match='call limit'):a.recover('c','new',query='other')


@pytest.mark.parametrize('search',[False,True])
def test_archive_corruption_all_recovery_paths(tmp_path,search):
    a=Archive(tmp_path/'a',CompressionConfig());h=a.put('c','original')
    with a.connect() as db:db.execute('UPDATE blobs SET content=? WHERE namespace=?',('tampered','c'))
    with pytest.raises(LabError,match='hash'):
        a.recover('c','r',**({'query':'tampered'} if search else {'handle':h}))


def test_recovery_beyond_end(tmp_path):
    a=Archive(tmp_path/'a',CompressionConfig());h=a.put('c','one')
    with pytest.raises(LabError,match='beyond'):a.recover('c','r',handle=h,start=4)


@pytest.mark.asyncio
async def test_duplicate_record_rejected(tmp_path):
    cfg=CompressionConfig();a=Record(id='same',kind='note',text='x')
    with pytest.raises(LabError,match='unique'):
        await Compressor(cfg,Archive(tmp_path/'a',cfg)).compress([a,a],'tail',cell='c')
