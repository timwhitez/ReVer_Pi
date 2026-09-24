"""Regression witnesses for local, zero-dispatch and release verification contracts."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from reverpi.preflight import validate_provider_environment
from reverpi.errors import LabError

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('value', [' leading', 'trailing ', '   ', '\tvalue', 'value\t', ' both '])
def test_header_whitespace_is_rejected_before_dispatch(provider, monkeypatch, value):
    p = provider.model_copy(update={'mock': False, 'api_key_env':'REVER_UNIT_KEY',
        'extra_headers_env':{'X-Project':'REVER_UNIT_HEADER'}})
    monkeypatch.setenv('REVER_UNIT_KEY','not-a-live-key')
    monkeypatch.setenv('REVER_UNIT_HEADER',value)
    with pytest.raises(LabError) as exc:
        validate_provider_environment(p)
    assert exc.value.kind == 'configuration_preflight'
    assert exc.value.message == 'Missing or invalid header environment variable: REVER_UNIT_HEADER'
    assert not exc.value.ambiguous


def test_internal_header_spaces_remain_valid(provider, monkeypatch):
    p = provider.model_copy(update={'mock':False,'api_key_env':'REVER_UNIT_KEY',
        'extra_headers_env':{'X-Project':'REVER_UNIT_HEADER'}})
    monkeypatch.setenv('REVER_UNIT_KEY','not-a-live-key')
    monkeypatch.setenv('REVER_UNIT_HEADER','alpha beta')
    assert validate_provider_environment(p)['paid_calls'] == 0


def _release_tree(tmp_path):
    (tmp_path/'scripts').mkdir()
    shutil.copyfile(ROOT/'scripts/verify_release.py',tmp_path/'scripts/verify_release.py')
    (tmp_path/'payload.txt').write_text('new release')
    (tmp_path/'RELEASE_MANIFEST.json').write_text(json.dumps({'files':{'payload.txt':'0'*64}}))
    manifest={'files':{'payload.txt':hashlib.sha256(b'new release').hexdigest()}}
    (tmp_path/'SHA256SUMS.json').write_text(json.dumps(manifest))
    return manifest


def _verify(tmp_path, *extra):
    return subprocess.run([sys.executable,str(tmp_path/'scripts/verify_release.py'), *extra],
        cwd=tmp_path,capture_output=True,text=True,timeout=10)


def test_release_verifier_prefers_current_manifest(tmp_path):
    _release_tree(tmp_path)
    result=_verify(tmp_path)
    assert result.returncode==0, result.stdout+result.stderr
    assert json.loads(result.stdout)['manifest']=='SHA256SUMS.json'


def test_release_verifier_detects_current_tampering(tmp_path):
    _release_tree(tmp_path);(tmp_path/'payload.txt').write_text('changed')
    result=_verify(tmp_path)
    assert result.returncode != 0


@pytest.mark.parametrize('rel',['../escape','/tmp/escape','a\\b','./payload.txt','.'])
def test_release_verifier_refuses_unsafe_or_noncanonical_paths(tmp_path,rel):
    _release_tree(tmp_path)
    (tmp_path/'SHA256SUMS.json').write_text(json.dumps({'files':{rel:'0'*64}}))
    result=_verify(tmp_path)
    assert result.returncode != 0


def test_release_verifier_refuses_duplicate_json_keys(tmp_path):
    _release_tree(tmp_path)
    h=hashlib.sha256(b'new release').hexdigest()
    (tmp_path/'SHA256SUMS.json').write_text('{"files":{"payload.txt":"'+'0'*64+'","payload.txt":"'+h+'"}}')
    assert _verify(tmp_path).returncode != 0


def test_release_verifier_does_not_follow_symlink(tmp_path):
    _release_tree(tmp_path)
    (tmp_path/'payload.txt').rename(tmp_path/'actual.txt')
    (tmp_path/'payload.txt').symlink_to('actual.txt')
    assert _verify(tmp_path).returncode != 0


def test_release_verifier_legacy_fallback_is_explicit(tmp_path):
    manifest=_release_tree(tmp_path)
    (tmp_path/'SHA256SUMS.json').unlink()
    (tmp_path/'RELEASE_MANIFEST.json').write_text(json.dumps(manifest))
    assert _verify(tmp_path).returncode != 0
    result=_verify(tmp_path, '--allow-legacy')
    assert result.returncode==0
    assert json.loads(result.stdout)['manifest']=='RELEASE_MANIFEST.json'


def _import_script(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_network_fixture_failure_produces_nonzero_exit(monkeypatch,tmp_path):
    mod=_import_script('network_fixture')
    monkeypatch.setattr(mod,'run_local',lambda *args:{'all_complete':False})
    monkeypatch.setattr(sys,'argv',['network_fixture.py','local','--out',str(tmp_path/'run'),'--delays','0'])
    assert mod.main()==1


def test_network_fixture_success_produces_zero_exit(monkeypatch,tmp_path):
    mod=_import_script('network_fixture')
    monkeypatch.setattr(mod,'run_local',lambda *args:{'all_complete':True})
    monkeypatch.setattr(sys,'argv',['network_fixture.py','local','--out',str(tmp_path/'run'),'--delays','0'])
    assert mod.main()==0


def test_static_audit_recovers_hot_journal_only_on_copy(tmp_path):
    import sqlite3
    mod=_import_script('audit_rc5_campaign')
    source=tmp_path/'source.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA page_size=1024')
        db.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, value INTEGER, pad BLOB)')
        db.executemany('INSERT INTO t VALUES (?,0,?)',[(i,b'x'*1000) for i in range(100)])
        db.commit()
    code="""import sqlite3,os,sys
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA cache_size=2');c.execute('PRAGMA synchronous=FULL')
c.execute('BEGIN IMMEDIATE');c.execute('UPDATE t SET value=1');os._exit(0)
"""
    subprocess.run([sys.executable,'-c',code,str(source)],check=True,timeout=15)
    journal=Path(str(source)+'-journal')
    assert journal.stat().st_size>512
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,journal)}
    # The unsafe old copying scheme sees uncommitted pages.
    bare=tmp_path/'bare.sqlite';shutil.copyfile(source,bare)
    with sqlite3.connect(bare) as db:
        assert db.execute('SELECT SUM(value) FROM t').fetchone()[0]>0
    target=tmp_path/'safe.sqlite';mod.copy_sqlite_family(source,target)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT SUM(value) FROM t').fetchone()[0]==0
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,journal)}


def test_static_audit_copy_never_overwrites(tmp_path):
    mod=_import_script('audit_rc5_campaign')
    src=tmp_path/'input.sqlite';src.write_bytes(b'data')
    dst=tmp_path/'output.sqlite';dst.write_bytes(b'preserve')
    with pytest.raises(FileExistsError):mod.copy_sqlite_family(src,dst)
    assert dst.read_bytes()==b'preserve'


def test_static_audit_rejects_symlinked_sidecar(tmp_path):
    mod=_import_script('audit_rc5_campaign')
    src=tmp_path/'input.sqlite';src.write_bytes(b'data')
    other=tmp_path/'other';other.write_bytes(b'static')
    Path(str(src)+'-journal').symlink_to(other)
    with pytest.raises(ValueError):mod.copy_sqlite_family(src,tmp_path/'copy.sqlite')


@pytest.mark.parametrize('key',['OPENAI_API_KEY','REVER_API_KEY','TOKEN','MY_SECRET','PASSWORD',
    'PYTHONOPTIMIZE','PYTHONINSPECT','PYTHONSTARTUP','AWS_SHARED_CREDENTIALS_FILE','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','REVER_GATEWAY_URL','PI_AUTH_FILE'])
def test_offline_driver_scrubs_credentials_and_routes(tmp_path,monkeypatch,key):
    mod=_import_script('verify_offline')
    monkeypatch.setenv(key,'do-not-propagate')
    env=mod.clean_environment(ROOT,tmp_path/'home',tmp_path/'guard.so',tmp_path/'guard.log')
    assert env.get(key) != 'do-not-propagate'
    assert env['HOME']==str(tmp_path/'home') and env['LD_PRELOAD']==str(tmp_path/'guard.so')


@pytest.mark.parametrize('folder',['src','tests','pi','scripts','provenance','data'])
def test_offline_output_cannot_overwrite_evidence_or_source(tmp_path,folder):
    mod=_import_script('verify_offline')
    with pytest.raises(ValueError):mod.validate_destination(ROOT,ROOT/folder/'must-not-create')
    assert mod.validate_destination(ROOT,tmp_path/'new')==tmp_path/'new'


def test_offline_output_never_reuses_directory(tmp_path):
    mod=_import_script('verify_offline')
    with pytest.raises(ValueError):mod.validate_destination(ROOT,tmp_path)


def test_offline_step_records_expected_and_unexpected_failures(tmp_path):
    mod=_import_script('verify_offline');(tmp_path/'logs').mkdir()
    steps=mod.Steps(tmp_path,dict(os.environ))
    expected=steps.run('expected',[sys.executable,'-c','raise SystemExit(2)'],cwd=tmp_path,expected=(2,))
    unexpected=steps.run('unexpected',[sys.executable,'-c','raise SystemExit(2)'],cwd=tmp_path)
    assert expected['status']=='passed' and unexpected['status']=='failed'
    progress=json.loads((tmp_path/'progress.json').read_text())
    assert len(progress)==2 and all('log_sha256' in row for row in progress)


def test_offline_step_timeout_is_failure(tmp_path):
    mod=_import_script('verify_offline');(tmp_path/'logs').mkdir()
    steps=mod.Steps(tmp_path,dict(os.environ))
    row=steps.run('timeout',[sys.executable,'-c','import time;time.sleep(20)'],cwd=tmp_path,timeout=.1)
    assert row['status']=='failed' and row['returncode']==124 and row['timed_out']
    with pytest.raises(ProcessLookupError):os.kill(row['pid'],0)


def test_offline_mock_stage_cannot_pass_with_only_zero_exit(tmp_path):
    mod=_import_script('verify_offline')
    with pytest.raises(FileNotFoundError):mod.verify_mock_artifacts(tmp_path)


@pytest.mark.parametrize('count,status',[(143,'complete'),(144,'pending'),(145,'complete')])
def test_offline_mock_stage_checks_denominator_and_completion(tmp_path,monkeypatch,count,status):
    mod=_import_script('verify_offline')
    monkeypatch.setattr('reverpi.results.read_results',lambda _:[{'status':status} for i in range(count)])
    for protocol in ('chat','responses'):(tmp_path/f'{protocol}-analysis.json').write_text('{}')
    with pytest.raises(ValueError):mod.verify_mock_artifacts(tmp_path)


def test_offline_mock_stage_checks_both_protocols(tmp_path,monkeypatch):
    mod=_import_script('verify_offline')
    monkeypatch.setattr('reverpi.results.read_results',lambda _:[{'status':'complete'} for i in range(144)])
    for protocol in ('chat','responses'):
        (tmp_path/f'{protocol}-analysis.json').write_text(json.dumps({'comparisons':[
            {'baseline':'full','paired_tasks':12} for _ in range(11)]}))
        (tmp_path/f'{protocol}-probe').mkdir()
        (tmp_path/f'{protocol}-probe/probe.json').write_text(json.dumps({'mock':True,
            'protocol':'chat_completions' if protocol=='chat' else 'responses',
            'compatible_for_this_probe':True,'checks':{'function_call':True,
                'nonempty_complete_response':True,'tool_and_reasoning_replay':True}}))
    result=mod.verify_mock_artifacts(tmp_path)
    assert set(result)=={'chat','responses'}
    assert all(x['rows']==144 for x in result.values())


def _public_receipt(expected):
    text=('96 passed, 1 skipped in 0.1s\n' if expected else
        'FAILED tests/test_api.py::test__dict - AssertionError\n'
        'FAILED tests/test_api.py::test_dict_decoder - AssertionError\n'
        '2 failed, 19 passed, 2 deselected in 0.1s\n')
    return {'status':'completed','observed_inputs_stable':True,'output_truncated':False,
        'passed':expected,'returncode':0 if expected else 1,'output':text,
        'output_sha256':hashlib.sha256(text.encode()).hexdigest()}


@pytest.mark.parametrize('expected',[True,False])
def test_public_receipt_requires_actual_expected_tests(expected):
    _import_script('offline_fixture_checks').validate_public_receipt(_public_receipt(expected),expected)


@pytest.mark.parametrize('broken',['missing_pytest','timeout','truncated','changed_input','extra_failure','bad_hash'])
def test_public_negative_control_is_not_any_failure(broken):
    mod=_import_script('offline_fixture_checks');r=_public_receipt(False)
    if broken=='missing_pytest':r['output']='python3: No module named pytest\n'
    elif broken=='timeout':r['status']='timeout'
    elif broken=='truncated':r['output_truncated']=True
    elif broken=='changed_input':r['observed_inputs_stable']=False
    elif broken=='extra_failure':r['output']+='FAILED tests/test_api.py::other - AssertionError\n'
    r['output_sha256']=hashlib.sha256(r['output'].encode()).hexdigest()
    if broken=='bad_hash':r['output_sha256']='0'*64
    with pytest.raises(ValueError):mod.validate_public_receipt(r,False)
