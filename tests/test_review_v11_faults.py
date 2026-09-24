"""Third-pass fault injection. Native tests use explicitly named local doubles."""
import asyncio
import copy
import itertools
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
import pytest
from reverpi.config import Budget,CompressionConfig,StudyConfig
from reverpi.errors import LabError
from reverpi.memory import Archive,Compressor,Record
from reverpi.ledger import Ledger
from reverpi.gateway import Sessions
from reverpi.protocols import Message,parse_completion
from reverpi.transport import APIClient,sse_events
from reverpi.util import canonical
from reverpi.review import validate_findings
from test_transport import ByteStream,raw,event


def test_corrupt_completed_model_cache_rejected(ledger):
    ledger.claim('a','hash','c');ledger.finish('a',result={'ok':True})
    with ledger.db(True) as db:
        text=db.execute('SELECT result FROM operations WHERE op=?',('a',)).fetchone()[0]
        db.execute('UPDATE operations SET result=? WHERE op=?',(text.replace('true','false'),'a'))
    with pytest.raises(LabError):ledger.claim('a','hash','c')


def test_corrupt_cached_recovery_rejected(tmp_path):
    a=Archive(tmp_path/'a',CompressionConfig());h=a.put('c','honest evidence')
    a.recover('c','r',handle=h)
    with a.connect() as db:
        text=db.execute('SELECT result FROM recovery').fetchone()[0]
        db.execute('UPDATE recovery SET result=?',(text.replace('honest','forged'),))
    with pytest.raises(LabError):a.recover('c','r',handle=h)


def test_existing_corrupt_blob_is_not_silently_reused(tmp_path):
    a=Archive(tmp_path/'a',CompressionConfig());a.put('c','honest')
    with a.connect() as db:db.execute('UPDATE blobs SET content=?',('forged',))
    with pytest.raises(LabError):a.put('c','honest')


@pytest.mark.asyncio
@pytest.mark.parametrize('table,column',[('sessions','memory'),('compact_ops','result')])
async def test_corrupt_persistent_memory_is_not_replayed(tmp_path,table,column):
    s=Sessions(tmp_path/'s');s.create('one','rever_lite')
    cfg=CompressionConfig();m=await Compressor(cfg,Archive(tmp_path/'a',cfg)).compress([Record(id='g',kind='goal',text='honest')],'rever_lite',cell='one')
    s.claim_compaction('one','op','hash',0);s.commit('one','op',0,m)
    with s.db() as db:
        v=db.execute(f'SELECT {column} FROM {table}').fetchone()[0]
        db.execute(f'UPDATE {table} SET {column}=?',(v.replace('honest','forged'),))
    with pytest.raises(LabError):
        if table=='sessions':s.get('one')
        else:s.claim_compaction('one','op','hash',0)


def test_reviewer_duplicate_verdict_cannot_pass(tmp_path):
    p=tmp_path/'x.py';p.write_text('x=1\n')
    job={'file':'x.py','first':1,'last':1}
    with pytest.raises(ValueError):validate_findings('{"verdict":"needs_changes","verdict":"pass","findings":[]}',job,tmp_path,False)


@pytest.mark.asyncio
async def test_case_insensitive_sse_content_type(provider,ledger):
    p=provider.model_copy(update={'stream':True})
    data=event({'id':'r','model':p.model,'choices':[{'index':0,'delta':{'content':'ok'},'finish_reason':'stop'}]})+b'data: [DONE]\n\n'
    async with APIClient(p,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'Content-Type':'Text/Event-Stream; charset=UTF-8'},stream=ByteStream(data)))) as c:
        assert (await c.complete([Message('user','x')],op='op',cell='c')).text=='ok'


