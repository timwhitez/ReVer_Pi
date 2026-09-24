import asyncio
import hashlib
import json
import os
import secrets
import sys
import threading
from pathlib import Path
import httpx
import pytest
from reverpi.config import CompressionConfig
from reverpi.data import Checkpoint, Question
from reverpi.memory import Archive,Compressor,Record
from reverpi.paired_interventions import Arm,freeze_intervention,read_intervention,run_arm
from reverpi.protocols import Message,Completion,validate_messages
from reverpi.revalidation import VerifierSpec,execute_verifier,load_registry,within
from reverpi.transport import APIClient
from reverpi.transport_trace import exception_signature
from reverpi.errors import LabError
from reverpi.util import digest,canonical


def records(secret):
    return [Record(id='goal',kind='goal',text='Complete the audit.'),
      Record(id='rule',kind='constraint',text='No network access.'),
      Record(id='receipt',kind='observation',text='Audit receipt\n'+'progress\n'*800+'TOKEN='+secret+'\n'+'progress\n'*800),
      Record(id='tail',kind='note',text='Await the next question.')]

@pytest.mark.asyncio
@pytest.mark.parametrize('method',['mask','rever_lite','tail','lexical'])
async def test_extractive_success_can_omit_unprotected_id(tmp_path,method):
    secret=secrets.token_hex(16);rs=records(secret)
    cfg=CompressionConfig(memory_bytes=2048,recent_records=1,recovery_chars=20000)
    archive=Archive(tmp_path/'a.sqlite',cfg)
    mem=await Compressor(cfg,archive).compress(rs,method,cell='c')
    assert not mem.generated and secret not in mem.text
    assert all(r.text in mem.text for r in rs if r.kind in {'goal','constraint'})
    recovered=archive.recover('c','once',handle=mem.details['handles']['receipt'],chars=20000)
    assert secret in recovered['text']
    assert mem.semantic_certified is False

@pytest.mark.asyncio
async def test_protected_oversize_still_fails_closed(tmp_path):
    cfg=CompressionConfig(memory_bytes=512);a=Archive(tmp_path/'a.sqlite',cfg)
    with pytest.raises(LabError,match='compression_capacity'):
        await Compressor(cfg,a).compress([Record(id='g',kind='goal',text='x'*1000)],'mask',cell='c')


def workspace(tmp_path,program="assert 1 == 1\n"):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path/'check.py').write_text(program)
    return VerifierSpec('check',(sys.executable,'-S','-B','check.py'),('check.py',),timeout_seconds=2)


def test_reverification_current_failure_not_historical_pass(tmp_path):
    spec=workspace(tmp_path)
    old=execute_verifier(spec,tmp_path);assert old['passed']
    (tmp_path/'check.py').write_text('assert 1 == 2\n')
    new=execute_verifier(spec,tmp_path)
    assert not new['passed'] and new['returncode']!=0 and new['status']=='completed'
    assert old['input_sha256_after']!=new['input_sha256_before']
    assert not new['decision_sufficiency_certified']

@pytest.mark.parametrize('value',[0,-1,True,float('inf'),601])
def test_revalidation_timeout_bounds(value):
    with pytest.raises(ValueError):VerifierSpec('x',('python','-V'),('x',),timeout_seconds=value)

@pytest.mark.parametrize('value',[0,-1,True,1048577])
def test_revalidation_output_bounds(value):
    with pytest.raises(ValueError):VerifierSpec('x',('python','-V'),('x',),max_output_bytes=value)

@pytest.mark.parametrize('value',['../bad','/tmp/anything','.',''])
def test_revalidation_path_reject(tmp_path,value):
    with pytest.raises(ValueError):within(tmp_path,value)


def test_revalidation_symlink_reject(tmp_path):
    (tmp_path/'a').write_text('x');(tmp_path/'b').symlink_to('a')
    with pytest.raises(ValueError):within(tmp_path,'b')


def test_revalidation_timeout_kills(tmp_path):
    spec=workspace(tmp_path,'import time; time.sleep(5)\n')
    spec=VerifierSpec(spec.id,spec.argv,spec.inputs,.05)
    r=execute_verifier(spec,tmp_path);assert r['status']=='timeout' and not r['passed']


def test_revalidation_output_limit(tmp_path):
    spec=workspace(tmp_path,'print("x"*5000)\n')
    spec=VerifierSpec(spec.id,spec.argv,spec.inputs,2,128)
    r=execute_verifier(spec,tmp_path);assert r['status']=='output_limit' and len(r['output'])==128


def test_revalidation_changed_during_command(tmp_path):
    spec=workspace(tmp_path,'from pathlib import Path; Path("state").write_text("B")\n')
    (tmp_path/'state').write_text('A');spec=VerifierSpec(spec.id,spec.argv,('check.py','state'))
    r=execute_verifier(spec,tmp_path);assert r['status']=='completed' and not r['observed_inputs_stable'] and not r['passed']


