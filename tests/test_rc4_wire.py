"""Real loopback HTTP tests, NOT the Harbor egress path and NOT paid models."""
import asyncio
import contextlib
import json
import socket
import threading
import time
import httpx
import pytest
import uvicorn
from reverpi.config import StudyConfig, CompressionConfig
from reverpi.gateway import create_app
from reverpi.memory import Compressor
from test_transport import raw

@contextlib.contextmanager
def server(app):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    srv=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:srv.run(sockets=[sock]),daemon=True);thread.start()
    deadline=time.monotonic()+5
    while not srv.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
    if not srv.started:raise RuntimeError('Local test server did not start')
    try:yield f'http://127.0.0.1:{port}'
    finally:
        srv.should_exit=True;thread.join(6);sock.close()
        assert not thread.is_alive(), 'Test server leaked a thread'

@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint',['complete','compact'])
@pytest.mark.parametrize('heartbeat',[0.0,0.05])
async def test_headers_arrive_before_completion_and_body_is_exact(tmp_path,provider,monkeypatch,endpoint,heartbeat):
    release=threading.Event();started=threading.Event();finished=threading.Event()
    async def wait():
        started.set()
        async with asyncio.timeout(5):
            while not release.is_set():await asyncio.sleep(.005)
        finished.set()
    async def supplier(_):
        await wait();return httpx.Response(200,json=raw(provider,'wire-ok'))
    original=Compressor.compress
    async def slow_compact(self,*args,**kw):
        await wait();return await original(self,*args,**kw)
    monkeypatch.setattr(Compressor,'compress',slow_compact)
    app=create_app(provider,StudyConfig(methods=['mask'],early_response_headers=True,
        response_heartbeat_seconds=heartbeat,compression=CompressionConfig()),tmp_path/'g',transport=httpx.MockTransport(supplier))
    token=app.state.sessions.create('one','mask')
    body=({'op':'a','messages':[{'role':'user','content':'Hi'}]} if endpoint=='complete' else
          {'op':'a','expected_revision':0,'records':[{'id':'g','kind':'goal','text':'Keep goal'}]})
    with server(app) as url:
        async with httpx.AsyncClient(timeout=2,trust_env=False) as c:
            async with c.stream('POST',url+'/'+endpoint,headers={'Authorization':'Bearer '+token},json=body) as r:
                assert r.status_code==200 and not finished.is_set()
                chunks=r.aiter_bytes();prefix=b''
                if heartbeat:
                    prefix=await anext(chunks)
                    assert prefix.isspace() and not finished.is_set()
                release.set()
                collected=prefix+b''.join([part async for part in chunks])
                obj=json.loads(collected)
                assert obj.get('text')=='wire-ok' if endpoint=='complete' else obj['revision']==1
                assert finished.is_set()

@pytest.mark.asyncio
async def test_disconnect_cancels_dispatched_work_without_rebilling(tmp_path,provider):
    started=threading.Event();cancelled=threading.Event()
    async def supplier(_):
        started.set()
        try:await asyncio.sleep(30)
        except asyncio.CancelledError:cancelled.set();raise
    app=create_app(provider,StudyConfig(methods=['mask'],early_response_headers=True,response_heartbeat_seconds=.05),
        tmp_path/'g',transport=httpx.MockTransport(supplier))
    token=app.state.sessions.create('one','mask')
    with server(app) as url:
        async with httpx.AsyncClient(timeout=2,trust_env=False) as c:
            async with c.stream('POST',url+'/complete',headers={'Authorization':'Bearer '+token},
                               json={'op':'a','messages':[{'role':'user','content':'Hi'}]}) as r:
                assert (await anext(r.aiter_bytes())).isspace() and started.is_set()
            deadline=time.monotonic()+3
            while not cancelled.is_set() and time.monotonic()<deadline:await asyncio.sleep(.02)
            assert cancelled.is_set()
    totals=app.state.ledger.totals()
    assert totals['attempts']==1 and totals['unknown_attempts']==1
