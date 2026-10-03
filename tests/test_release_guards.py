import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
import pytest
from reverpi.config import StudyConfig
from reverpi.data import make_fixtures,freeze
from reverpi.gateway import create_app
from reverpi.util import canonical,digest,bytes_digest


def test_fixture_outputs_cannot_be_overwritten(tmp_path):
    make_fixtures(tmp_path)
    with pytest.raises(ValueError,match='already exist'):make_fixtures(tmp_path)


def test_freeze_outputs_cannot_be_overwritten(tmp_path):
    p=tmp_path/'f';p.write_text('prior')
    with pytest.raises(ValueError,match='Freeze exists'):freeze(tmp_path,p,p,p,[p],p)
    assert p.read_text()=='prior'


def test_unknown_gateway_method_fails_before_creation(tmp_path,provider):
    with pytest.raises(ValueError,match='Unknown'):create_app(provider,StudyConfig(methods=['invented']),tmp_path/'run')
    assert not (tmp_path/'run').exists()


@pytest.fixture
def harbor_double(monkeypatch):
    # Explicit contract doubles, NOT a substitute for upstream type/API acceptance.
    names=['harbor','harbor.agents','harbor.agents.base','harbor.environments','harbor.environments.base','harbor.models','harbor.models.agent','harbor.models.agent.context']
    for n in names:monkeypatch.setitem(sys.modules,n,types.ModuleType(n))
    class Base:
        def __init__(self,*args,logs_dir=None,**kwargs):self.logs_dir=Path(logs_dir)
    sys.modules['harbor.agents.base'].BaseAgent=Base
    sys.modules['harbor.environments.base'].BaseEnvironment=object
    sys.modules['harbor.models.agent.context'].AgentContext=SimpleNamespace
    old=sys.modules.pop('reverpi.harbor_agent',None)
    mod=importlib.import_module('reverpi.harbor_agent')
    yield mod.ReVerPiAgent
    sys.modules.pop('reverpi.harbor_agent',None)
    if old is not None:sys.modules['reverpi.harbor_agent']=old


@pytest.mark.asyncio
async def test_harbor_adapter_setup_run_and_revoke_with_explicit_doubles(tmp_path,provider,harbor_double):
    app=create_app(provider,StudyConfig(methods=['mask']),tmp_path/'gateway')
    worker=tmp_path/'worker.tgz';worker.write_bytes(b'not-a-real-tar: contract-double-only')
    from reverpi.util import source_manifest
    root=Path(__file__).resolve().parents[1]
    Path(str(worker)+'.json').write_text(canonical({'archive_sha256':bytes_digest(worker.read_bytes()),'source_sha':digest(source_manifest(root))}))
    class Env:
        def __init__(self):self.uploads=[];self.commands=[];self.envs=[];self.contents={}
        async def upload_file(self,a,b):self.uploads.append((a,b));self.contents[b]=Path(a).read_bytes()
        async def download_dir(self,a,b):return None
        async def exec(self,command,env=None,timeout_sec=None):
            self.commands.append(command);self.envs.append(env)
            return SimpleNamespace(return_code=0,stdout='v22.19.0',stderr='')
    e=Env();context=SimpleNamespace();agent=harbor_double(logs_dir=tmp_path/'logs',gateway_run=str(tmp_path/'gateway'),gateway_url='http://gateway:8765',worker_archive=str(worker),method='mask',cell_id='contract-cell')
    # Exercise shell quoting in the consumer without claiming a real container.
    agent.runtime="/tmp/synthetic runtime with 'quote"
    await agent.setup(e)
    instruction="\t\nFix the owned 中文😀 task.\r\n@literal --not-an-option  "
    await agent.run(instruction,e,context)
    assert context.cost_usd is None
    runtime=json.loads((tmp_path/'logs/reverpi_runtime.json').read_text())
    assert runtime['status']=='process_exited' and runtime['official_reward'] is None
    assert any(x and 'REVER_SESSION_TOKEN' in x and 'REVER_API_KEY' not in x for x in e.envs)
    assert all('REVER_API_KEY' not in x for x in e.envs if x)
    import shlex
    assert e.contents[agent.runtime+'/prompt.txt'] == instruction.encode('utf-8')
    assert json.loads(e.contents[agent.runtime+'/prompt-input.json']) == {'schema':1,'prompt':instruction}
    run=e.commands[-1]
    assert '<'+shlex.quote(agent.runtime+'/prompt-input.json') in run
    assert ' --print ' in run and '@'+agent.runtime not in run and instruction not in run
    assert e.envs[-1]['REVER_PROMPT_TRANSPORT']=='json_v1'
    with pytest.raises(RuntimeError,match='replayed'):await agent.run('x',e,context)
    await app.state.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('instruction', ['', 'x'*1000001], ids=['empty', 'over-limit'])
