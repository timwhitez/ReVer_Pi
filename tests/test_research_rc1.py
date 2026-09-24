"""Offline contracts, not reasoning-model benchmark scores."""
import hashlib
import math
import sqlite3
import pytest
from reverpi.config import CompressionConfig
from reverpi.errors import LabError
from reverpi.memory import Archive, Compressor, Record
from reverpi.evidence_policy import Evidence, evidence_state, plan_evidence, SavingsEstimate, economic_gate
from reverpi.risk_calibration import harm_upper_bound, PairedCluster, calibrate
from reverpi.research_audit import audit_ledger, audit_sessions, compare_reported_scopes, diagnose_trial, TrialObservation, readonly_db


def ev(deps=None, verifier=None):
    return Evidence('v', hashlib.sha256(b'pass').hexdigest(), {'tree':'A'} if deps is None else deps, verifier)


@pytest.mark.parametrize('current,want', [({'tree':'A'},'snapshot_matches'),({'tree':'B'},'stale'),({},'unknown')])
@pytest.mark.parametrize('raw,integrity',[(None,'unchecked'),(b'pass','matches'),(b'failure','mismatch')])
def test_validity_is_not_integrity(current,want,raw,integrity):
    s=evidence_state(ev(),current,raw)
    assert s['applicability']==want and s['integrity']==integrity
    assert not s['decision_sufficiency_certified'] and not s['dependency_completeness_certified']


def test_unknown_dependencies_and_copied_versions():
    d={'x':'A'};e=ev(d);d['x']='B'
    assert e.dependencies['x']=='A'
    assert evidence_state(ev({}),{})['applicability']=='unknown'


@pytest.mark.parametrize('cur,raw,verifier,require,want',[
    ({'tree':'A'},None,None,True,'recover'),
    ({'tree':'B'},b'pass','test',True,'revalidate'),
    ({'tree':'B'},None,'test',True,'revalidate'),
    ({'tree':'B'},None,None,True,'request_new_evidence'),
    ({'tree':'A'},b'pass',None,True,'retain_as_evidence_not_a_proof'),
    ({'tree':'B'},b'wrong','test',True,'quarantine'),
    ({'tree':'B'},None,None,False,'recover')])
def test_lifecycle_plan_does_not_execute(cur,raw,verifier,require,want):
    p=plan_evidence(ev(verifier=verifier),cur,raw=raw,require_current=require)
    assert p['action']==want and not p['executes_verifier']


@pytest.mark.parametrize('sha',['','G'*64,'f'*63,'f'*65])
def test_bad_evidence_identity(sha):
    with pytest.raises(ValueError):Evidence('v',sha)


@pytest.mark.parametrize('value',[-1,float('nan'),float('inf'),True,'10'])
def test_bad_cost_inputs(value):
    with pytest.raises(ValueError):SavingsEstimate(value,1,1,1,1,'tokens')


def test_full_cost_gate_and_calibration_required():
    e=SavingsEstimate(100,10,20,15,5,'tokens',True)
    assert e.net_lower==50
    kw=dict(harm_tolerance=.05,representation_fits=True,evidence_contract_passed=True)
    assert economic_gate(e,calibrated_harm_upper=None,**kw)['reason']=='uncalibrated'
    assert economic_gate(e,calibrated_harm_upper=.06,**kw)['reason']=='risk_above_tolerance'
    assert economic_gate(e,calibrated_harm_upper=.04,**kw)['compress']
    assert not economic_gate(SavingsEstimate(100,10,20,15,5,'tokens'),calibrated_harm_upper=.04,**kw)['compress']
    assert not economic_gate(SavingsEstimate(30,10,20,15,5,'tokens',True),calibrated_harm_upper=.04,**kw)['compress']
    assert not economic_gate(e,calibrated_harm_upper=.01,**{**kw,'representation_fits':False})['compress']
    assert not economic_gate(e,calibrated_harm_upper=.01,**{**kw,'evidence_contract_passed':False})['compress']


@pytest.mark.parametrize('h,n,p',[(0,0,1),(0,6,1),(0,60,1),(0,60,4),(3,20,1),(9,10,1),(10,10,1)])
def test_exact_upper_bound(h,n,p):
    got=harm_upper_bound(h,n,policies=p)
    assert 0<=got<=1
    if n==0 or h==n:assert got==1
    elif h==0:assert got==pytest.approx(1-(.05/p)**(1/n))
    else:
        cdf=sum(math.comb(n,j)*got**j*(1-got)**(n-j) for j in range(h+1))
        assert cdf==pytest.approx(.05/p,abs=1e-10)


