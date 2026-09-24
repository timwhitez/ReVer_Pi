"""Offline regression tests. Mock responses never certify a research release."""
import asyncio
import json
from pathlib import Path
import pytest
from reverpi.config import Budget, CompressionConfig, Provider
from reverpi.errors import LabError
from reverpi.ledger import Ledger
from reverpi.review import review_jobs, review_plan, run_reviews, review_messages
from reverpi.util import digest, canonical, source_manifest, bytes_digest
from reverpi.bundle_audit import audit_delivery, contained, session_evidence, audit_review
from reverpi.research_audit import readonly_db
from reverpi.canary_recovery import select_probe, verify_recovery
from reverpi.memory import Archive
from reverpi.acceptance import check_acceptance


def setup_review(tmp_path, provider, policy='halt', *, freeze_policy=True):
    root=tmp_path/'source'; (root/'src').mkdir(parents=True)
    (root/'src/testcase.py').write_text('x = 1\n')
    out=tmp_path/'review'
    budget=Budget(max_total_tokens=100000,per_cell_tokens=100000)
    ledger=Ledger(out/'ledger.sqlite',budget)
    manifest,jobs=review_jobs(root,2)
    plan=review_plan(manifest,jobs,provider,policy)
    if freeze_policy: ledger.bind('review_schedule_policy',policy)
    op=plan['jobs'][0]['op']
    ledger.claim(op,plan['jobs'][0]['payload_sha'],op)
    attempt=ledger.reserve(op,op,provider.name,1000,0)
    return root,out,budget,ledger,op,attempt


@pytest.mark.parametrize('metadata',['x.egg-info','x.dist-info'])
def test_source_hash_ignores_generated_install_metadata(tmp_path,metadata):
    (tmp_path/'src/pkg').mkdir(parents=True)
    p=tmp_path/'src/pkg/x.py';p.write_text('x = 1\n')
    before=source_manifest(tmp_path)
    (tmp_path/'src'/metadata).mkdir();(tmp_path/'src'/metadata/'METADATA').write_text('volatile build')
    assert source_manifest(tmp_path)==before
    p.write_text('x = 2\n');assert source_manifest(tmp_path)!=before


@pytest.mark.asyncio
@pytest.mark.parametrize('policy,visited,attempts',[('halt',1,1),('quarantine_and_continue',2,2)])
async def test_frozen_quarantine_never_resends_ambiguous_op(tmp_path,provider,policy,visited,attempts):
    root,out,budget,ledger,op,attempt=setup_review(tmp_path,provider,policy)
    report=await run_reviews(root,provider,budget,out,rounds=2,in_doubt_policy=policy)
    assert report['attempted_jobs']==visited and not report['all_jobs_valid']
    assert not report['eligible_for_human_acceptance']
    assert report['cost']['attempts']==attempts
    with readonly_db(out/'ledger.sqlite') as db:
        assert db.execute('SELECT state FROM operations WHERE op=?',(op,)).fetchone()[0]=='running'
        a=dict(db.execute('SELECT * FROM attempts WHERE id=?',(attempt,)).fetchone())
    assert a['reserve_tokens']==1000 and a['actual_tokens'] is None and a['state']=='reserved'
    progress=json.loads((out/'review_progress.json').read_text())
    assert progress['state']==('halted' if policy=='halt' else 'finished')
    assert json.loads((out/'review_plan.json').read_text())['in_doubt_policy']==policy
    again=await run_reviews(root,provider,budget,out,rounds=2,in_doubt_policy=policy)
    assert again['cost']['attempts']==attempts
    assert again['cache_replays']==(1 if policy=='quarantine_and_continue' else 0)


@pytest.mark.asyncio
async def test_existing_policy_cannot_change(tmp_path,provider):
    root,out,budget,_,_,_=setup_review(tmp_path,provider)
    with pytest.raises(LabError,match='Frozen'):
        await run_reviews(root,provider,budget,out,rounds=2,in_doubt_policy='quarantine_and_continue')


@pytest.mark.asyncio
async def test_legacy_generation_cannot_retrofit_quarantine(tmp_path,provider):
    root,out,budget,_,_,_=setup_review(tmp_path,provider,freeze_policy=False)
    with pytest.raises(LabError,match='retrofit'):
        await run_reviews(root,provider,budget,out,rounds=2,in_doubt_policy='quarantine_and_continue')


