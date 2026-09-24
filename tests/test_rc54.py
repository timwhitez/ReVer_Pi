"""RC5.4 offline evidence / admission contract regression tests; no real provider."""
import importlib.util
import json
from pathlib import Path
import sys
import shutil
import pytest
from reverpi.util import canonical, digest
from reverpi.lean import init_campaign, prepare_campaign_review, validate_slot, run_slot
from reverpi.config import Budget
from reverpi.errors import LabError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from audit_supplement import validate_manifest, official_check
from offline_artifact_contracts import validate_profile_artifacts


def manifest(tmp_path):
    from hashlib import sha256
    f=tmp_path/'a.txt';f.write_text('unaltered')
    entries=[{'path':'a.txt','bytes':f.stat().st_size,'sha256':sha256(f.read_bytes()).hexdigest()}]
    (tmp_path/'SUPPLEMENT_MANIFEST.json').write_text(canonical(entries));return entries


def test_supplement_manifest_positive(tmp_path):
    manifest(tmp_path);assert set(validate_manifest(tmp_path))=={'a.txt'}


@pytest.mark.parametrize('case',['modified','duplicate','wrongsize','escape','symlink','fifo','unlistedwal','duplicatejson'])
def test_supplement_refuses_invalid_manifest(tmp_path,case):
    import os
    entries=manifest(tmp_path)
    if case=='modified':(tmp_path/'a.txt').write_text('changed')
    if case=='duplicate':entries*=2
    if case=='wrongsize':entries[0]['bytes']+=1
    if case=='escape':entries[0]['path']='../a.txt'
    if case=='symlink':
        (tmp_path/'a.txt').rename(tmp_path/'b.txt');(tmp_path/'a.txt').symlink_to('b.txt')
    if case=='fifo':
        (tmp_path/'a.txt').unlink();os.mkfifo(tmp_path/'a.txt')
    if case=='unlistedwal':
        (tmp_path/'a.txt').rename(tmp_path/'a.sqlite');entries[0]['path']='a.sqlite';(tmp_path/'a.sqlite-wal').write_bytes(b'hot')
    (tmp_path/'SUPPLEMENT_MANIFEST.json').write_text(canonical(entries))
    if case=='duplicatejson':
        (tmp_path/'SUPPLEMENT_MANIFEST.json').write_text('[{"path":"a.txt","path":"b.txt","bytes":1,"sha256":"x"}]')
    with pytest.raises((ValueError,OSError)):validate_manifest(tmp_path)


@pytest.mark.parametrize('field',['method','cell','reward','checksum'])
def test_official_original_must_correspond(field):
    row={'cell':'c','method':'mask','reward_key':'reward','official_reward':1,'official_task_checksum':'t'}
    official={'config':{'agent':{'kwargs':{'cell_id':'c','method':'mask'}}},'verifier_result':{'rewards':{'reward':1}},'task_checksum':'t'}
    official_check(row,official)
    if field=='method':official['config']['agent']['kwargs']['method']='pi_original'
    elif field=='cell':official['config']['agent']['kwargs']['cell_id']='x'
    elif field=='reward':official['verifier_result']['rewards']['reward']=0
    else:official['task_checksum']='other'
    with pytest.raises(ValueError):official_check(row,official)


def frozen_review(tmp_path,provider):
    root=tmp_path/'source';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('x=1\n')
    p=provider.model_copy(update={'model':'deepseek-flash','effort':'low','concurrency':1})
    out=tmp_path/'campaign';r=init_campaign(root,out,p)
    sub=prepare_campaign_review(root,out,r['campaign_sha256'],[{'file':'src/x.py'}])
    plan=json.loads((out/'campaign.json').read_text())
    return root,out,r,sub,plan


def test_independent_slot_anchor_detects_coordinated_rewrite(tmp_path,provider):
    root,out,r,sub,plan=frozen_review(tmp_path,provider)
    validate_slot(out,plan,r['campaign_sha256'],'review',expected_binding_sha=sub['slot_binding_sha256'])
    child=out/'review/plan.json';b=out/'review/campaign_binding.json'
    v=json.loads(child.read_text());v['jobs'][0]['rows'][0]['text']='x=2';child.write_text(canonical(v))
    v=json.loads(b.read_text());v['plan_sha256']=digest(json.loads(child.read_text()));b.write_text(canonical(v))
    # Internally consistent is insufficient; operator-held identity is separate.
    validate_slot(out,plan,r['campaign_sha256'],'review')
    with pytest.raises(ValueError,match='operator-held'):
        validate_slot(out,plan,r['campaign_sha256'],'review',expected_binding_sha=sub['slot_binding_sha256'])
    assert not (out/'review/ledger.sqlite').exists()


