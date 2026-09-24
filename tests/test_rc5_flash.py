"""Offline contracts, not measurements of a model's research performance."""
from pathlib import Path
import copy
import json
import sys
import sqlite3
import zipfile
import hashlib
import httpx
import pytest

from reverpi.config import Provider, Budget
from reverpi.errors import LabError
from reverpi.lean import (init_campaign, load_campaign, campaign_status, prepare_dev,
    prepare_campaign_review, run_slot, validate_slot, audit_gen10)
from reverpi.lean_review import require_flash, prepare_review, validate_review, run_review
from reverpi.lean_subset import freeze_subset, compare_subset
from reverpi.lean_export import export_run
from reverpi.util import digest, canonical, source_manifest
from test_transport import raw
from test_rc4_matrix import prep

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def flash(provider):
    return provider.model_copy(update={'model':'deepseek-flash','concurrency':1})

@pytest.fixture
def code(tmp_path):
    p=tmp_path/'source';(p/'src').mkdir(parents=True)
    (p/'src/a.py').write_text('def f():\n    return 1\n')
    return p

@pytest.mark.parametrize('change',[
    {'model':'deepseek-v4-pro'}, {'model':'mock-reasoner'}, {'effort':'high'},
    {'effort_map':{'low':'off'}}, {'reasoning_style':'thinking_only'}, {'concurrency':2}])
def test_flash_no_model_or_effort_fallback(flash,change):
    with pytest.raises(ValueError): require_flash(flash.model_copy(update=change))

def test_flash_ambiguous_replay_refused(flash):
    p=flash.model_copy(update={'retry':flash.retry.model_copy(update={'retry_ambiguous':True})})
    with pytest.raises(ValueError):require_flash(p)

def test_init_fixed_allocations_no_paid_fullbench(code,tmp_path,flash):
    out=tmp_path/'campaign';r=init_campaign(code,out,flash)
    plan=load_campaign(code,out,r['campaign_sha256'])
    assert r['allocated_tokens']==300000 and len(plan['budgets'])==3
    assert plan['dev_repeats']==1 and plan['provider']['concurrency']==1
    assert not plan['external_benchmark_authorized'] and not r['physical_billing_cap_proven']
    assert campaign_status(code,out,r['campaign_sha256'])['accounted_tokens']==0
    with pytest.raises(FileExistsError):init_campaign(code,out,flash)

@pytest.mark.parametrize('args',[{'dev_tokens':0},{'dev_tokens':True},{'review_tokens':-5}, {'max_review_jobs':17}])
def test_init_rejects_invalid_admissions(code,tmp_path,flash,args):
    with pytest.raises(ValueError):init_campaign(code,tmp_path/'campaign',flash,**args)

def test_cannot_shrink_reservation_to_fit_tiny_budget(code,tmp_path,flash):
    p=flash.model_copy(update={'mock':False,'max_output_tokens':65536})
    with pytest.raises(ValueError,match='conservative'):init_campaign(code,tmp_path/'campaign',p,dev_tokens=20000)

def test_source_drift_blocks_run_not_status(code,tmp_path,flash):
    out=tmp_path/'campaign';r=init_campaign(code,out,flash)
    (code/'src/a.py').write_text('different')
    with pytest.raises(LabError):load_campaign(code,out,r['campaign_sha256'])
    assert not campaign_status(code,out,r['campaign_sha256'])['source_unchanged']

def test_campaign_tamper_and_unbound_ledger(code,tmp_path,flash):
    out=tmp_path/'campaign';r=init_campaign(code,out,flash)
    (out/'dev-01').mkdir();(out/'dev-01/ledger.sqlite').touch()
    with pytest.raises(ValueError,match='Unbound'):campaign_status(code,out,r['campaign_sha256'])
    (out/'dev-01/ledger.sqlite').unlink();(out/'dev-01/ledger.sqlite').symlink_to(tmp_path/'missing')
    with pytest.raises(ValueError,match='symlink'):campaign_status(code,out,r['campaign_sha256'])
    p=json.loads((out/'campaign.json').read_text());p['total_allocated_tokens']=1
    (out/'campaign.json').write_text(json.dumps(p))
    with pytest.raises(ValueError,match='identity'):load_campaign(code,out,r['campaign_sha256'])