@pytest.mark.parametrize('protocol',['chat_completions','responses'])
def test_malformed_wire_shape_mutation_sweep(provider,protocol):
    """Every mutation must either parse a valid answer or raise a classified LabError."""
    p=provider.model_copy(update={'protocol':protocol});base=raw(p)
    values=[None,False,True,0,1,1.5,'', 'unexpected', [],{},[None],{'x':1}]
    paths=[('model',),('id',),('usage',)]
    paths+=([('choices',),('choices',0),('choices',0,'index'),('choices',0,'message'),('choices',0,'finish_reason'),('choices',0,'message','content'),('choices',0,'message','tool_calls'),('choices',0,'message','reasoning_content'),('choices',0,'message','role')]
            if protocol=='chat_completions' else [('status',),('output',),('output',0),('output',0,'type'),('output',0,'content'),('output',0,'content',0),('output',0,'content',0,'text'),('output',0,'role'),('output',0,'status')])
    for path,v in itertools.product(paths,values):
        candidate=copy.deepcopy(base);target=candidate
        for k in path[:-1]:target=target[k]
        target[path[-1]]=copy.deepcopy(v)
        try:result=parse_completion(p,candidate)
        except LabError:continue
        assert isinstance(result.text,str) and result.text.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize('line_end',['\n','\r','\r\n'])
async def test_sse_every_two_chunk_boundary_with_utf8(line_end):
    payload=('\ufeff: keepalive'+line_end+'data: {"text":"中文🙂"}'+line_end+line_end+'data: [DONE]'+line_end+line_end).encode()
    for split in range(len(payload)+1):
        async def parts():
            yield payload[:split];yield payload[split:]
        assert [x async for x in sse_events(parts(),8192)]==[{'text':'中文🙂'},'[DONE]']


@pytest.mark.asyncio
async def test_random_chunked_sse_seeded(provider):
    payload=('data: '+canonical({'text':'🙂'*20})+'\r\n\r\ndata: [DONE]\r\n\r\n').encode()
    for seed in range(50):
        rng=random.Random(seed)
        async def parts():
            offset=0
            while offset<len(payload):
                n=rng.randint(1,17);yield payload[offset:offset+n];offset+=n
        assert [x async for x in sse_events(parts(),8192)]==[{'text':'🙂'*20},'[DONE]']


def test_contended_currency_and_token_budget_remain_bounded(tmp_path):
    l=Ledger(tmp_path/'l',Budget(max_total_tokens=500,per_cell_tokens=500,max_total_usd=.005,max_attempts=100))
    for i in range(40):l.claim(str(i),'hash',str(i))
    def attempt(i):
        try:return l.reserve(str(i),str(i),'p',50,.001)
        except LabError:return None
    with ThreadPoolExecutor(max_workers=12) as ex:rows=list(ex.map(attempt,range(40)))
    assert sum(r is not None for r in rows)==5
    assert l.totals()['accounted_tokens']==250 and l.totals()['accounted_usd']==pytest.approx(.005)


@pytest.mark.asyncio
async def test_many_identical_operations_dispatch_only_once(provider,ledger):
    calls=[]
    async def handler(req):calls.append(1);await asyncio.sleep(.01);return httpx.Response(200,json=raw(provider))
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handler)) as c:
        rows=await asyncio.gather(*(c.complete([Message('user','same')],op='same',cell='same') for _ in range(25)))
    assert len(rows)==25 and len(calls)==1 and ledger.totals()['attempts']==1

@pytest.mark.parametrize('usage',[
    {'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':10},'prompt_tokens_details':{'cached_tokens':20}},
    {'input_tokens':100,'output_tokens':20,'output_tokens_details':{'reasoning_tokens':10},'completion_tokens_details':{'reasoning_tokens':19}},
    {'input_tokens':100,'output_tokens':20,'input_tokens_details':{},'prompt_tokens_details':[]},
    {'input_tokens':100,'output_tokens':20,'output_tokens_details':{},'completion_tokens_details':[]},
    {'input_tokens':100,'output_tokens':20,'output_tokens_details':{'reasoning_tokens':None}},
])
def test_all_present_usage_detail_aliases_are_validated(usage):
    from reverpi.protocols import usage_parts
    assert usage_parts(usage) is None


def test_optional_null_detail_alias_does_not_hide_known_cached_tokens():
    from reverpi.protocols import usage_parts
    assert usage_parts({'input_tokens':100,'output_tokens':20,'input_tokens_details':None,
                        'prompt_tokens_details':{'cached_tokens':60}})==(100,20,60)