@pytest.mark.asyncio
async def test_paid_dispatch_without_slot_anchor_is_blocked_before_ledger(tmp_path,provider,monkeypatch):
    from reverpi import lean
    p=provider.model_copy(update={'mock':False,'max_output_tokens':1024})
    root,out,r,sub,plan=frozen_review(tmp_path,p)
    async def forbidden(*a,**k):raise AssertionError('must not invoke transport')
    monkeypatch.setattr(lean,'run_review',forbidden)
    with pytest.raises(LabError,match='missing_operator_held_slot_binding_sha256'):
        await run_slot(root,out,r['campaign_sha256'],'review',allow_paid=True)
    assert not (out/'review/ledger.sqlite').exists()


def test_pressure_screen_is_explicitly_not_sufficient(tmp_path,provider):
    from reverpi.preflight import budget_screen
    root,out,r,sub,plan=frozen_review(tmp_path,provider)
    child=json.loads((out/'review/plan.json').read_text());screen=budget_screen(out/'review',child)
    assert screen['necessary_not_sufficient_screen'] is True
    assert screen['full_workload_completion_guaranteed'] is False
    assert screen['physical_billing_cap_proven'] is False
    assert screen['stress_screen_passed']==screen['output_pressure_screen_passed']


@pytest.mark.asyncio
async def test_standalone_review_cannot_bypass_pressure_check(tmp_path,provider,monkeypatch):
    from reverpi.lean_review import prepare_review,run_review
    root=tmp_path/'srcroot';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('x=1\n')
    p=provider.model_copy(update={'mock':False,'model':'deepseek-flash','effort':'low','concurrency':1,'max_output_tokens':65536})
    monkeypatch.setenv(p.api_key_env,'offline-sentinel-never-dispatched')
    out=tmp_path/'review';r=prepare_review(root,out,p,Budget(max_total_tokens=1000,per_cell_tokens=1000),[{'file':'src/x.py'}])
    with pytest.raises(LabError,match='pressure'):
        await run_review(root,out,r['plan_sha256'],allow_paid=True)
    assert not (out/'ledger.sqlite').exists()


def profile_copy(tmp_path):
    src=ROOT/'reports/rc5_3/full_offline'
    names=['historical/report.json','public-tests/report.json','campaign/report.json','review-cache/report.json',
           'native/report.json','network/matrix.json','mechanism/results.json']
    for n in names:
        dst=tmp_path/n;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src/n,dst)
    return names


def test_profile_semantics_accept_prior_exact_contract(tmp_path):
    profile_copy(tmp_path)
    assert len(validate_profile_artifacts(tmp_path)['fixed_profile_families_verified'])==7


@pytest.mark.parametrize('family,mutation',[
 ('historical','missing_gold'),('historical','wrong_budget'),('public-tests','false_positive'),
 ('campaign','duplicate_cell'),('campaign','replay_billed'),('review-cache','missing_work'),
 ('review-cache','certify_mock'),('native','bad_receipt_hash'),('native','wrong_pass'),
 ('network','incomplete'),('network','duplicate_grid'),('mechanism','lost_archive'),('mechanism','wrong_current')])
def test_profile_rejects_success_exit_with_bad_artifacts(tmp_path,family,mutation):
    names=profile_copy(tmp_path);name=next(n for n in names if n.startswith(family+'/'))
    p=tmp_path/name;d=json.loads(p.read_text())
    if mutation=='missing_gold':d['gold_checks']=[]
    elif mutation=='wrong_budget':d['budget_expected_blocks']=[]
    elif mutation=='false_positive':d['rows'][1]['observed_passed']=True
    elif mutation=='duplicate_cell':d['matrix_rows'][1]=d['matrix_rows'][0]
    elif mutation=='replay_billed':d['matrix_rows'][0]['cached_replay_new_calls']=1
    elif mutation=='missing_work':d['completed']-=1
    elif mutation=='certify_mock':d['formal_gate_rejects_mock']=False
    elif mutation=='bad_receipt_hash':d['cells'][0]['receipt']['output']='tampered'
    elif mutation=='wrong_pass':d['cells'][1]['observed_passed']=True
    elif mutation=='incomplete':d['rows'][0]['phase']='headers'
    elif mutation=='duplicate_grid':d['rows'][1]=d['rows'][0]
    elif mutation=='lost_archive':d['compressed_methods'][0]['exact_archive_record_matches']=False
    elif mutation=='wrong_current':d['fresh_receipt']['passed']=True
    p.write_text(canonical(d))
    with pytest.raises(ValueError):validate_profile_artifacts(tmp_path)