def test_review_scope_is_explicit_and_bounded(code,tmp_path,flash):
    r=prepare_review(code,tmp_path/'review',flash,Budget(),[{'file':'src/a.py','first':2,'last':2}])
    assert r['jobs']==1 and r['coverage']['unique_lines']==1 and not r['formal_gate_passed']
    with pytest.raises(ValueError):prepare_review(code,tmp_path/'bad',flash,Budget(),[{'file':'../secret'}])
    with pytest.raises(ValueError,match='Overlapping'):
        prepare_review(code,tmp_path/'bad',flash,Budget(),[{'file':'src/a.py'},{'file':'src/a.py'}])

def test_review_never_silently_truncates_scope(code,tmp_path,flash):
    (code/'src/a.py').write_text(('x = "'+ 'y'*100+'"\n')*40)
    with pytest.raises(ValueError,match='above'):
        prepare_review(code,tmp_path/'review',flash,Budget(),[{'file':'src/a.py'}],max_jobs=1,packet_bytes=1024)
    assert not (tmp_path/'review').exists()

def review_good():
    return {'verdict':'needs_changes','overflow':False,'findings':[{'severity':'high','file':'src/a.py','line':2,
        'evidence':'return 1','issue':'Concrete issue','recommendation':'Concrete fix'}]}

@pytest.mark.parametrize('mutation',['fakequote','fakeline','passwithfinding','emptyneeds','extrakey','too_many'])
def test_review_contract_keeps_strict_evidence(mutation):
    job={'rows':[{'file':'src/a.py','line':2,'text':'    return 1'}]}
    obj=review_good()
    if mutation=='fakequote':obj['findings'][0]['evidence']='return 2'
    if mutation=='fakeline':obj['findings'][0]['line']=3
    if mutation=='passwithfinding':obj['verdict']='pass'
    if mutation=='emptyneeds':obj['findings']=[]
    if mutation=='extrakey':obj['approved']=True
    if mutation=='too_many':obj['findings']*=5
    with pytest.raises(ValueError):validate_review(canonical(obj),job)

def test_review_valid_and_insufficient_not_pass():
    job={'rows':[{'file':'src/a.py','line':2,'text':'    return 1'}]}
    assert validate_review(canonical(review_good()),job)['verdict']=='needs_changes'
    assert validate_review('{"verdict":"insufficient_context","overflow":false,"findings":[]}',job)['verdict']!='pass'

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_scoped_mock_review_does_not_certify_code(code,tmp_path,flash,protocol):
    p=flash.model_copy(update={'protocol':protocol});out=tmp_path/'review'
    report=prepare_review(code,out,p,Budget(),[{'file':'src/a.py'}])
    a=await run_review(code,out,report['plan_sha256']);b=await run_review(code,out,report['plan_sha256'])
    assert a['cost']['attempts']==b['cost']['attempts']==1
    assert a['all_outputs_valid'] and not a['all_scoped_checks_pass']
    assert a['mock'] and not a['formal_gate_passed'] and not a['whole_tree_certified']

@pytest.mark.asyncio
async def test_invalid_review_cached_no_paid_repair(code,tmp_path,flash):
    out=tmp_path/'review';r=prepare_review(code,out,flash,Budget(),[{'file':'src/a.py'}]);calls=0
    def reply(_):
        nonlocal calls;calls+=1
        return httpx.Response(200,json=raw(flash,'garbage'))
    a=await run_review(code,out,r['plan_sha256'],transport=httpx.MockTransport(reply))
    b=await run_review(code,out,r['plan_sha256'],transport=httpx.MockTransport(reply))
    assert calls==1 and a['rows'][0]['error']=='invalid_review_evidence' and not b['all_outputs_valid']

@pytest.mark.asyncio
async def test_scoped_review_source_change_retains_failed_generation(code,tmp_path,flash):
    out=tmp_path/'review';r=prepare_review(code,out,flash,Budget(),[{'file':'src/a.py'}])
    def reply(_):
        (code/'src/a.py').write_text('changed')
        return httpx.Response(200,json=raw(flash,'{"verdict":"pass","overflow":false,"findings":[]}'))
    report=await run_review(code,out,r['plan_sha256'],transport=httpx.MockTransport(reply))
    assert report['halt_reason']=='source_changed' and not report['source_unchanged']

