import copy,hashlib
import pytest
from reverpi.errors import LabError
from reverpi_sources.selector import *

def rows(split='development',start=0,n=3):
    return [{'id':f'{split}-{i}','source_group':f'source-{i}','snapshot_sha256':hashlib.sha256(str(i).encode()).hexdigest(),'split':split,'model_fork':'flash','synthetic':True,
             'features':{'eligible_bytes':10000+i,'history_bytes':30000+i,'eligible_count':1,'prefix_requests':3,'last_cached_fraction':.5},
             'full_correct':True,'projected_correct':True,'full_tokens':1000,'projected_tokens':800,'status':'complete_pair'} for i in range(start,start+n)]

def test_refuses_one_source():
    with pytest.raises(LabError,match='insufficient'):fit(rows(n=1))

def test_deterministic_fit_no_deployment():
    a=fit(rows());assert a==fit(rows());assert not a['deployable'] and a['synthetic']
    assert a['development_cost_ratio']==pytest.approx(.8)

def test_unsafe_rule_not_selected():
    r=rows();r[0]['projected_correct']=False;r[0]['projected_tokens']=1
    p=fit(r);assert not action(p['rule'],r[0]['features'])

def test_calibration_absent_refuses():
    p=fit(rows());c=calibrate(p,[]);assert not c['deployable'] and c['upper_bound']==1
    assert decide(p,c,rows()[0]['features'])=='full'

def test_zero_tolerance_not_claimed():
    p=fit(rows());c=calibrate(p,rows('calibration',3,30),risk_margin=0)
    assert not c['deployable'] and not c['zero_loss_certified'] and c['upper_bound']>0

def test_synthetic_never_deploys():
    p=fit(rows());c=calibrate(p,rows('calibration',3,300),risk_margin=.02)
    assert c['calibration_passed'] and not c['deployable'] and c['synthetic']

def test_source_overlap_rejected():
    with pytest.raises(LabError,match='leakage'):calibrate(fit(rows()),rows('calibration'))

def test_snapshot_relabel_rejected():
    r=rows();r[1]['snapshot_sha256']=r[0]['snapshot_sha256']
    with pytest.raises(LabError,match='renamed'):fit(r)

@pytest.mark.parametrize('extra',['task_name','answer','gold','reward','future_search'])
def test_feature_leak_rejected(extra):
    r=rows();r[0]['features'][extra]=1
    with pytest.raises(LabError,match='Feature allowlist'):fit(r)

@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,True])
def test_bad_numeric(value):
    r=rows();r[0]['features']['history_bytes']=value
    with pytest.raises(LabError):fit(r)

def test_no_external_training():
    with pytest.raises(LabError,match='Wrong split'):fit(rows('external'))

def test_incomplete_refuses():
    r=rows();r[0]['status']='budget_stop'
    with pytest.raises(LabError,match='Incomplete'):fit(r)

def test_model_forks_not_pooled():
    r=rows();r[0]['model_fork']='luna'
    with pytest.raises(LabError,match='pool'):fit(r)

@pytest.mark.parametrize('k,n',[(0,1),(0,24),(1,4),(4,10),(9,10),(10,10),(0,0)])
def test_bound_ranges(k,n):assert 0<=binomial_upper(k,n)<=1

def test_zero_margin_nonfinite():
    with pytest.raises(LabError):calibrate(fit(rows()),[],risk_margin=float('nan'))

@pytest.mark.parametrize('rule',[
    {'kind':'constant','project':'false'},
    {'kind':'constant','project':False,'gold':1},
    {'kind':'threshold','feature':'history_bytes','direction':'oops','threshold':1},
    {'kind':'threshold','feature':'history_bytes','direction':'ge','threshold':float('nan')},
])
def test_invalid_rule(rule):
    with pytest.raises(LabError):action(rule,rows()[0]['features'])

def test_calibration_tampering():
    p=fit(rows());c=calibrate(p,[]);c['deployable']=True
    with pytest.raises(LabError,match='identity'):decide(p,c,rows()[0]['features'])

def test_missing_features_not_silently_full():
    p=fit(rows());c=calibrate(p,[])
    # Even a valid fallback record is not a live policy; strict action validates features.
    with pytest.raises(LabError):action(p['rule'],{'gold':1})

@pytest.mark.parametrize('k,n',[(1,2),(9,10),(499,500)])
def test_extreme_alpha_conservative_no_crash(k,n):
    assert binomial_upper(k,n,1e-300)==1.0