def test_upper_monotonic_and_multiple_policy_penalty():
    assert harm_upper_bound(0,6)>.39
    assert harm_upper_bound(0,60)<.05
    assert harm_upper_bound(0,60,policies=4)>.05
    assert harm_upper_bound(2,60)>harm_upper_bound(1,60)>harm_upper_bound(0,60)


@pytest.mark.parametrize('h,n,alpha,policies',[(True,6,.05,1),(7,6,.05,1),(0,-1,.05,1),(0,1,0,1),(0,1,float('nan'),1),(0,1,.05,0),(0,10001,.05,1)])
def test_bound_bad_inputs(h,n,alpha,policies):
    with pytest.raises(ValueError):harm_upper_bound(h,n,alpha=alpha,policies=policies)


def test_calibration_cluster_identity_and_heldout():
    a=PairedCluster('repo-a',True,False);b=PairedCluster('repo-b',False,True)
    r=calibrate([a,b],policies=2)
    assert r['harms']==1 and r['wins']==1 and r['quality_difference']==0
    with pytest.raises(ValueError):calibrate([a,a])
    with pytest.raises(ValueError):PairedCluster('a',True,False,'external')
    with pytest.raises(ValueError):PairedCluster('a',1,False)
    assert calibrate([])['harm_upper']==1