async def test_harbor_invalid_instruction_revokes_without_replay(tmp_path, provider, harbor_double, instruction):
    from reverpi.util import source_manifest
    from reverpi.errors import LabError
    app=create_app(provider,StudyConfig(methods=['mask']),tmp_path/'gateway')
    worker=tmp_path/'worker.tgz';worker.write_bytes(b'contract-double-only')
    root=Path(__file__).resolve().parents[1]
    Path(str(worker)+'.json').write_text(canonical({'archive_sha256':bytes_digest(worker.read_bytes()),'source_sha':digest(source_manifest(root))}))
    class Env:
        executed=False
        async def upload_file(self,a,b):pass
        async def exec(self,command,env=None,timeout_sec=None):
            if env:self.executed=True
            return SimpleNamespace(return_code=0,stdout='v22',stderr='')
        async def download_dir(self,a,b):return None
    env=Env();agent=harbor_double(logs_dir=tmp_path/'logs',gateway_run=str(tmp_path/'gateway'),
                                 gateway_url='http://gateway:8765',worker_archive=str(worker),method='mask')
    try:
        await agent.setup(env)
        with pytest.raises(ValueError,match='prompt empty or too large'):
            await agent.run(instruction,env,SimpleNamespace())
        assert agent.started and not env.executed and app.state.ledger.totals()['attempts']==0
        with pytest.raises(LabError):agent.sessions.auth(agent.token)
        with pytest.raises(RuntimeError,match='replayed'):await agent.run('valid retry',env,SimpleNamespace())
        assert json.loads((tmp_path/'logs/reverpi_runtime.json').read_text())['status']=='interrupted_or_failed'
    finally:await app.state.client.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('execution_error,collection_error',[('timeout',None),('timeout','io'),('timeout','hang'),('cancel',None),(None,'io'),(None,'cancel')])
async def test_harbor_collects_partial_logs_without_changing_execution(tmp_path,provider,harbor_double,execution_error,collection_error):
    import asyncio
    from reverpi.util import source_manifest
    app=create_app(provider,StudyConfig(methods=['mask']),tmp_path/'gateway')
    worker=tmp_path/'worker.tgz';worker.write_bytes(b'contract-double-only')
    Path(str(worker)+'.json').write_text(canonical({'archive_sha256':bytes_digest(worker.read_bytes()),'source_sha':digest(source_manifest(Path(__file__).resolve().parents[1]))}))
    original=TimeoutError('original-run-timeout') if execution_error=='timeout' else asyncio.CancelledError() if execution_error=='cancel' else None
    class Env:
        downloads=0
        async def upload_file(self,a,b):pass
        async def exec(self,command,env=None,timeout_sec=None):
            if env and original is not None:raise original
            return SimpleNamespace(return_code=0,stdout='v22',stderr='')
        async def download_dir(self,a,b):
            # Log transport must never extend the session's new-request authority.
            from reverpi.errors import LabError
            with pytest.raises(LabError,match="Invalid or expired"):
                agent.sessions.auth(agent.token)
            self.downloads+=1
            if collection_error=='io':raise OSError('logs unavailable')
            if collection_error=='hang':await asyncio.sleep(100)
            if collection_error=='cancel':raise asyncio.CancelledError()
            Path(b).mkdir(parents=True,exist_ok=True);(Path(b)/'session.jsonl').write_text('partial')
    env=Env();agent=harbor_double(logs_dir=tmp_path/'logs',gateway_run=str(tmp_path/'gateway'),gateway_url='http://gateway:8765',worker_archive=str(worker),method='mask',cell_id='partial-cell')
    agent.LOG_COLLECTION_TIMEOUT_SECONDS=.02
    try:
        await agent.setup(env)
        setup=json.loads((tmp_path/'logs/reverpi_setup.json').read_text())
        assert setup['pi_settings']['compaction']['enabled'] is True
        if original is not None:
            with pytest.raises(type(original)) as err:await agent.run('owned instruction',env,SimpleNamespace())
            assert err.value is original
        elif collection_error=='cancel':
            with pytest.raises(asyncio.CancelledError):await agent.run('owned instruction',env,SimpleNamespace())
        else:await agent.run('owned instruction',env,SimpleNamespace())
        record=json.loads((tmp_path/'logs/reverpi_runtime.json').read_text())
        assert env.downloads==1 and record['official_reward'] is None
        assert record['log_collection']['status']==('failed' if collection_error else 'download_returned')
        assert record['status']==('interrupted_or_failed' if original else 'process_exited')
    finally:await app.state.client.close()
