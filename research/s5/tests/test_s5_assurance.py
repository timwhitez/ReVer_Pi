import copy
import pytest
from research.s5.contracts import digest
from research.s5.assurance import assess, checked_action, inspect_legacy_gate, METRIC


def artifacts(n=30):
    c={'schema':'reverpi.s4.rule.v1','model_fork':'flash','synthetic':False,
       'development_groups':['development'],'rule':{'kind':'threshold','feature':'history_bytes','threshold':50,'direction':'ge'}}
    c['candidate_sha256']=digest(c)
    d={'schema':'reverpi.s5.design.v1','candidate_sha256':c['candidate_sha256'],'model_fork':'flash',
       'scorer_sha256':'1'*64,'runtime_sha256':'2'*64,'metric':METRIC,'frame':{f'g{i}':[f't{i}']for i in range(n)},
       'method':'fixed_sample','alpha':.05,'risk_margin':.1,'precommitted_before_outcomes':True,
       'independent_sampling_attested':True,'selection_not_outcome_dependent':True,
       'candidate_frozen_before_calibration':True,'development_source_groups':['development']}
    a={'schema':'reverpi.s5.risk-authorization.v1','approved':True,'design_sha256':digest(d),
       'candidate_sha256':c['candidate_sha256'],'risk_margin':.1,'alpha':.05,'metric':METRIC,'model_fork':'flash'}
    rows=[dict(task_id=f't{i}',source_group=f'g{i}',eligible=True,features={'history_bytes':100},full=True,projected=True)for i in range(n)]
    return c,d,a,rows

def call(c,d,a,rows):return assess(c,d,a,rows,expected_design_sha256=digest(d),expected_authorization_sha256=digest(a))

def test_conditional_positive_does_not_authorize_paid_requests():
    c,d,a,r=artifacts();g=call(c,d,a,r)
    assert g['allow_projection'] and not g['paid_requests_authorized']
    assert not g['currency_savings_established']
    kwargs=dict(expected_gate_sha256=g['gate_sha256'],model_fork='flash',runtime_sha256='2'*64,scorer_sha256='1'*64)
    assert checked_action(c,{'history_bytes':100},g,**kwargs)=='projected'
    assert checked_action(c,{'history_bytes':1},g,**kwargs)=='full'

@pytest.mark.parametrize('key',['precommitted_before_outcomes','independent_sampling_attested','selection_not_outcome_dependent','candidate_frozen_before_calibration'])
def test_each_attestation_required(key):
    c,d,a,r=artifacts();d[key]=False;a['design_sha256']=digest(d)
    assert not call(c,d,a,r)['allow_projection']

def test_five_percent_authorization_cannot_become_thirty():
    c,d,a,r=artifacts(9);d['risk_margin']=.3;a.update(design_sha256=digest(d),risk_margin=.05)
    g=call(c,d,a,r)
    assert g['upper_bound']<.3
    assert 'risk_margin_exceeds_authorization'in g['reasons'] and not g['allow_projection']

def test_incomplete_source_not_dropped():
    c,d,a,r=artifacts();g=call(c,d,a,r[:-1])
    assert 'incomplete_sampling_frame'in g['reasons']
    assert not g['allow_projection']

def test_explicit_unknown_not_zero():
    c,d,a,r=artifacts();r[0]['projected']=None;g=call(c,d,a,r)
    assert 'unresolved_group_outcomes'in g['reasons']
    assert not g['allow_projection']

def test_external_design_anchor_catches_consistent_local_rewrite():
    c,d,a,r=artifacts();old=digest(d);d['risk_margin']=.3;a.update(risk_margin=.3,design_sha256=digest(d))
    with pytest.raises(ValueError):assess(c,d,a,r,expected_design_sha256=old,expected_authorization_sha256=digest(a))

def test_legacy_gate_never_permission():
    c,d,a,r=artifacts();cal={'deployable':True,'candidate_sha256':c['candidate_sha256'],'data_sha256':'a'*64,'alpha':.05,'risk_margin':.3,'upper_bound':.283}
    cal['calibration_sha256']=digest(cal)
    auth={'approved':True,'risk_margin':.05,'alpha':.05,'scope':{'candidate_sha256':c['candidate_sha256'],'calibration_data_sha256':'b'*64}}
    out=inspect_legacy_gate(c,cal,auth)
    assert not out['allow_projection']
    assert {'risk_margin_exceeds_authorization','authorization_dataset_mismatch'} <= set(out['reasons'])
    assert cal['deployable'] is True

def test_default_without_bound_gate_is_full():
    c,d,a,r=artifacts();g=call(c,d,a,r)
    assert checked_action(c,{'history_bytes':999},None,model_fork='flash',runtime_sha256='2'*64,scorer_sha256='1'*64)=='full'
    with pytest.raises(ValueError):checked_action(c,{'history_bytes':999},g,model_fork='flash',runtime_sha256='2'*64,scorer_sha256='1'*64)

@pytest.mark.parametrize('field,value',[('model_fork','luna'),('runtime_sha256','3'*64),('scorer_sha256','3'*64)])
def test_runtime_identity_mismatch(field,value):
    c,d,a,r=artifacts();g=call(c,d,a,r);kw=dict(expected_gate_sha256=g['gate_sha256'],model_fork='flash',runtime_sha256='2'*64,scorer_sha256='1'*64);kw[field]=value
    with pytest.raises(ValueError):checked_action(c,{'history_bytes':99},g,**kw)

def test_gate_rehash_still_requires_external_digest():
    c,d,a,r=artifacts();g=call(c,d,a,r);old=g['gate_sha256'];g['runtime_sha256']='3'*64;del g['gate_sha256'];g['gate_sha256']=digest(g)
    with pytest.raises(ValueError):checked_action(c,{'history_bytes':99},g,expected_gate_sha256=old,model_fork='flash',runtime_sha256='3'*64,scorer_sha256='1'*64)

def test_zero_tolerance_never_finite_sample_certified():
    c,d,a,r=artifacts();d['risk_margin']=0;a.update(risk_margin=0,design_sha256=digest(d))
    assert not call(c,d,a,r)['allow_projection']

def test_synthetic_candidate_cannot_deploy():
    c,d,a,r=artifacts();c['synthetic']=True;del c['candidate_sha256'];c['candidate_sha256']=digest(c)
    d['candidate_sha256']=c['candidate_sha256'];a.update(candidate_sha256=c['candidate_sha256'],design_sha256=digest(d))
    assert not call(c,d,a,r)['allow_projection']

def test_exact_source_separation():
    c,d,a,r=artifacts();d['frame']['development']=['d0'];a['design_sha256']=digest(d)
    with pytest.raises(ValueError):call(c,d,a,r)


def test_actions_cannot_be_falsely_labelled_as_full():
    c,d,a,r=artifacts();r[0]['action']='full'
    with pytest.raises(ValueError):call(c,d,a,r)

def test_rule_choice_recomputed_from_feature():
    c,d,a,r=artifacts();r[0]['projected']=False
    g=call(c,d,a,r);assert g['group_identification']['harm_lower_count']==1
    r[0]['features']['history_bytes']=1
    g=call(c,d,a,r);assert g['group_identification']['harm_lower_count']==0

def test_no_eligible_is_full_and_not_harm():
    c,d,a,r=artifacts();r[0].update(eligible=False,features={},projected=None)
    assert call(c,d,a,r)['group_identification']['harm_lower_count']==0