@pytest.mark.asyncio
async def test_cancellation_writes_terminal_progress_and_manifest(tmp_path,provider,monkeypatch):
    import reverpi.review as mod
    root=tmp_path/'code';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('x = 1\n')
    async def cancelled(*a,**kw):raise asyncio.CancelledError()
    monkeypatch.setattr(mod.APIClient,'complete',cancelled)
    with pytest.raises(asyncio.CancelledError):
        await run_reviews(root,provider,Budget(),tmp_path/'review',rounds=2)
    report=json.loads((tmp_path/'review/review_manifest.json').read_text())
    progress=json.loads((tmp_path/'review/review_progress.json').read_text())
    assert report['halt_reason']=='cancelled' and report['attempted_jobs']==1
    assert report['unvisited_jobs']==1 and progress['state']=='halted'
    assert not report['eligible_for_human_acceptance']


@pytest.mark.asyncio
async def test_unknown_schedule_rejected_before_ledger(tmp_path,provider):
    root=tmp_path/'code';root.mkdir()
    with pytest.raises(ValueError):
        await run_reviews(root,provider,Budget(),tmp_path/'r',in_doubt_policy='retry')
    assert not (tmp_path/'r').exists()


def test_delivery_verifies_but_does_not_authenticate(tmp_path):
    p=tmp_path/'x';p.write_text('evidence')
    manifest={'file_count':1,'files':{'x':bytes_digest(p.read_bytes())}}
    (tmp_path/'MANIFEST_DELIVERY.json').write_text(canonical(manifest))
    report=audit_delivery(tmp_path)
    assert report['all_listed_hashes_match'] and not report['authenticity_proven']
    p.write_text('changed')
    assert audit_delivery(tmp_path)['changed']==['x']


@pytest.mark.parametrize('name',['../escape','/tmp/escape',''])
def test_evidence_paths_cannot_escape(tmp_path,name):
    with pytest.raises(ValueError):contained(tmp_path,name)


def test_symlink_evidence_is_rejected(tmp_path):
    (tmp_path/'target').write_text('x');(tmp_path/'alias').symlink_to(tmp_path/'target')
    with pytest.raises(ValueError):contained(tmp_path,'alias')


def test_missing_sqlite_is_not_created(tmp_path):
    p=tmp_path/'missing.sqlite'
    with pytest.raises((ValueError,FileNotFoundError)):
        with readonly_db(p):pass
    assert not p.exists()


def test_unobserved_logs_are_unknown_not_zero(tmp_path):
    evidence=session_evidence(tmp_path/'missing')
    assert evidence['available'] is False and evidence['compactions'] is None
    assert evidence['transport_errors'] is None


def test_session_parsing_counts_actual_compaction_and_recovery(tmp_path):
    p=tmp_path/'log';p.write_text('\n'.join(map(canonical,[
        {'type':'compaction'},
        {'message':{'role':'assistant','content':[{'type':'toolCall','name':'recover_evidence'}]}},
        {'message':{'role':'assistant','stopReason':'error','errorMessage':'fetch failed: other side closed'}}])))
    r=session_evidence(p)
    assert (r['compactions'],r['recovery_tool_calls'],r['transport_errors'])==(1,1,1)
    assert r['recovery_correctness_verified'] is False


def make_probe(tmp_path):
    a=Archive(tmp_path/'archive.sqlite',CompressionConfig())
    nonce='secret-123';content='αβγ\nsecret_nonce='+nonce+'\n'+'noise'*100
    handle=a.put('canary',content)
    p=select_probe(a.path,'canary',nonce,100)
    assert p.handle==handle and nonce in p.expected and nonce not in p.prompt()
    response={'handle':p.handle,'start':p.start,'end':p.start+len(p.expected),
              'total_chars':p.total_chars,'text':p.expected}
    event={'type':'tool_execution_end','toolName':'recover_evidence','isError':False,
           'result':{'content':[{'type':'text','text':canonical(response)}]}}
    return p,event


def test_actual_exact_recovery_is_verified(tmp_path):
    p,e=make_probe(tmp_path);r=verify_recovery([e],p)
    assert r['recovery_verified'] and r['recovery_exact_results']==1
    assert not r['autonomous_retrieval_choice_verified']
    assert p.expected not in canonical(r)


