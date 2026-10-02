import asyncio
import json
from datetime import datetime, timezone
import httpx
import pytest
from reverpi.transport import APIClient, retry_after, sse_events, consume_sse
from reverpi.protocols import Message
from reverpi.errors import LabError


def raw(p, text='ok', usage=True, finish='stop'):
    if p.protocol == 'chat_completions':
        x={'id':'r1','model':p.model,'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':finish}]}
        if usage:x['usage']={'prompt_tokens':11,'completion_tokens':7}
    else:
        x={'id':'r1','model':p.model,'status':'completed','output':[{'id':'m1','type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}
        if usage:x['usage']={'input_tokens':11,'output_tokens':7}
    return x


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_json_and_cache(provider,ledger,protocol):
    p=provider.model_copy(update={'protocol':protocol})
    seen=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(200,json=raw(p))
    async with APIClient(p,ledger,transport=httpx.MockTransport(handler)) as c:
        a=await c.complete([Message('user','test')],op='x',cell='c')
        b=await c.complete([Message('user','test')],op='x',cell='c')
        assert a.to_dict()==b.to_dict()
    assert len(seen)==1 and ledger.totals()['known_tokens']==18
    assert seen[0].url.path.endswith('/chat/completions' if protocol=='chat_completions' else '/responses')


@pytest.mark.asyncio
async def test_retry_rate_then_ok(provider,ledger):
    n=0
    def handle(req):
        nonlocal n;n+=1
        if n==1:return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded'}},headers={'Retry-After':'0'})
        return httpx.Response(200,json=raw(provider))
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handle)) as c:
        assert (await c.complete([Message('user','x')],op='x',cell='c')).text=='ok'
    assert n==2
    assert [r['actual_tokens'] for r in ledger.attempts()]==[0,18]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,kind',[(400,'invalid_request'),(401,'authentication'),(403,'permission'),(404,'endpoint_or_model'),(402,'quota'),(302,'redirect_rejected')])
async def test_nonretryable_admission(provider,ledger,status,kind):
    seen=[]
    def handle(req):seen.append(req);return httpx.Response(status,json={'error':{'message':'SECRET'}})
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(LabError) as e:await c.complete([Message('user','x')],op='x',cell='c')
    assert e.value.kind==kind and 'SECRET' not in str(e.value)
    assert len(seen)==1 and ledger.totals()['accounted_tokens']==0


@pytest.mark.asyncio
async def test_error_with_usage_is_billed(provider,ledger):
    async with APIClient(provider,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(400,json={'error':{'code':'bad'},'usage':{'prompt_tokens':5,'completion_tokens':3}}))) as c:
        with pytest.raises(LabError):await c.complete([Message('user','x')],op='x',cell='c')
    assert ledger.totals()['known_tokens']==8


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['server','read','write','malformed','oversize','disconnect'])
async def test_ambiguous_no_automatic_resend(provider,ledger,failure):
    n=0
    def handle(req):
        nonlocal n;n+=1
        if failure=='server':return httpx.Response(503,json={'error':{'message':'busy'}})
        if failure=='read':raise httpx.ReadTimeout('private provider message')
        if failure=='write':raise httpx.WriteTimeout('private provider message')
        if failure=='malformed':return httpx.Response(200,content=b'{oops')
        if failure=='oversize':return httpx.Response(200,content=b'x'*2048)
        raise httpx.RemoteProtocolError('EOF')
    p=provider.model_copy(update={'max_response_bytes':1024})
    async with APIClient(p,ledger,transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(LabError):await c.complete([Message('user','x')],op='x',cell='c')
        with pytest.raises(LabError):await c.complete([Message('user','x')],op='x',cell='c')
    assert n==1 and ledger.totals()['unknown_attempts']==1 and ledger.totals()['accounted_tokens']>0


@pytest.mark.asyncio
async def test_opt_in_ambiguous_retry_keeps_first_reservation(provider,ledger):
    p=provider.model_copy(update={'retry':provider.retry.model_copy(update={'retry_ambiguous':True})})
    n=0
    def handle(req):
        nonlocal n;n+=1
        if n==1:raise httpx.ReadError('interrupted')
        return httpx.Response(200,json=raw(p))
    async with APIClient(p,ledger,transport=httpx.MockTransport(handle)) as c:
        await c.complete([Message('user','x')],op='x',cell='c')
    assert n==2 and ledger.totals()['unknown_attempts']==1 and ledger.totals()['known_tokens']==18
    assert ledger.totals()['accounted_tokens']>18


@pytest.mark.asyncio
async def test_connect_retry_is_safe(provider,ledger):
    n=0
    def handle(req):
        nonlocal n;n+=1
        if n<3:raise httpx.ConnectError('dns')
        return httpx.Response(200,json=raw(provider))
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handle)) as c:
        await c.complete([Message('user','x')],op='x',cell='c')
    assert n==3 and ledger.totals()['unknown_attempts']==0 and ledger.totals()['known_tokens']==18