def test_revalidation_no_inherited_secrets(tmp_path,monkeypatch):
    monkeypatch.setenv('REVER_API_KEY','VERY_PRIVATE')
    spec=workspace(tmp_path,'import os; print(os.getenv("REVER_API_KEY","absent"))\n')
    assert execute_verifier(spec,tmp_path)['output'].strip()=='absent'


def test_revalidation_cancel(tmp_path):
    spec=workspace(tmp_path,'import time;time.sleep(4)\n');event=threading.Event()
    timer=threading.Timer(.05,event.set);timer.start()
    try:r=execute_verifier(spec,tmp_path,cancel_event=event)
    finally:timer.join()
    assert r['status']=='cancelled'


def test_registry_is_pinned(tmp_path):
    p=tmp_path/'registry.json';raw=json.dumps({'schema':1,'verifiers':[{'id':'ok','argv':[sys.executable,'-V'],'inputs':['check.py']}]}).encode();p.write_bytes(raw)
    assert 'ok' in load_registry(p,hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError):load_registry(p,'0'*64)
    raw=b'{"schema":1,"schema":1,"verifiers":[]}';p.write_bytes(raw)
    with pytest.raises(ValueError):load_registry(p,hashlib.sha256(raw).hexdigest())


@pytest.mark.asyncio
async def test_freeze_generation_gate_and_no_overwrite(tmp_path):
    rs=records('dummy');cfg=CompressionConfig(memory_bytes=2048,recent_records=1)
    mem=await Compressor(cfg,Archive(tmp_path/'a.sqlite',cfg)).compress(rs,'mask',cell='c')
    cp=Checkpoint(id='late',split='search',source_group='owned-delayed-query-fixture',provenance={'template':'rc3-owned-mechanism-v1'},task='Read the historic TOKEN.',records=rs,
       questions=[Question(id='id',text='What was TOKEN?',category='E')],synthetic=True)
    p=tmp_path/'parent.json'
    with pytest.raises(ValueError):freeze_intervention(p,cp,mem,parent_cost={},expected_generated=True)
    sha=freeze_intervention(p,cp,mem,parent_cost={})
    assert read_intervention(p,sha)['memory']['text']==mem.text
    with pytest.raises(FileExistsError):freeze_intervention(p,cp,mem,parent_cost={})
    p.write_text(p.read_text()+' ')
    with pytest.raises(ValueError):read_intervention(p,sha)

class ScriptedReader:
    """Programmatic test double, NOT an LLM or claimed capability result."""
    def __init__(self,ledger,mode):self.ledger=ledger;self.mode=mode;self.payloads=[]
    async def complete(self,messages,**kw):
        validate_messages(messages);self.payloads.append(messages[1].content)
        calls=[];text=''
        if self.mode=='revalidate' and messages[-1].role!='tool':calls=[{'id':'t1','name':'revalidate_evidence','arguments':{'verifier_id':'check'}}]
        elif self.mode=='recover' and messages[-1].role!='tool':calls=[{'id':'t1','name':'recover_evidence','arguments':{'query':'Audit receipt','chars':2000}}]
        else:text=json.dumps({'answers':{'id':'unknown'}})
        return Completion(text,calls,None,None,None,'scripted',None,'tool' if calls else 'stop',{})

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['none','recover','revalidate'])
async def test_arm_executes_tools_with_same_parent(tmp_path,ledger,mode):
    rs=records('secret-not-in-question');cfg=CompressionConfig(memory_bytes=2048,recent_records=1)
    mem=await Compressor(cfg,Archive(tmp_path/'a.sqlite',cfg)).compress(rs,'mask',cell='parent')
    cp=Checkpoint(id='q',split='search',source_group='owned-delayed-query-fixture',provenance={'template':'rc3-owned-mechanism-v1'},task='Audit the historical receipt.',records=rs,
        questions=[Question(id='id',text='What is its token?',category='E')],synthetic=True)
    p=tmp_path/'parent.json';sha=freeze_intervention(p,cp,mem,parent_cost={'known_tokens':0})
    spec=workspace(tmp_path/'workspace');client=ScriptedReader(ledger,mode)
    arm=Arm(mode,recovery_calls=int(mode=='recover'),revalidation_calls=int(mode=='revalidate'))
    r=await run_arm(p,sha,arm,client,tmp_path/'arm',workspace=tmp_path/'workspace',verifiers={'check':spec})
    assert r['status']=='complete' and r['memory_text_sha256']==hashlib.sha256(mem.text.encode()).hexdigest()
    assert r['tool_calls']==int(mode!='none')
    assert all('secret-not-in-question' not in s for s in client.payloads)
    assert not r['native_pi_rollout']
    with pytest.raises(FileExistsError):await run_arm(p,sha,arm,client,tmp_path/'arm',workspace=tmp_path/'workspace',verifiers={'check':spec})

