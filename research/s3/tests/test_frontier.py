from pathlib import Path
import sys, math
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from reverpi_study.frontier import *


def rows(n, final=False, exact=True):
    return [dict(has_final_text=final and i==n-1,final_answer_exact=exact if final and i==n-1 else False) for i in range(n)]

@pytest.mark.parametrize('u',[(1,2,0),(-1,0,0),(True,0,0),(0,False,0),(0,0,1.5)])
def test_invalid_usage(u):
    with pytest.raises(ValueError):Usage(*u)

def test_usage_subsets():
    x=Usage.sum([Usage(10,8,2),Usage(15,3,1)])
    assert x.total==28 and x.uncached==14 and x.cached_tokens==11

@pytest.mark.parametrize('exact,expected',[(True,[0,0,1,1]),(False,[0,0,0,0])])
def test_observed_final(exact,expected):
    a=budget_curve(rows(3,True,exact),frozen_cap=4,status='completed',reason=None)
    assert [r['correct_by_k'] for r in a['curve']]==expected
    assert a['inference'] is False
    assert a['correct_completion_cost_observed']==exact

def test_admission_stop_is_not_request_only_failure():
    a=budget_curve(rows(2),frozen_cap=8,status='stopped',reason='budget_exhausted')
    assert [r['correct_by_k'] for r in a['curve']]==[0,0,None,None,None,None,None,None]

def test_unvisited():
    a=budget_curve([],frozen_cap=3,status='unvisited',reason=None)
    assert all(x['correct_by_k'] is None for x in a['curve'])

def test_cap():
    a=budget_curve(rows(6),frozen_cap=6,status='stopped',reason='suffix_cap')
    assert all(x['correct_by_k']==0 for x in a['curve'])

@pytest.mark.parametrize('r,n,status,reason',[(rows(3,True),2,'completed',None),(rows(3),3,'completed',None),(rows(2),3,'stopped','suffix_cap'),(rows(1),3,'unvisited',None),(rows(1,True),3,'stopped',None)])
def test_bad_endpoints(r,n,status,reason):
    with pytest.raises(ValueError):budget_curve(r,frozen_cap=n,status=status,reason=reason)

def test_multiple_final():
    with pytest.raises(ValueError):first_answer(rows(1,True)+rows(1,True))

def test_requests_after_final():
    with pytest.raises(ValueError):first_answer(rows(1,True)+rows(1))

def test_frontier_has_no_universal_winner():
    p=[dict(arm='full',requests=3,tokens=113462,correct=True),dict(arm='projected',requests=4,tokens=86162,correct=True)]
    assert nondominated(p)==['full','projected']
    p.append(dict(arm='worse',requests=6,tokens=120000,correct=True))
    assert nondominated(p)==['full','projected']

@pytest.mark.parametrize('points',[[dict(arm='x',requests=1,tokens=1,correct=False)],[dict(arm='x',requests=0,tokens=1,correct=True)],[dict(arm='x',requests=1,tokens=1,correct=True)]*2])
def test_no_failed_cost_victory(points):
    with pytest.raises(ValueError):nondominated(points)

def test_price_coefficients_actual():
    f=Usage(113303,73728,159);g=Usage(85982,7680,180)
    c=coefficients(f,g)
    assert c==dict(uncached=38727,cached=-66048,output=21)
    r=break_even(c,5)
    assert r['relation']=='greater_than'
    assert r['threshold']==pytest.approx(38832/66048)
    assert normalized_cost(g,.1,5)>normalized_cost(f,.1,5)
    assert normalized_cost(g,1,1)<normalized_cost(f,1,1)
    assert normalized_cost(g,.1,5)-normalized_cost(f,.1,5)==pytest.approx(32227.2)

@pytest.mark.parametrize('c,q,kind,t',[({'uncached':-2,'cached':0,'output':0},0,'all',None),({'uncached':0,'cached':0,'output':0},0,'none',None),({'uncached':-4,'cached':2,'output':1},0,'less_than',2.0)])
def test_price_inequality_general(c,q,kind,t):
    r=break_even(c,q);assert r['relation']==kind and r['threshold']==t

@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,True,'0.1'])
def test_unknown_price_not_zero(bad):
    with pytest.raises(ValueError):normalized_cost(Usage(1,0,1),bad,1)

def test_standardizing_cache_is_hypothesis():
    x=standardized_cache_scenario(Usage(20,20,1),Usage(10,0,2),.5,.1,5)
    assert x['delta']==pytest.approx(-.5)
    assert x['mode']=='algebraic_sensitivity_not_cache_experiment'

def test_missing_coverage():
    x=coverage_interval(2,6,2)
    assert (x['lower'],x['upper'])==(.2,.4)
    assert 'not_confidence' in x['interval_type']

def test_no_coverage():
    with pytest.raises(ValueError):coverage_interval(0,0,0)

def test_deployment_conditional_not_population():
    assert deployment_difference(1000,.01,-27300)['difference']==727
    assert deployment_difference(1000,.1,-27300)['difference']==-1730
    assert deployment_difference(1000,.1,-27300)['experimental_estimate'] is False

@pytest.mark.parametrize('margin,n',[(0,None),(.01,299),(.025,119),(.05,59),(.1,29)])
def test_fixed_sample_illustration(margin,n):
    assert zero_harm_sample_size(margin)==n
    if n:
        assert -math.expm1(math.log(.05)/n)<=margin
        assert n==1 or -math.expm1(math.log(.05)/(n-1))>margin

@pytest.mark.parametrize('m,a',[(1,.05),(-.01,.05),(.05,0),(.05,1),(.05,float('nan'))])
def test_invalid_sample_design(m,a):
    with pytest.raises(ValueError):zero_harm_sample_size(m,a)
