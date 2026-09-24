"""Offline fault witnesses for reviewed fixes. No remote provider or paid calls."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import httpx
import pytest

from reverpi.config import Budget
from reverpi.util import atomic_create, canonical, contained_regular_file, digest, bytes_digest, source_manifest
from reverpi.lean_review import prepare_review, run_review
from reverpi.lean_export import export_run
from reverpi.intervention_matrix import execute_matrix, tree_identity
from reverpi.ledger import Ledger
from test_rc4_matrix import prep
from test_transport import raw

ROOT=Path(__file__).resolve().parents[1]

def script(name):
    sys.path.insert(0,str(ROOT/'scripts'))
    spec=importlib.util.spec_from_file_location('rc53_'+name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_atomic_create_is_private_exclusive_and_complete(tmp_path):
    p=tmp_path/'certificate.json';atomic_create(p,'{"ok":true}')
    assert p.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):atomic_create(p,'changed')
    assert p.read_text()=='{"ok":true}' and not list(tmp_path.glob('.pending*'))


def test_atomic_create_failure_has_no_partial_final(tmp_path,monkeypatch):
    import reverpi.util as util
    def fail(*a,**k):raise OSError('injected fsync failure')
    monkeypatch.setattr(util.os,'fsync',fail)
    with pytest.raises(OSError):atomic_create(tmp_path/'new','secret')
    assert not (tmp_path/'new').exists() and not list(tmp_path.glob('.pending*'))


@pytest.mark.parametrize('name',['../outside','./x','x//y','x/../y','C:/x','x\\y','','/absolute','x/'])
def test_contained_path_rejects_noncanonical_names(tmp_path,name):
    with pytest.raises(ValueError):contained_regular_file(tmp_path,name)


def test_contained_path_rejects_parent_symlink(tmp_path):
    outside=tmp_path/'outside';outside.mkdir();(outside/'data').write_text('external')
    root=tmp_path/'root';root.mkdir();(root/'link').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):contained_regular_file(root,'link/data')


@pytest.mark.parametrize('kind',['fifo','parent_symlink','windows_drive'])
def test_release_special_paths_reject_without_blocking(tmp_path,kind):
    mod=script('verify_release');name='a'
    if kind=='fifo':os.mkfifo(tmp_path/'a')
    elif kind=='parent_symlink':
        ext=tmp_path/'outside';ext.mkdir();(ext/'a').write_text('x')
        (tmp_path/'link').symlink_to(ext,target_is_directory=True);name='link/a'
    else:name='C:/a'
    (tmp_path/'SHA256SUMS.json').write_text(canonical({'files':{name:'0'*64}}))
    assert not mod.verify(tmp_path)['all_original_files_match']


def test_release_safeopen_resists_parent_replaced_by_symlink(tmp_path,monkeypatch):
    mod=script('verify_release');(tmp_path/'safe').mkdir();(tmp_path/'safe/file').write_text('trusted')
    outside=tmp_path/'outside';outside.mkdir();(outside/'file').write_text('external')
    (tmp_path/'SHA256SUMS.json').write_text(canonical({'files':{'safe/file':bytes_digest(b'trusted')}}))
    original=mod.regular_reader
    def swap(fd,name):
        if name=='safe/file':
            (tmp_path/'safe').rename(tmp_path/'old');(tmp_path/'safe').symlink_to(outside,target_is_directory=True)
        return original(fd,name)
    monkeypatch.setattr(mod,'regular_reader',swap)
    assert not mod.verify(tmp_path)['all_original_files_match']


def test_audit_requires_manifested_sidecars_before_copy(tmp_path):
    mod=script('audit_rc5_campaign');root=tmp_path/'in';root.mkdir()
    src=root/'ledger.sqlite';src.write_bytes(b'data');Path(str(src)+'-wal').write_bytes(b'wal')
    with pytest.raises(ValueError):mod.copy_sqlite_family(src,tmp_path/'out',root=root,manifest={'ledger.sqlite':bytes_digest(b'data')})
    assert not (tmp_path/'out').exists()


def test_audit_integrity_recheck_survives_python_optimization(tmp_path):
    root=tmp_path/'in';root.mkdir();(root/'x').write_bytes(b'changed')
    manifest={'x':bytes_digest(b'original')};(root/'MANIFEST_SHA256.json').write_text(canonical(manifest))
    code="""import sys;from pathlib import Path
