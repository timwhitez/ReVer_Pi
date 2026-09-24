"""Actual OS process supervision with explicit fake Pi/Harbor programs.

These tests do NOT install, import or certify upstream Pi or Harbor. They exercise
our launcher/controller subprocess, timeout, resume and artifact contracts.
"""
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from reverpi import launcher, benchmark
from reverpi.config import StudyConfig
from reverpi.errors import LabError
from reverpi.ledger import Ledger
from reverpi.util import canonical, digest, source_manifest
from reverpi.devtasks import generate_tasks


def launcher_setup(tmp_path, monkeypatch, script):
    root=tmp_path/'code';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('x=1')
    work=tmp_path/'workspace';work.mkdir();prompt=tmp_path/'prompt';prompt.write_text('owned smoke task')
    key=tmp_path/'token';key.write_text('mock-scoped-token')
    monkeypatch.setattr(launcher,'pi_paths',lambda _: ('TEST-DOUBLE',Path('NOT-PI'),'test-double','test-double'))
    # Stdlib-only stand-ins exclude host site initialization from the 1s supervision test.
    monkeypatch.setattr(launcher,'command',lambda *args: [sys.executable,'-S','-c',script])
    info={'revision':0,'accounting':{'attempts':0},'effort':'low','max_output_tokens':1024,
          'context_window':8192,'method':'mask','model':'fake-model','mock':True}
    actual=httpx.AsyncClient
    monkeypatch.setattr(launcher.httpx,'AsyncClient',lambda **kw:actual(**kw,transport=httpx.MockTransport(lambda _:httpx.Response(200,json=info))))
    return root,work,prompt,tmp_path/'out','http://127.0.0.1:9876',key


@pytest.mark.asyncio
@pytest.mark.parametrize('events,exitcode,expected',[
    ([{'type':'agent_end'}],0,'completed'),
    ([{'type':'message_end','message':{'stopReason':'length'}},{'type':'agent_end'}],0,'execution_failed'),
    ([{'type':'extension_error'},{'type':'agent_end'}],0,'execution_failed'),
    ([{'type':'agent_end'}],1,'execution_failed'),
    ([],0,'execution_failed'),
])
async def test_fake_pi_real_process_outcomes(tmp_path,monkeypatch,events,exitcode,expected):
    script='import json,sys\n'
    script+='\n'.join('print('+repr(json.dumps(e))+',flush=True)' for e in events)
    script+=f'\nsys.exit({exitcode})'
    args=launcher_setup(tmp_path,monkeypatch,script)
    r=await launcher.launch(*args,wall_seconds=3,acknowledge_unsandboxed=True)
    assert r['status']==expected and r['mock_provider'] is True
    settings=json.loads((args[3]/'agent-config/settings.json').read_text())
    assert settings['compaction']['reserveTokens']==2048  # actual session window, not 131072
    with pytest.raises(LabError,match='replayed'):
        await launcher.launch(*args,wall_seconds=3,acknowledge_unsandboxed=True)


@pytest.mark.asyncio
async def test_launcher_kills_surviving_child_after_leader_exit(tmp_path,monkeypatch):
    # The parent exits immediately, while the descendant keeps the pipes open
    # and ignores TERM. A leader-only cleanup check leaves it running in v1.
    pidpath=tmp_path/'child.pid'
    child='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    script=f'import subprocess,sys,pathlib,time\np=subprocess.Popen([sys.executable,"-S","-c",{child!r}])\npathlib.Path({str(pidpath)!r}).write_text(str(p.pid))\ntime.sleep(.15)'
    args=launcher_setup(tmp_path,monkeypatch,script)
    childpid=None
    try:
        r=await launcher.launch(*args,wall_seconds=1,acknowledge_unsandboxed=True)
        childpid=int(pidpath.read_text())
        assert r['status']=='timeout'
        await asyncio.sleep(.1)
        stat=Path(f'/proc/{childpid}/stat')
        assert not stat.exists() or stat.read_text().split()[2]=='Z', 'orphan tool process was left running'
    finally:
        if childpid is None and pidpath.exists():childpid=int(pidpath.read_text())
        if childpid:
            try:os.kill(childpid,signal.SIGKILL)
            except ProcessLookupError:pass