@pytest.mark.asyncio
async def test_campaign_review_binding_and_accounting(code,tmp_path,flash):
    out=tmp_path/'campaign';r=init_campaign(code,out,flash)
    rr=prepare_campaign_review(code,out,r['campaign_sha256'],[{'file':'src/a.py'}])
    result=await run_slot(code,out,r['campaign_sha256'],'review')
    status=campaign_status(code,out,r['campaign_sha256'])
    assert status['observed_tokens']>0 and status['accounted_tokens']<=300000
    assert not result['formal_gate_passed']
    p=json.loads((out/'review/plan.json').read_text());p['budget']['max_total_tokens']+=1
    (out/'review/plan.json').write_text(json.dumps(p))
    with pytest.raises(ValueError):await run_slot(code,out,r['campaign_sha256'],'review')


def inventory(n=40):
    return {'benchmark':'DEMO-NOT-OFFICIAL','revision':'a'*40,'license':'DEMO metadata',
        'tasks':[{'id':f't{i}','source_group':f'g{i}','stratum':'a' if i<30 else 'b',
            'content_sha256':hashlib.sha256(str(i).encode()).hexdigest(),'exposure':'unseen',
            'eligible':True,'exclusion_reason':''} for i in range(n)]}

def outcomes(plan):
    return [{'id':t['id'],'baseline_success':True,'candidate_success':True,'baseline_tokens':100,
        'candidate_tokens':90,'baseline_result_sha256':'1'*64,'candidate_result_sha256':'2'*64} for t in plan['selected']]


def test_subset_seed_weighted_and_no_official_claim(tmp_path):
    inv=inventory();r=freeze_subset(inv,tmp_path/'a.json',n=24)
    rr=freeze_subset(inv,tmp_path/'b.json',n=24)
    assert r['plan_sha256']==rr['plan_sha256']
    p=json.loads((tmp_path/'a.json').read_text())
    assert [s['sample_n'] for s in p['strata']]==[18,6]
    assert sum(s['population_weight'] for s in p['strata'])==1
    assert not p['paid_evaluation_authorized'] and not p['official_full_benchmark_score']
    with pytest.raises(FileExistsError):freeze_subset(inv,tmp_path/'a.json')

@pytest.mark.parametrize('mutation',['outcomes','latest','duplicate','cost','exposure','noreason'])
def test_subset_rejects_outcome_selection_and_bad_metadata(tmp_path,mutation):
    inv=inventory()
    if mutation=='outcomes':inv['score']=1
    if mutation=='latest':inv['revision']='main'
    if mutation=='duplicate':inv['tasks'][1]['id']=inv['tasks'][0]['id']
    if mutation=='cost':inv['tasks'][0]['estimated_tokens']=1
    if mutation=='exposure':inv['tasks'][0]['exposure']='secretly_trained'
    if mutation=='noreason':inv['tasks'][0]['eligible']=False
    with pytest.raises(ValueError):freeze_subset(inv,tmp_path/'x.json')

def test_subset_exposure_exclusions_retained(tmp_path):
    inv=inventory();inv['tasks'][0].update(exposure='development',exclusion_reason='Previously read')
    freeze_subset(inv,tmp_path/'x.json');p=json.loads((tmp_path/'x.json').read_text())
    assert p['population_n']==39 and len(p['exclusions'])==1
    assert 't0' not in [x['id'] for x in p['selected']]

def test_no_harms_not_zero_loss_certificate(tmp_path):
    freeze_subset(inventory(),tmp_path/'x.json');p=json.loads((tmp_path/'x.json').read_text())
    r=compare_subset(p,digest(p),outcomes(p))
    assert r['no_observed_additional_failure'] and r['conditional_weighted_harm_upper_95']>0
    assert not r['quality_noninferiority_proven'] and not r['strict_zero_loss_proven']
    assert r['sample_token_reduction']==pytest.approx(.1)

def test_missing_rows_labels_costs_not_zero_or_dropped(tmp_path):
    freeze_subset(inventory(),tmp_path/'x.json');p=json.loads((tmp_path/'x.json').read_text());o=outcomes(p)
    r=compare_subset(p,digest(p),o[:-1]);assert r['allocated_pairs']==24 and r['reported_pairs']==23
    assert r['weighted_quality_difference'] is None and r['known_baseline_tokens'] is None
    o[0]['candidate_success']=None;o[0]['candidate_tokens']=None
    r=compare_subset(p,digest(p),o);assert not r['all_labels_known'] and r['known_candidate_tokens'] is None

