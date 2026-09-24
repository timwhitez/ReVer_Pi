import asyncio
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import httpx
import pytest
from reverpi.config import Provider,StudyConfig,Budget
from reverpi.protocols import Message
from reverpi.transport import APIClient,consume_sse
from reverpi.ledger import Ledger
from reverpi.errors import LabError
from reverpi.selection import select_development
from reverpi.gateway import create_app
from reverpi.memory import Archive
from reverpi.util import canonical
from test_transport import raw

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
@pytest.mark.parametrize('sse',[False,True])
async def test_real_loopback_http_both_protocols_and_sse(tmp_path,protocol,sse):
    bodies=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));bodies.append((self.path,body))
            response=raw(p,text='network-ok')
            if sse:
                if protocol=='responses':events=[{'type':'response.completed','response':response}]
                else:events=[{'id':'r1','model':'mock-reasoner','choices':[{'index':0,'delta':{'content':'network-ok'},'finish_reason':'stop'}],'usage':response['usage']}]
                b=(''.join('data: '+canonical(e)+'\n\n' for e in events)+'data: [DONE]\n\n').encode()
            else:b=canonical(response).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if sse else 'application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        p=Provider(mock=True,model='mock-reasoner',protocol=protocol,stream=sse,base_url=f'http://127.0.0.1:{server.server_port}/v1')
        ledger=Ledger(tmp_path/'ledger',Budget())
        # Supplying a REAL transport bypasses the deterministic mock even though
        # the no-secret profile is marked mock. This test never leaves loopback.
        async with APIClient(p,ledger,transport=httpx.AsyncHTTPTransport()) as client:
            result=await client.complete([Message('user','x')],op='op',cell='c')
            assert result.text=='network-ok'
            assert (await client.complete([Message('user','x')],op='op',cell='c')).text=='network-ok'
        assert len(bodies)==1 and bodies[0][0]==('/v1/responses' if protocol=='responses' else '/v1/chat/completions')
        assert ledger.totals()['known_tokens']==18
    finally:server.shutdown();server.server_close();thread.join(timeout=3)

@pytest.mark.asyncio
async def test_tls_failure_never_disables_verify_or_retries(provider,ledger):
    calls=0
    def handle(request):
        nonlocal calls;calls+=1
        try:raise ssl.SSLCertVerificationError('certificate failed')
        except ssl.SSLCertVerificationError as e:raise httpx.ConnectError('TLS',request=request) from e
    async with APIClient(provider,ledger,transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(LabError) as e:await c.complete([Message('user','x')],op='tls',cell='c')
    assert e.value.kind=='tls_configuration' and calls==1 and ledger.totals()['known_tokens']==0

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_sse_data_after_terminal_rejected(provider,ledger,protocol):
    p=provider.model_copy(update={'protocol':protocol,'stream':True})
    if protocol=='responses':es=[{'type':'response.completed','response':raw(p)},{'type':'response.output_text.delta','delta':'bad'}]
    else:es=[{'choices':[{'index':0,'delta':{'content':'one'},'finish_reason':'stop'}]}, {'choices':[{'index':0,'delta':{'content':'two'},'finish_reason':None}]}]
    data=''.join('data: '+canonical(e)+'\n\n' for e in es)+'data: [DONE]\n\n'
    async with APIClient(p,ledger,transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'Content-Type':'text/event-stream'},content=data.encode()))) as c:
        with pytest.raises(LabError,match='terminal'):await c.complete([Message('user','x')],op='o',cell='c')
    assert ledger.totals()['unknown_attempts']==1

@pytest.mark.parametrize('mapping',['OFF',' None ','Disabled',''])
def test_reasoning_only_mapping_case_whitespace(mapping):
    with pytest.raises(ValueError):Provider(effort_map={'low':mapping})

@pytest.mark.asyncio
async def test_original_pi_not_subject_to_unneeded_archive_quota(provider,tmp_path):
    study=StudyConfig(methods=['pi_original'])
    app=create_app(provider,study,tmp_path/'run');token=app.state.sessions.create('orig','pi_original')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post('/complete',headers={'Authorization':'Bearer '+token},json={'op':'a','messages':[{'role':'user','content':'x'*2000}]})
        assert r.status_code==200
    assert Archive(tmp_path/'run/archive.sqlite',study.compression).used('orig')==0
    await app.state.client.close()

def write_selection(root,split='dev'):
    root.mkdir();(root/'manifest.json').write_text(canonical({'study':{'split':split,'methods':['full','mask']},'mock':True,'track':'diagnostic'}))
    rows=[]
    for task in ['a','b']:
        for method,n in [('full',100),('mask',50)]:rows.append({'task_id':task,'source_group':task,'method':method,'repeat':0,'status':'complete','success':True,'cost':{'known_tokens':n,'accounted_tokens':n,'unknown_attempts':0}})
    (root/'results.json').write_text(canonical(rows));return rows

@pytest.mark.parametrize('split',['gate','external'])
def test_selection_cannot_use_sealed_feedback(tmp_path,split):
    write_selection(tmp_path/'run',split)
    with pytest.raises(LabError,match='fed back'):select_development(tmp_path/'run',tmp_path/'out','full')

def test_development_screen_not_noninferiority_certificate(tmp_path):
    rows=write_selection(tmp_path/'run')
    r=select_development(tmp_path/'run',tmp_path/'out','full')
    assert r['pareto_development_candidates']==['mask'] and not r['proves_noninferiority']
    rows[-1]['cost']['unknown_attempts']=1;(tmp_path/'run/results.json').write_text(canonical(rows))
    r=select_development(tmp_path/'run',tmp_path/'out2','full');assert not r['pareto_development_candidates']