@pytest.mark.parametrize('field,value',[('recovery_calls',True),('max_turns',0),('revalidation_calls',11)])
def test_arm_limits(field,value):
    with pytest.raises(ValueError):Arm('test',**{field:value})


def test_exception_signature_redacts():
    inner=OSError(104,'VERY_PRIVATE');outer=httpx.ReadError('Bearer VERY_PRIVATE');outer.__cause__=inner
    v=exception_signature(outer);assert 'VERY_PRIVATE' not in json.dumps(v) and v[-1]['errno']==104
    inner.__cause__=outer;assert len(exception_signature(outer))==2

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['responses','chat_completions'])
@pytest.mark.parametrize('phase',['headers','body'])
async def test_real_socket_read_timeout_attribution(provider,ledger,monkeypatch,protocol,phase):
    seen=0;tasks=set()
    async def handler(reader,writer):
        nonlocal seen;seen+=1;task=asyncio.current_task();tasks.add(task)
        try:
            headers=await reader.readuntil(b'\r\n\r\n')
            size=int(next(x.split(b':',1)[1].strip() for x in headers.split(b'\r\n') if x.lower().startswith(b'content-length:')))
            await reader.readexactly(size)
            if phase=='body':writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\nContent-Type: application/json\r\n\r\n');await writer.drain()
            await asyncio.sleep(.15)
        finally:writer.close();await writer.wait_closed();tasks.discard(task)
    server=await asyncio.start_server(handler,'127.0.0.1',0)
    monkeypatch.setenv('REVER_API_KEY','dummy-not-a-provider-credential')
    p=provider.model_copy(update={'protocol':protocol,'mock':False,'base_url':f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', 'read_seconds':.03})
    try:
        async with APIClient(p,ledger) as client:
            with pytest.raises(LabError,match='transport_ambiguous'):
                await client.complete([Message('user','local-only')],op='one',cell='c')
            with pytest.raises(LabError):await client.complete([Message('user','local-only')],op='one',cell='c')
        assert seen==1 and ledger.totals()['unknown_attempts']==1
        with ledger.db() as db:events=[json.loads(row[0]) for row in db.execute('select event from audit')]
        e=next(v for v in events if v.get('type')=='transport_trace_v1')
        assert any(v['type']=='ReadTimeout' for v in e['exception_chain'])
        assert e['phase']==('await_headers' if phase=='headers' else 'read_body')
        assert e['http_status']==(None if phase=='headers' else 200)
    finally:
        server.close();await server.wait_closed()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_nonstream_json_whitespace_heartbeat_on_real_socket(provider,ledger,monkeypatch,protocol):
    """A transport compatibility witness, not a deployed CLIProxyAPI fix."""
    seen=0;tasks=set()
    async def handler(reader,writer):
        nonlocal seen
        task=asyncio.current_task();tasks.add(task);seen+=1
        try:
            headers=await reader.readuntil(b'\r\n\r\n')
            size=int(next(x.split(b':',1)[1].strip() for x in headers.split(b'\r\n') if x.lower().startswith(b'content-length:')))
            await reader.readexactly(size)
            if protocol=='chat_completions':
                obj={'id':'local','model':provider.model,'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],
                     'usage':{'prompt_tokens':11,'completion_tokens':7}}
            else:
                obj={'id':'local','model':provider.model,'status':'completed','output':[{'id':'m','type':'message','role':'assistant','content':[{'type':'output_text','text':'ok'}]}],
                     'usage':{'input_tokens':11,'output_tokens':7}}
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n');await writer.drain()
            for _ in range(10):
                writer.write(b'1\r\n\n\r\n');await writer.drain();await asyncio.sleep(.02)
            body=json.dumps(obj).encode();writer.write(f'{len(body):x}\r\n'.encode()+body+b'\r\n0\r\n\r\n');await writer.drain()
        finally:writer.close();await writer.wait_closed();tasks.discard(task)
    server=await asyncio.start_server(handler,'127.0.0.1',0)
    monkeypatch.setenv('REVER_API_KEY','local-test-placeholder')
    p=provider.model_copy(update={'mock':False,'protocol':protocol,'stream':False,'read_seconds':.1,
       'base_url':f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'})
    try:
        async with APIClient(p,ledger) as c:
            result=await c.complete([Message('user','local heartbeat')],op='heartbeat',cell='local')
        assert result.text=='ok' and seen==1
        assert ledger.totals()['known_tokens']==18 and ledger.totals()['unknown_attempts']==0
    finally:
        server.close();await server.wait_closed()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

@pytest.mark.parametrize('obj',[{'schema':True,'verifiers':[]},[],{'schema':1,'verifiers':[{'id':'x','argv':['x'],'inputs':[None]}]}])
def test_registry_rejects_wrong_shapes(tmp_path,obj):
    raw=json.dumps(obj).encode();p=tmp_path/'registry.json';p.write_bytes(raw)
    with pytest.raises(ValueError):load_registry(p,hashlib.sha256(raw).hexdigest())