def test_paired_harm_not_hidden_by_aggregate_wins(tmp_path):
    freeze_subset(inventory(),tmp_path/'x.json');p=json.loads((tmp_path/'x.json').read_text());o=outcomes(p)
    o[0]['candidate_success']=False;o[1]['baseline_success']=False
    r=compare_subset(p,digest(p),o);assert r['harms']==1 and r['wins']==1
    assert not r['no_observed_additional_failure']

def test_duplicate_clusters_disable_nominal_bound(tmp_path):
    inv=inventory()
    for t in inv['tasks']:t['source_group']='one-cluster'
    freeze_subset(inv,tmp_path/'x.json');p=json.loads((tmp_path/'x.json').read_text())
    r=compare_subset(p,digest(p),outcomes(p));assert r['conditional_weighted_harm_upper_95'] is None

@pytest.mark.parametrize('mutation',['unselected','duplicaterow','negative','boolean_cost','missinghash','listid'])
def test_outcome_integrity(tmp_path,mutation):
    freeze_subset(inventory(),tmp_path/'x.json');p=json.loads((tmp_path/'x.json').read_text());o=outcomes(p)
    if mutation=='unselected':o[0]['id']='not-selected'
    if mutation=='duplicaterow':o.append(o[0])
    if mutation=='negative':o[0]['candidate_tokens']=-1
    if mutation=='boolean_cost':o[0]['candidate_tokens']=False
    if mutation=='missinghash':o[0]['baseline_result_sha256']=None
    if mutation=='listid':o[0]['id']=[]
    with pytest.raises(ValueError):compare_subset(p,digest(p),o)

@pytest.mark.asyncio
async def test_portable_export_committed_matrix_and_sqlite(tmp_path,flash):
    root,h=await prep(tmp_path,flash,repeats=1)
    from reverpi.intervention_matrix import execute_matrix
    await execute_matrix(root,h,acknowledge_unsandboxed=True)
    out=tmp_path/'export.zip';r=export_run(root,out)
    with zipfile.ZipFile(out) as z:
        m=json.loads(z.read('matrix/EXPORT_MANIFEST.json'))
        assert m['plan_sha256']==h
        for name,sha in m['files'].items():assert hashlib.sha256(z.read('matrix/'+name)).hexdigest()==sha
        assert 'ledger.sqlite' in m['files'] and 'parent.json' in m['files']
        target=tmp_path/'restored.sqlite';target.write_bytes(z.read('matrix/ledger.sqlite'))
    with sqlite3.connect(target) as db:assert db.execute('select count(*) from attempts').fetchone()[0]==4
    assert not r['public_release_redaction_done']
    with pytest.raises(ValueError):export_run(root,out)

@pytest.mark.asyncio
async def test_export_rejects_symlink_incomplete_inside_root(tmp_path,flash):
    root,h=await prep(tmp_path,flash,repeats=1)
    with pytest.raises(ValueError):export_run(root,tmp_path/'x.zip') # No ledger before execution.
    from reverpi.intervention_matrix import execute_matrix
    await execute_matrix(root,h,acknowledge_unsandboxed=True)
    with pytest.raises(ValueError):export_run(root,root/'x.zip')
    (root/'link').symlink_to(root/'plan.json')
    with pytest.raises(ValueError):export_run(root,tmp_path/'x.zip')

@pytest.mark.asyncio
async def test_campaign_development_preparation_and_resume(tmp_path,flash):
    base,h=await prep(tmp_path/'existing',flash,repeats=1)
    out=tmp_path/'campaign';r=init_campaign(ROOT,out,flash)
    kw=dict(parent=base/'parent.json',parent_sha=json.loads((base/'plan.json').read_text())['parent_sha256'],
        workspace=base/'template',registry=base/'registry.json',
        registry_sha=hashlib.sha256((base/'registry.json').read_bytes()).hexdigest(),gold_sha='a'*64,
        source_note='Test-only synthetic source; not independent public benchmark')
    # Gold commitment must be a known explicit hash, no gold is visible to actor.
    prepare_dev(ROOT,out,r['campaign_sha256'],'dev-01',**kw)
    with pytest.raises(ValueError,match='distinct'):prepare_dev(ROOT,out,r['campaign_sha256'],'dev-02',**kw)
    a=await run_slot(ROOT,out,r['campaign_sha256'],'dev-01',acknowledge_unsandboxed=True)
    b=await run_slot(ROOT,out,r['campaign_sha256'],'dev-01',acknowledge_unsandboxed=True)
    assert a['cost']['attempts']==b['cost']['attempts']==4
    assert campaign_status(ROOT,out,r['campaign_sha256'])['observed_tokens']>0