def make_db(path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE attempts(id INTEGER PRIMARY KEY,cell TEXT,state TEXT,actual_tokens INTEGER,reserve_tokens INTEGER)')
        db.executemany('INSERT INTO attempts VALUES(?,?,?,?,?)',[(1,'a','complete',10,30),(2,'a','unknown',None,50),(3,'b','failed',3,2)])


def test_readonly_ledger_reconciles_unknown_and_does_not_mutate(tmp_path):
    p=tmp_path/'ledger.sqlite';make_db(p);before=p.read_bytes()
    r=audit_ledger(p);t=r['totals']
    assert t['observed_tokens']==13 and t['unknown_reserved_tokens']==50 and t['accounted_tokens']==63
    assert t['unknown_attempts']==1 and t['observed_over_reservation_attempts']==1
    assert r['usd_cost'] is None and not r['unknown_reservations_are_verified_upper_bounds']
    assert before==p.read_bytes()
    with readonly_db(p) as db:
        with pytest.raises(sqlite3.OperationalError):db.execute('DELETE FROM attempts')


def test_wrong_costs_path_creates_nothing(tmp_path):
    p=tmp_path/'missing'/'ledger.sqlite'
    with pytest.raises(FileNotFoundError):audit_ledger(p)
    assert not p.parent.exists()


def test_incompatible_database_and_corruption(tmp_path):
    p=tmp_path/'a';sqlite3.connect(p).close()
    with pytest.raises(ValueError):audit_ledger(p)
    make_db(p)
    with sqlite3.connect(p) as db:db.execute('UPDATE attempts SET actual_tokens=-1 WHERE id=1')
    with pytest.raises(ValueError):audit_ledger(p)


def test_session_audit_does_not_invent_native_activation(tmp_path):
    p=tmp_path/'sessions.sqlite'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE compact_ops(session TEXT,state TEXT)')
        db.executemany('INSERT INTO compact_ops VALUES(?,?)',[('x','complete'),('x','failed')])
    r=audit_sessions(p)
    assert r['native_pi_compactions'] is None and len(r['gateway_compactions'])==2


@pytest.mark.parametrize('calls,compact,symptom,activation',[(0,0,'no_model_call','not_activated'),(None,None,'calls_unknown','not_observed'),(2,1,'model_was_called','activated')])
def test_failure_diagnosis_not_causal(calls,compact,symptom,activation):
    r=diagnose_trial(TrialObservation('x',0,calls,compact,0,True))
    assert r['official_reward']==0 and r['cause_status']=='unproven'
    assert r['symptom']==symptom and r['compression_activation']==activation
    assert r['compression_caused_outcome'] is None and not r['retry_authorized']


@pytest.mark.parametrize('calls,reward',[(True,0),(-1,0),(1,float('nan')),(0,True),(None,2)])
def test_invalid_trial_observation(calls,reward):
    with pytest.raises(ValueError):TrialObservation('x',reward,calls,None)


def test_reported_scope_discrepancy_is_not_missing_cost():
    r=compare_reported_scopes({'pi_original':64361,'pi_native':79467,'mask':64874,'rever_lite':24352},453341)
    assert r['condition_table_tokens']==233054 and r['difference_pending_scope_reconciliation']==220287
    assert r['difference_is_missing_cost'] is None


def hist():
    return [Record(id='g',kind='goal',text='Preserve 用户要求.'),Record(id='o',kind='observation',text='large '*1200,dependencies={'tree':'A'}),
        Record(id='t',kind='verification',text='A passed',dependencies={'tree':'A'}),
        Record(id='edit',kind='edit',text='Edited B',updates={'tree':'B'}),Record(id='todo',kind='obligation',text='B must be re-tested')]


@pytest.mark.asyncio
async def test_index_is_complete_and_pins_obligations(tmp_path):
    cfg=CompressionConfig(memory_bytes=3000,recent_records=1,excerpt_chars=32)
    a=Archive(tmp_path/'a',cfg);c=Compressor(cfg,a)
    m=await c.compress(hist(),'rever_indexed',cell='c')
    assert m.bytes<=3000 and not m.semantic_certified
    for r in hist():assert f'[{r.id}]' in m.text and m.details['handles'][r.id] in m.text
    assert 'B must be re-tested' in m.text and 'validity=stale' in m.text
    raw=a.recover('c','recover',handle=m.details['handles']['o'],chars=30)
    assert raw['text']==hist()[1].text[:30]


@pytest.mark.asyncio
async def test_index_recursive_refreshes_status_not_nested_stale_snapshots(tmp_path):
    cfg=CompressionConfig(memory_bytes=5000,recent_records=1);c=Compressor(cfg,Archive(tmp_path/'a',cfg))
    a=await c.compress([Record(id='g',kind='goal',text='Goal'),Record(id='v',kind='edit',text='A',updates={'tree':'A'}),
        Record(id='t',kind='verification',text='PASS',dependencies={'tree':'A'})],'rever_indexed',cell='c')
    b=await c.compress([Record(id='e',kind='edit',text='B',updates={'tree':'B'})],'rever_indexed',cell='c',previous=a)
    assert b.details['validity']['t']=='stale' and b.text.count('STATE_SNAPSHOT')==1
    assert set(a.record_ids)<set(b.record_ids)
    repeated=await c.compress([Record(id='v',kind='edit',text='A',updates={'tree':'A'})],'rever_indexed',cell='c',previous=b)
    assert repeated.details['current_versions']['tree']=='B' and repeated.text==b.text


@pytest.mark.asyncio
async def test_index_does_not_silently_omit_under_capacity_pressure(tmp_path):
    cfg=CompressionConfig(memory_bytes=512,recent_records=0);c=Compressor(cfg,Archive(tmp_path/'a',cfg))
    with pytest.raises(LabError) as e:await c.compress(hist(),'rever_indexed',cell='c')
    assert e.value.kind=='indexed_directory_capacity'


@pytest.mark.asyncio
async def test_index_rejects_changed_identity_and_config(tmp_path):
    cfg=CompressionConfig(memory_bytes=3000,recent_records=0);c=Compressor(cfg,Archive(tmp_path/'a',cfg))
    a=await c.compress(hist(),'rever_indexed',cell='c')
    with pytest.raises(LabError):await c.compress([Record(id='g',kind='goal',text='Changed')],'rever_indexed',cell='c',previous=a)
    with pytest.raises(LabError):await c.compress([hist()[0],hist()[0]],'rever_indexed',cell='c')
    with pytest.raises(LabError):await c.compress([],'rever_lite',cell='c',previous=a)
    other=Compressor(CompressionConfig(memory_bytes=4000),c.archive)
    with pytest.raises(LabError):await other.compress([],'rever_indexed',cell='c',previous=a)


def test_cli_offline_no_evidence_is_not_certified(tmp_path):
    from reverpi.research_cli import main
    p=tmp_path/'empty.json';p.write_text('[]')
    assert main(['calibrate','--input',str(p),'--policies','4'])==0
    assert main(['ledger','--db',str(tmp_path/'absent')])==2