@pytest.mark.parametrize('mutation',['wrong_handle','wrong_text','error','boolean_offset','wrong_total','not_json','self_report'])
def test_recovery_rejects_fabricated_or_incorrect_receipts(tmp_path,mutation):
    p,e=make_probe(tmp_path)
    obj=json.loads(e['result']['content'][0]['text'])
    if mutation=='wrong_handle':obj['handle']='0'*64
    elif mutation=='wrong_text':obj['text']='invented'
    elif mutation=='error':e['isError']=True
    elif mutation=='boolean_offset':obj['start']=False
    elif mutation=='wrong_total':obj['total_chars']+=1
    elif mutation=='self_report':e={'type':'message_end','message':{'role':'assistant','content':p.expected}}
    if 'result' in e:e['result']['content'][0]['text']='not JSON' if mutation=='not_json' else canonical(obj)
    with pytest.raises(LabError,match='No successful'):verify_recovery([e],p)


def test_recovery_cannot_cross_archive_namespace(tmp_path):
    a=Archive(tmp_path/'archive.sqlite',CompressionConfig());a.put('other','secret')
    with pytest.raises(LabError,match='did not archive'):select_probe(a.path,'mine','secret',100)


def test_recovery_does_not_accept_corrupt_archive(tmp_path):
    a=Archive(tmp_path/'archive.sqlite',CompressionConfig());h=a.put('mine','secret')
    with a.connect() as db:db.execute('UPDATE blobs SET content=? WHERE handle=?',('secret forged',h))
    with pytest.raises(LabError,match='identity'):select_probe(a.path,'mine','secret',100)


@pytest.mark.parametrize('limit',[True,0,3])
def test_recovery_limit_is_enforced(tmp_path,limit):
    with pytest.raises(ValueError):select_probe(tmp_path/'missing','mine','secret',limit)
    assert not (tmp_path/'missing').exists()


def test_formal_custom_method_gate_requires_real_recovery():
    report={'mock':False,'eligible_for_native_gate':True,'source_sha':'code','provider_sha':'p',
            'methods':[{'method':'mask','accepted':True}]}
    check_acceptance(report,'code','p',['mask'])
    with pytest.raises(LabError,match='exact recovery'):
        check_acceptance(report,'code','p',['mask'],require_recovery_methods=['mask'])
    report['methods'][0].update(recovery_verified=True,recovery_exact_results=1)
    check_acceptance(report,'code','p',['mask'],require_recovery_methods=['mask'])


@pytest.mark.asyncio
async def test_review_auditor_preserves_frozen_numeric_profile_identity(tmp_path,provider):
    root,out,budget,_,_,_=setup_review(tmp_path,provider)
    await run_reviews(root,provider,budget,out,rounds=2)
    before=bytes_digest((out/'ledger.sqlite').read_bytes())
    report=audit_review(root,out)
    assert report['terminal_job']['payload_sha_matches'] is True
    assert report['frozen_provider_sha_matches_report'] is True
    assert bytes_digest((out/'ledger.sqlite').read_bytes())==before
    assert report['terminal_job']['automatic_resend_authorized'] is False
    assert not report['eligible_for_human_acceptance']


def native_fixture(tmp_path):
    from reverpi.bundle_audit import audit_native_run
    g={'cell':'cell_a','method':'mask','task_id':'owned-x','source_group':'owned','repeat':0,'task_sha':'task'}
    (tmp_path/'manifest.json').write_text(canonical({'grid':[g]}))
    p=tmp_path/'jobs/cell_a/trial/result.json';p.parent.mkdir(parents=True)
    p.write_text(canonical({'verifier_result':{'rewards':{'reward':1}}}))
    row={**g,'official_result_sha':bytes_digest(p.read_bytes()),'official_reward':1,
         'reward_key':'reward','success':True}
    row['row_sha256']=digest(row)
    (tmp_path/'results.json').write_text(canonical([row]))
    return row,audit_native_run


def test_native_audit_no_session_means_unknown(tmp_path):
    _,audit=native_fixture(tmp_path);r=audit(tmp_path)
    assert r['summary']['all_result_identities_match']
    assert r['summary']['unobserved_sessions']==1 and r['cells'][0]['compactions'] is None
    assert r['network_root_cause_proven'] is False


def test_native_audit_rechecks_frozen_grid_not_only_row_hash(tmp_path):
    row,audit=native_fixture(tmp_path);row['method']='rever_lite'
    row['row_sha256']=digest({k:v for k,v in row.items() if k!='row_sha256'})
    (tmp_path/'results.json').write_text(canonical([row]))
    assert not audit(tmp_path)['summary']['all_result_identities_match']