def test_gen10_audit_is_rows_not_missing_raw_ledger():
    if not (ROOT/'reports/rc4_gen10_20260919').is_dir():
        pytest.skip("sealed gen10 reports are not distributed (research artifact)")
    r=audit_gen10(ROOT)
    assert r['result_rows_tokens']==37909 and r['evaluation_reports_tokens']==37909
    assert r['explicit_rerun_obligation_in_builder']
    assert all(not s['raw_gen10_ledger_in_gen10_report_directory'] for s in r['sources'])

@pytest.mark.parametrize('malformed',[{'verdict':[],'findings':[],'overflow':False},
    {'verdict':'needs_changes','findings':[dict(review_good()['findings'][0],severity=[])],'overflow':False}])
def test_malformed_model_types_are_validation_failures(malformed):
    job={'rows':[{'file':'src/a.py','line':2,'text':'    return 1'}]}
    with pytest.raises(ValueError):validate_review(canonical(malformed),job)

@pytest.mark.asyncio
async def test_deleted_review_ledger_never_means_fresh_free_resume(code,tmp_path,flash):
    out=tmp_path/'campaign';r=init_campaign(code,out,flash)
    rr=prepare_campaign_review(code,out,r['campaign_sha256'],[{'file':'src/a.py'}])
    await run_slot(code,out,r['campaign_sha256'],'review')
    (out/'review/ledger.sqlite').unlink()
    with pytest.raises(ValueError,match='ledger'):
        await run_slot(code,out,r['campaign_sha256'],'review')
    with pytest.raises(ValueError,match='ledger'):
        await run_review(code,out/'review',rr['plan_sha256'])

@pytest.mark.asyncio
async def test_scoped_interrupted_paid_claim_not_replayed(code,tmp_path,flash):
    import asyncio
    out=tmp_path/'review';r=prepare_review(code,out,flash,Budget(),[{'file':'src/a.py'}])
    calls=0
    def killed(_):
        nonlocal calls;calls+=1
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run_review(code,out,r['plan_sha256'],transport=httpx.MockTransport(killed))
    before=json.loads((out/'report.json').read_text())
    again=await run_review(code,out,r['plan_sha256'],transport=httpx.MockTransport(killed))
    assert calls==1 and again['halt_reason'] in {'operation_in_doubt','cancelled'}
    assert again['cost']['attempts']==before['cost']['attempts']==1
    assert not again['all_outputs_valid']

@pytest.mark.asyncio
async def test_cross_slot_reservation_breach_halts_campaign(code,tmp_path,flash):
    out=tmp_path/'campaign';r=init_campaign(code,out,flash)
    prepare_campaign_review(code,out,r['campaign_sha256'],[{'file':'src/a.py'}])
    await run_slot(code,out,r['campaign_sha256'],'review')
    db=sqlite3.connect(out/'review/ledger.sqlite')
    db.execute('update attempts set actual_tokens=reserve_tokens+1');db.commit();db.close()
    assert campaign_status(code,out,r['campaign_sha256'])['admission_breached']
    with pytest.raises(LabError,match='exceeded'):await run_slot(code,out,r['campaign_sha256'],'review')

@pytest.mark.asyncio
@pytest.mark.parametrize('changed',['parent','template'])
async def test_export_refuses_changed_frozen_inputs(tmp_path,flash,changed):
    root,h=await prep(tmp_path,flash,repeats=1)
    from reverpi.intervention_matrix import execute_matrix
    await execute_matrix(root,h,acknowledge_unsandboxed=True)
    if changed=='parent':(root/'parent.json').write_text('{}')
    else:(root/'template/check.py').write_text('different')
    with pytest.raises(ValueError):export_run(root,tmp_path/'x.zip')