sys.path.insert(0,sys.argv[1]);from audit_rc5_campaign import verify_manifest
from reverpi.util import strict_json_loads,bytes_digest
r=Path(sys.argv[2]);p=r/'MANIFEST_SHA256.json'
verify_manifest(r,strict_json_loads(p.read_bytes()),bytes_digest(p.read_bytes()))
"""
    proc=subprocess.run([sys.executable,'-O','-c',code,str(ROOT/'scripts'),str(root)],capture_output=True,text=True,timeout=10)
    assert proc.returncode!=0 and 'ValueError' in proc.stderr and 'verification' in proc.stderr.lower()


@pytest.mark.parametrize('value',['garbage','[]','{}'])
def test_network_malformed_client_becomes_failed_rows(tmp_path,monkeypatch,value):
    mod=script('network_fixture')
    monkeypatch.setattr(mod.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,stdout=value,stderr=''))
    result=mod.run_local(tmp_path/'out','unused-node',[0])
    assert len(result['rows'])==2 and not result['all_complete']
    assert all(not r['ok'] for r in result['rows'])


def test_network_bad_delay_is_argparse_failure(tmp_path,monkeypatch):
    mod=script('network_fixture');monkeypatch.setattr(sys,'argv',['network_fixture','local','--out',str(tmp_path/'out'),'--delays','bad'])
    with pytest.raises(SystemExit) as e:mod.main()
    assert e.value.code==2 and not (tmp_path/'out').exists()


def test_network_client_schema_and_identity():
    mod=script('network_fixture')
    for obj in [{'id':'x','ok':1},{'id':'y','ok':True},{'id':'x','ok':True,'status':200,'phase':'read_body'}]:
        with pytest.raises(ValueError):mod.validate_client_result(obj,'x')


@pytest.mark.parametrize('key',['AWS_ACCESS_KEY_ID','AZURE_CLIENT_ID','GITHUB_AUTH','SOME_KEY','GOOGLE_APPLICATION_CREDENTIALS'])
def test_cloud_credentials_not_inherited(tmp_path,monkeypatch,key):
    mod=script('verify_offline');monkeypatch.setenv(key,'do-not-inherit')
    assert key not in mod.clean_environment(ROOT,tmp_path,tmp_path/'guard',tmp_path/'log')


@pytest.mark.parametrize('folder',['configs','runs','reports','artifacts'])
def test_offline_output_anywhere_in_source_rejected(folder):
    mod=script('verify_offline')
    with pytest.raises(ValueError):mod.validate_destination(ROOT,ROOT/folder/'new-check')


def test_steps_cleanup_descendant_after_parent_exit(tmp_path):
    mod=script('verify_offline');(tmp_path/'logs').mkdir();marker=tmp_path/'orphan_wrote'
    child=f'import time;from pathlib import Path;time.sleep(1);Path({str(marker)!r}).write_text("bad")'
    parent='import subprocess,sys;subprocess.Popen([sys.executable,"-c",'+repr(child)+'])'
    steps=mod.Steps(tmp_path,dict(os.environ))
    row=steps.run('child',[sys.executable,'-c',parent],cwd=tmp_path,timeout=5)
    import time;time.sleep(1.2)
    assert row['status']=='passed' and not marker.exists()


def test_analysis_empty_json_is_not_success(tmp_path,monkeypatch):
    mod=script('verify_offline')
    monkeypatch.setattr('reverpi.results.read_results',lambda p:[{'status':'complete'}]*144)
    (tmp_path/'chat-analysis.json').write_text('{}')
    with pytest.raises(ValueError):mod.verify_mock_artifacts(tmp_path)


@pytest.mark.asyncio
async def test_export_preserves_frozen_cache_lock_and_database(tmp_path,provider):
    root,h=await prep(tmp_path,provider,repeats=1)
    (root/'template/__pycache__').mkdir();(root/'template/__pycache__/example.pyc').write_bytes(b'opaque cache')
    (root/'template/frozen.lock').write_bytes(b'locked version')
    with sqlite3.connect(root/'template/input.db') as c:c.execute('create table source(x)')
    plan=json.loads((root/'plan.json').read_text());plan['workspace_files']=tree_identity(root/'template')
    (root/'plan.json').write_text(canonical(plan));h=digest(plan)
    await execute_matrix(root,h,acknowledge_unsandboxed=True)
    before=tree_identity(root/'template');out=tmp_path/'matrix.zip';export_run(root,out)
    with zipfile.ZipFile(out) as z:
        for name,sha in before.items():assert bytes_digest(z.read('matrix/template/'+name))==sha
        manifest=json.loads(z.read('matrix/EXPORT_MANIFEST.json'))
        assert 'ledger.sqlite' in manifest['sqlite_backup_methods']
        assert 'template/input.db' not in manifest['sqlite_backup_methods']


@pytest.mark.asyncio
async def test_export_unit_traversal_rejected_before_read(tmp_path,provider):
    root,h=await prep(tmp_path,provider,repeats=1);Ledger(root/'ledger.sqlite',Budget())
    plan=json.loads((root/'plan.json').read_text());plan['units'][0]['unit']='../outside'
    (root/'plan.json').write_text(canonical(plan))
    with pytest.raises(ValueError):export_run(root,tmp_path/'bad.zip')
    assert not (tmp_path/'bad.zip').exists()


@pytest.mark.asyncio
async def test_export_detects_late_mutation_no_published_zip(tmp_path,provider,monkeypatch):
    import reverpi.lean_export as le
    root,h=await prep(tmp_path,provider,repeats=1);await execute_matrix(root,h,acknowledge_unsandboxed=True)
    real=le.inventory;calls=0
    def changed(p):
        nonlocal calls;calls+=1
        if calls==3:(root/'late.txt').write_text('drift')
        return real(p)
    monkeypatch.setattr(le,'inventory',changed)
    with pytest.raises(ValueError,match='changed'):le.export_run(root,tmp_path/'bad.zip')
    assert not (tmp_path/'bad.zip').exists() and not list(tmp_path.glob('.pending-export-*'))


@pytest.mark.asyncio
async def test_export_records_missing_units_without_claiming_complete(tmp_path,provider):
    root,h=await prep(tmp_path,provider,repeats=1);Ledger(root/'ledger.sqlite',Budget())
    out=tmp_path/'incomplete.zip';r=export_run(root,out)
    assert not r['all_planned_units_committed']
    with zipfile.ZipFile(out) as z:
        m=json.loads(z.read('matrix/EXPORT_MANIFEST.json'))
        assert set(m['unit_states'].values())=={'not_started'}


@pytest.mark.parametrize('value',[[],{},None,17])
def test_scope_malformed_file_is_explicit_error(tmp_path,provider,value):
    code=tmp_path/'code';(code/'src').mkdir(parents=True);(code/'src/a.py').write_text('x=1\n')
    flash=provider.model_copy(update={'model':'deepseek-flash','concurrency':1})
    with pytest.raises(ValueError):prepare_review(code,tmp_path/'review',flash,Budget(),[{'file':value}])


@pytest.mark.asyncio
async def test_review_schema_rejects_before_creating_ledger(tmp_path,provider):
    code=tmp_path/'code';(code/'src').mkdir(parents=True);(code/'src/a.py').write_text('x=1\n')
    flash=provider.model_copy(update={'model':'deepseek-flash','concurrency':1});out=tmp_path/'review'
    prepare_review(code,out,flash,Budget(),[{'file':'src/a.py'}])
    plan=json.loads((out/'plan.json').read_text());plan['jobs'][0]['rows']=None
    (out/'plan.json').write_text(canonical(plan))
    with pytest.raises(ValueError):await run_review(code,out,digest(plan))
    assert not (out/'ledger.sqlite').exists()


@pytest.mark.asyncio
async def test_review_operational_valueerror_halts_without_repair(tmp_path,provider,monkeypatch):
    import reverpi.lean_review as lr
    code=tmp_path/'code';(code/'src').mkdir(parents=True);(code/'src/a.py').write_text('x=1\n')
    flash=provider.model_copy(update={'model':'deepseek-flash','concurrency':1});out=tmp_path/'review'
    p=prepare_review(code,out,flash,Budget(),[{'file':'src/a.py'}],lenses=lr.LENSES)
    calls=0
    async def fail(*a,**k):
        nonlocal calls;calls+=1;raise ValueError('local transport failure')
    monkeypatch.setattr(lr.APIClient,'complete',fail)
    report=await run_review(code,out,p['plan_sha256'])
    assert calls==1 and report['halt_reason']=='internal_error' and report['rows'][0]['error']=='internal_error'


@pytest.mark.asyncio
async def test_review_snapshot_drift_cannot_pass_scope(tmp_path,provider,monkeypatch):
    import reverpi.lean_review as lr
    code=tmp_path/'code';(code/'src').mkdir(parents=True);(code/'src/a.py').write_text('x=1\n')
    flash=provider.model_copy(update={'model':'deepseek-flash','concurrency':1});out=tmp_path/'review'
    plan=prepare_review(code,out,flash,Budget(),[{'file':'src/a.py'}]);initial=source_manifest(code);count=0
    def snapshot(_):
        nonlocal count;count+=1
        return initial if count<=3 else {'src/a.py':'changed'}
    monkeypatch.setattr(lr,'source_manifest',snapshot)
    report=await run_review(code,out,plan['plan_sha256'])
    assert report['all_outputs_valid'] and not report['source_unchanged'] and not report['all_scoped_checks_pass']


def test_budget_cli_requires_independent_plan_identity(tmp_path):
    proc=subprocess.run([sys.executable,str(ROOT/'scripts/flash_workflow.py'),'budget-screen','--dir',str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert proc.returncode!=0 and '--plan-sha256' in proc.stderr


def test_sign_test_is_two_sided_not_reported_point625():
    mod=script('audit_official_window');p=mod.exact_sign(2,2)
    assert p['two_sided']==1.0 and p['one_sided_candidate_greater']==0.6875
    assert mod.exact_sign(0,0)['two_sided']==1.0


@pytest.mark.parametrize('counts',[(-1,0),(True,1),(1.5,1)])
def test_sign_rejects_invalid_counts(counts):
    with pytest.raises(ValueError):script('audit_official_window').exact_sign(*counts)


def test_latest_window_immutable_audit_and_timeout_reward_distinction():
    if not (ROOT/'provenance/official_window_20260920').is_dir():
        pytest.skip("sealed official-window provenance is not distributed (research artifact)")
    r=script('audit_official_window').audit(ROOT/'provenance/official_window_20260920')
    assert r['result_row_checks_passed']==48 and r['paired']['both_success']==12
    assert r['exceptions_with_successful_reward']==3 and len(r['exception_rows'])==10
    assert r['ledger_costs']['total_supplied_ledgers']=={'observed_tokens':13993033,'unknown_reserved_tokens':1391581,'accounted_tokens':15384614,'usd':None}
    assert r['review']['findings']==93 and not r['formal_g4_certified']
    assert r['limits']['custom_compaction_activation'].startswith('UNKNOWN')


def test_row_validation_rejects_duplicate_and_checksum_but_preserves_reward():
    m=script('audit_official_window');grid=[{'cell':'x','method':'mask'}]
    row={'cell':'x','method':'mask','success':True,'execution_success':False,'status':'complete'};row['row_sha256']=digest(row)
    assert m.validate_rows([row],grid)['x']['success']
    with pytest.raises(ValueError):m.validate_rows([row,row],grid)
    row['success']=False
    with pytest.raises(ValueError):m.validate_rows([row],grid)

@pytest.mark.asyncio
async def test_review_finalization_error_does_not_mask_cancellation(tmp_path,provider,monkeypatch):
    import asyncio
    import reverpi.lean_review as lr
    code=tmp_path/'code';(code/'src').mkdir(parents=True);(code/'src/a.py').write_text('x=1\n')
    flash=provider.model_copy(update={'model':'deepseek-flash','concurrency':1});out=tmp_path/'review'
    p=prepare_review(code,out,flash,Budget(),[{'file':'src/a.py'}]);real=lr.atomic_write
    async def cancel(*a,**k):raise asyncio.CancelledError()
    def bad_snapshot(path,*a,**k):
        if path.name=='report.json':raise OSError('injected disk failure')
        return real(path,*a,**k)
    monkeypatch.setattr(lr.APIClient,'complete',cancel);monkeypatch.setattr(lr,'atomic_write',bad_snapshot)
    with pytest.raises(asyncio.CancelledError):await run_review(code,out,p['plan_sha256'])
    note=json.loads((out/'finalization_failure.json').read_text())
    assert note['original_error']=='CancelledError' and note['finalization_error']=='OSError'