def matrix_setup(tmp_path,monkeypatch,provider,*,method_mismatch=False):
    root=tmp_path/'code';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('x=1')
    generate_tasks(tmp_path/'tasks')
    manifest=tmp_path/'tasks/tasks.jsonl';manifest.write_text(manifest.read_text().splitlines()[0]+'\n')
    worker=tmp_path/'fake-worker';worker.write_bytes(b'NOT A REAL WORKER; SUBPROCESS TEST ONLY')
    from reverpi.util import bytes_digest
    Path(str(worker)+'.json').write_text(canonical({'archive_sha256':bytes_digest(worker.read_bytes()),'source_sha':digest(source_manifest(root))}))
    cfg=StudyConfig(methods=['mask','rever_lite']);matrix=benchmark.MatrixConfig(methods=cfg.methods)
    pp=tmp_path/'plan.json';benchmark.make_plan(root,manifest,matrix,provider,cfg,pp,worker_path=worker)
    gateway=tmp_path/'gateway';ledger=Ledger(gateway/'ledger.sqlite',cfg.budget)
    ledger.bind('gateway_code',digest(source_manifest(root)));ledger.bind('gateway_study',cfg.model_dump());ledger.bind('provider:'+provider.name,provider.model_dump())
    script=tmp_path/'fake_harbor.py'
    script.write_text('''import sys,json,pathlib
if '--help' in sys.argv:
 print('--agent --jobs-dir --max-retries');sys.exit(0)
a=sys.argv
cell=a[a.index('--job-name')+1];directory=pathlib.Path(a[a.index('--jobs-dir')+1])/cell/'trial'
kwargs=dict(s.split('=',1) for i,s in enumerate(a) if i and a[i-1]=='--ak')
'''+("kwargs['method']='WRONG'\n" if method_mismatch else '')+'''
directory.mkdir(parents=True)
result={'trial_name':'fake','task_name':'fake','task_checksum':'fake','config':{'agent':{'kwargs':kwargs}},'verifier_result':{'rewards':{'reward':1}},'exception_info':None}
(directory/'result.json').write_text(json.dumps(result))
''')
    # Redirect only the selected executable while exercising the real command builder.
    monkeypatch.setattr(benchmark.shutil,'which',lambda _:sys.executable)
    monkeypatch.setattr(benchmark.importlib.metadata,'version',lambda _:'0.22.0')
    actual_run=benchmark.subprocess.run
    monkeypatch.setattr(benchmark.subprocess,'run',lambda argv,**kw:actual_run([sys.executable,str(script),*argv[1:]],**kw))
    actual_command=benchmark.harbor_command
    monkeypatch.setattr(benchmark,'harbor_command',lambda *args,**kw:[sys.executable,str(script),*actual_command(*args,**kw)[1:]])
    return root,pp,gateway,'http://restricted-gateway.invalid',worker,tmp_path/'run'


@pytest.mark.asyncio
async def test_fake_harbor_real_process_matrix_and_no_replay(tmp_path,monkeypatch,provider):
    args=matrix_setup(tmp_path,monkeypatch,provider)
    r=await benchmark.run_matrix(*args);assert r['finished']==2
    rows=json.loads((args[-1]/'results.json').read_text());assert all(x['success'] for x in rows)
    mtimes=[Path(x['official_result_path']).stat().st_mtime_ns for x in rows]
    await benchmark.run_matrix(*args)
    assert mtimes==[Path(x['official_result_path']).stat().st_mtime_ns for x in rows]
    benchmark.collect_matrix(args[-1],args[2])
    assert all(x['row_sha256'] for x in json.loads((args[-1]/'results.json').read_text()))


@pytest.mark.asyncio
async def test_wrong_method_stops_matrix_without_scoring_remaining_cells(tmp_path,monkeypatch,provider):
    args=matrix_setup(tmp_path,monkeypatch,provider,method_mismatch=True)
    await benchmark.run_matrix(*args)
    rows=json.loads((args[-1]/'results.json').read_text())
    assert rows[0]['status']=='infrastructure_error' and rows[0]['success'] is None
    assert rows[1]['status']=='pending' and rows[1]['success'] is None
    with pytest.raises(LabError,match='prior trial'):await benchmark.run_matrix(*args)


@pytest.mark.asyncio
async def test_existing_native_manifest_never_silently_overwritten(tmp_path,monkeypatch,provider):
    args=matrix_setup(tmp_path,monkeypatch,provider)
    args[-1].mkdir();p=args[-1]/'manifest.json';p.write_text('{"previous":"evidence"}')
    before=p.read_bytes()
    with pytest.raises(LabError,match='manifest'):
        await benchmark.run_matrix(*args)
    assert p.read_bytes()==before