@pytest.mark.asyncio
async def test_long_retry_after_records_cooldown_not_hammer(provider,ledger):
    async with APIClient(provider,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'999'},json={'error':{'code':'rate_limit'}}))) as c:
        with pytest.raises(LabError,match='Retry-After'):
            await c.complete([Message('user','x')],op='x',cell='c')
    assert ledger.totals()['attempts']==1
    assert ledger.throttle_delay(provider.name,10,100000,100000000)>990


@pytest.mark.asyncio
async def test_empty_and_length_outputs_are_charged(provider,ledger):
    responses=[raw(provider,text=''),raw(provider,text='partial',finish='length')]
    async with APIClient(provider,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=responses.pop(0)))) as c:
        for i in range(2):
            with pytest.raises(LabError):await c.complete([Message('user','x')],op=str(i),cell='c')
    assert ledger.totals()['known_tokens']==36 and ledger.totals()['unknown_attempts']==0


@pytest.mark.asyncio
async def test_no_usage_preserves_reservation(provider,ledger):
    async with APIClient(provider,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw(provider,usage=False)))) as c:
        r=await c.complete([Message('user','x')],op='x',cell='c')
    assert r.text=='ok' and ledger.totals()['unknown_attempts']==1


@pytest.mark.asyncio
async def test_cancel_after_rate_retry_not_mislabeled_free(provider,ledger):
    dispatched=asyncio.Event();n=0
    async def handle(req):
        nonlocal n;n+=1
        if n==1:return httpx.Response(429,json={'error':{'code':'rate_limit'}})
        dispatched.set();await asyncio.sleep(20)
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handle)) as c:
        t=asyncio.create_task(c.complete([Message('user','x')],op='x',cell='c'))
        await dispatched.wait();t.cancel()
        with pytest.raises(asyncio.CancelledError):await t
        with pytest.raises(LabError):await c.complete([Message('user','x')],op='x',cell='c')
    a=ledger.attempts()
    assert a[0]['actual_tokens']==0 and a[1]['actual_tokens'] is None


@pytest.mark.asyncio
async def test_same_operation_parallel_only_one_dispatch(provider,ledger):
    n=0
    async def handle(req):
        nonlocal n;n+=1;await asyncio.sleep(.01);return httpx.Response(200,json=raw(provider))
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handle)) as c:
        rs=await asyncio.gather(*(c.complete([Message('user','x')],op='same',cell='c') for _ in range(12)))
    assert n==1 and len(rs)==12


@pytest.mark.asyncio
async def test_response_model_drift_still_billed(provider,ledger):
    n=0
    def handle(req):
        nonlocal n;n+=1;r=raw(provider);r['model']='v1' if n==1 else 'v2';return httpx.Response(200,json=r)
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handle)) as c:
        await c.complete([Message('user','x')],op='a',cell='c')
        with pytest.raises(LabError,match='changed'):await c.complete([Message('user','x')],op='b',cell='c')
    assert ledger.totals()['known_tokens']==36


@pytest.mark.parametrize('headers,expected',[
    ({'retry-after':'5'},5),({'retry-after':'-2'},0),({'retry-after-ms':'250'},.25),
    ({'retry-after':'NaN'},None),({'retry-after':'inf'},None),({'retry-after-ms':'Infinity'},None),
    ({'retry-after':'bad'},None),({'retry-after':'Wed, 01 Jan 2020 00:00:07 GMT'},7),
])
def test_retry_after(headers,expected):
    assert retry_after(httpx.Headers(headers),datetime(2020,1,1,tzinfo=timezone.utc))==expected


async def chunks(data, n=1):
    for i in range(0,len(data),n):yield data[i:i+n]


@pytest.mark.asyncio
async def test_sse_unicode_crlf_multiline():
    b=':comment\r\ndata: {"text":\r\ndata: "中文"}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
    result=[x async for x in sse_events(chunks(b),10000)]
    assert result==[{'text':'中文'},'[DONE]']


@pytest.mark.asyncio
@pytest.mark.parametrize('data',[b'data: {oops}\n\n',b'data: {}\n',b'data: [1]\n\n',b'data: \xff\n\n',b'data: "\xe4'])
async def test_sse_malformed(data):
    with pytest.raises(LabError):[x async for x in sse_events(chunks(data),10000)]


def event(x):return ('data: '+json.dumps(x,ensure_ascii=False)+'\n\n').encode()
class ByteStream(httpx.AsyncByteStream):
    def __init__(self,data,cut=7):self.data,self.cut=data,cut
    async def __aiter__(self):
        async for c in chunks(self.data,self.cut):yield c