def test_native_audit_detects_changed_official_result(tmp_path):
    _,audit=native_fixture(tmp_path)
    (tmp_path/'jobs/cell_a/trial/result.json').write_text('{}')
    assert not audit(tmp_path)['summary']['all_result_identities_match']


def test_native_audit_rejects_duplicate_cells(tmp_path):
    row,audit=native_fixture(tmp_path)
    (tmp_path/'results.json').write_text(canonical([row,row]))
    with pytest.raises(ValueError,match='Duplicate'):audit(tmp_path)


def test_acceptance_audit_missing_log_does_not_count_recovery(tmp_path):
    from reverpi.bundle_audit import audit_acceptance
    (tmp_path/'native_acceptance.json').write_text(canonical({'methods':[{'method':'mask','accepted':True}]}))
    r=audit_acceptance(tmp_path)
    assert r['all_custom_methods_invoked_recovery'] is False
    assert r['methods'][0]['recovery_tool_calls'] is None


def test_immutable_snapshot_audit_does_not_create_sidecars(tmp_path):
    from reverpi.research_audit import audit_ledger
    p=tmp_path/'ledger.sqlite';Ledger(p,Budget())
    before={x.name:bytes_digest(x.read_bytes()) for x in tmp_path.iterdir()}
    audit_ledger(p,immutable_snapshot=True)
    assert before=={x.name:bytes_digest(x.read_bytes()) for x in tmp_path.iterdir()}


def test_immutable_snapshot_refuses_to_ignore_nonempty_wal(tmp_path):
    from reverpi.research_audit import audit_ledger
    p=tmp_path/'ledger.sqlite';Ledger(p,Budget());Path(str(p)+'-wal').write_bytes(b'pending data')
    with pytest.raises(ValueError,match='checkpointed'):audit_ledger(p,immutable_snapshot=True)


@pytest.mark.asyncio
async def test_abrupt_process_exit_keeps_recoverable_plan_and_never_redispatches(tmp_path,provider):
    """Child exits without finally, reproducing the claim/reserve crash window."""
    import subprocess, sys, os
    # Normalize once in both processes; do not accidentally change 10 to 10.0
    # in the frozen provider fingerprint merely through a serialization round-trip.
    p=Provider.model_validate(provider.model_dump())
    root=tmp_path/'code';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('x = 1\n')
    out=tmp_path/'review';budget=Budget(max_total_tokens=100000,per_cell_tokens=100000)
    cfg=tmp_path/'p.json';cfg.write_text(canonical(p.model_dump()))
    child=r'''
import asyncio,json,os,sys
from pathlib import Path
from reverpi.config import Provider,Budget
from reverpi.review import run_reviews
from reverpi.transport import APIClient
from reverpi.protocols import build_request
from reverpi.util import digest
p=Provider.model_validate(json.loads(Path(sys.argv[3]).read_text()))
async def interrupted(self,messages,*,op,cell,**kwargs):
    path,body=build_request(self.p,messages,None)
    sha=digest({'profile':self.fingerprint,'path':path,'body':body})
    self.ledger.claim(op,sha,cell)
    self.ledger.reserve(op,cell,p.name,1000,0)
    os._exit(73)
APIClient.complete=interrupted
asyncio.run(run_reviews(Path(sys.argv[1]),p,Budget(max_total_tokens=100000,per_cell_tokens=100000),
    Path(sys.argv[2]),rounds=2,in_doubt_policy='quarantine_and_continue'))
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    cp=subprocess.run([sys.executable,'-c',child,str(root),str(out),str(cfg)],env=env,capture_output=True,timeout=15)
    assert cp.returncode==73, cp.stderr.decode()
    progress=json.loads((out/'review_progress.json').read_text())
    assert progress['state']=='running' and progress['current']['index']==1
    assert not (out/'review_manifest.json').exists()
    result=await run_reviews(root,p,budget,out,rounds=2,in_doubt_policy='quarantine_and_continue')
    assert result['attempted_jobs']==2 and result['cost']['attempts']==2
    assert result['cost']['unknown_attempts']==1 and not result['eligible_for_human_acceptance']
    records=json.loads((out/'reviews.json').read_text())
    assert records[0]['error']['kind']=='operation_in_doubt' and records[1]['status']=='complete'