@pytest.mark.asyncio
async def test_stream_model_drift_persists_stop_and_blocks_next_operation(provider, ledger):
    data = event({'id':'r', 'model':provider.model, 'choices':[]})
    data += event({'id':'r', 'model':'different-model', 'choices':[]}) + b'data: [DONE]\n\n'
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=ByteStream(data))
    async with APIClient(provider, ledger, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LabError) as drift:
            await client.complete([Message('user','x')], op='drift', cell='c')
        assert drift.value.kind == 'model_drift'
        assert ledger.stop_reason() == 'model_drift'
        with pytest.raises(LabError) as stopped:
            await client.complete([Message('user','y')], op='later', cell='c')
        assert stopped.value.kind == 'run_stopped'
    assert len(seen) == 1
    assert ledger.totals()['unknown_attempts'] == 1


@pytest.mark.asyncio
async def test_chat_stream_tool_fragments(provider,ledger):
    data=event({'id':'r','model':provider.model,'choices':[{'index':0,'delta':{'reasoning_content':'思考','tool_calls':[{'index':0,'id':'call_1','function':{'name':'echo','arguments':'{"n":'}}]},'finish_reason':None}]})
    data+=event({'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':'1}'}}]},'finish_reason':'tool_calls'}]})
    data+=event({'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':8}})+b'data: [DONE]\n\n'
    async with APIClient(provider,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=ByteStream(data)))) as c:
        r=await c.complete([Message('user','x')],tools=[{'name':'echo','description':'echo','parameters':{'type':'object','properties':{}}}],op='s',cell='c')
    assert r.calls[0]['arguments']=={'n':1} and r.reasoning=='思考'
    assert ledger.totals()['known_tokens']==18


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_no_terminal_stream_never_executes_partial_tool(provider,ledger,protocol):
    p=provider.model_copy(update={'protocol':protocol})
    data=event({'id':'r','model':p.model,'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'c','function':{'name':'danger','arguments':'{}'}}]}}]})
    async with APIClient(p,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=ByteStream(data)))) as c:
        with pytest.raises(LabError,match='terminal'):await c.complete([Message('user','x')],op='s',cell='c')
    assert ledger.totals()['unknown_attempts']==1


@pytest.mark.asyncio
async def test_responses_terminal_preserves_opaque(provider,ledger):
    p=provider.model_copy(update={'protocol':'responses'})
    r=raw(p);r['output'].insert(0,{'id':'reason','type':'reasoning','summary':[],'encrypted_content':'UNCHANGED'})
    data=event({'type':'response.output_text.delta','delta':'not committed'})+event({'type':'response.completed','response':r})
    async with APIClient(p,ledger,transport=httpx.MockTransport(lambda q:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=ByteStream(data)))) as c:
        x=await c.complete([Message('user','x')],op='s',cell='c')
    assert x.response_items==r['output'] and x.text=='ok'


@pytest.mark.asyncio
async def test_duplicate_responses_terminal_rejected(provider):
    p=provider.model_copy(update={'protocol':'responses'})
    data=event({'type':'response.completed','response':raw(p)})*2
    with pytest.raises(LabError,match='Duplicate'):
        await consume_sse(p,httpx.Response(200,stream=ByteStream(data)))


@pytest.mark.asyncio
async def test_context_admission_is_explicit_not_silent_byte_window(provider,ledger):
    # Budget estimate counts bytes. Default advertised model window is NOT divided by ~4.
    p=provider.model_copy(update={'context_window':6000,'context_margin_tokens':100,'max_output_tokens':64})
    async with APIClient(p,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw(p)))) as c:
        await c.complete([Message('user','a'*7000)],op='allowed-upstream',cell='c')
    p2=p.model_copy(update={'name':'bound','context_admission':'byte_bound'})
    async with APIClient(p2,ledger,transport=httpx.MockTransport(lambda r:pytest.fail('must not dispatch'))) as c:
        with pytest.raises(LabError,match='Conservative'):await c.complete([Message('user','a'*7000)],op='bound',cell='c')


@pytest.mark.asyncio
async def test_total_deadline_reserves_ambiguous(provider,ledger):
    # Claim/reservation now consume this same budget. Leave enough setup margin
    # to exercise a dispatched attempt rather than a pre-dispatch expiry.
    p=provider.model_copy(update={'retry':provider.retry.model_copy(update={'total_seconds':1})})
    dispatched=asyncio.Event()
    async def hang(req):
        dispatched.set()
        await asyncio.sleep(10)
    async with APIClient(p,ledger,transport=httpx.MockTransport(hang)) as c:
        with pytest.raises(LabError,match='deadline'):await c.complete([Message('user','x')],op='x',cell='c')
    assert dispatched.is_set() and ledger.totals()['unknown_attempts']==1
