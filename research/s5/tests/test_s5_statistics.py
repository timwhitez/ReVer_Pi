import math
import pytest
from research.s5.statistics import binomial_upper, spending_upper, selected_harm, group_identification

@pytest.mark.parametrize('n',[1,2,9,20,59,100,299])
def test_zero_case(n):assert binomial_upper(0,n,.05)==pytest.approx(1-.05**(1/n))

def test_no_samples():assert binomial_upper(0,0)==1

@pytest.mark.parametrize('k,n,a',[(-1,1,.05),(2,1,.05),(True,2,.05),(0,True,.05),(0,1,0),(0,1,1),(0,1,float('nan'))])
def test_invalid_binomial(k,n,a):
    with pytest.raises(ValueError):binomial_upper(k,n,a)

def test_alpha_budget_and_extra_uncertainty():
    assert sum(.05/(i*(i+1))for i in range(1,10001))<.05
    value=spending_upper(0,9,.05)
    assert value['upper']==pytest.approx(.565186464478293)
    assert value['upper']>binomial_upper(0,9,.05)

@pytest.mark.parametrize('action,f,p,out',[
    ('full',True,False,False),('full',None,None,False),('projected',False,None,False),
    ('projected',None,True,False),('projected',True,False,True),('projected',True,None,None),
    ('projected',None,False,None)])
def test_partial_information(action,f,p,out):assert selected_harm(action,f,p) is out

def test_missing_tasks_keep_group_unknown():
    frame={'a':['a1','a2'],'b':['b1']}
    rows=[dict(task_id='a1',source_group='a',action='full',full=True,projected=False),
          dict(task_id='b1',source_group='b',action='projected',full=True,projected=False)]
    result=group_identification(frame,rows)
    assert result['harm_lower_count']==1 and result['harm_upper_count']==2
    assert result['unknown_groups']==1
    assert result['observed_tasks']==2 and result['planned_tasks']==3

@pytest.mark.parametrize('frame,rows',[({'a':['x'],'b':['x']},[]),({'a':[]},[]),({},[]),
    ({'a':['x']},[dict(task_id='y',source_group='a',action='full',full=True,projected=True)]),
    ({'a':['x']},[dict(task_id='x',source_group='b',action='full',full=True,projected=True)])])
def test_invalid_frames(frame,rows):
    with pytest.raises(ValueError):group_identification(frame,rows)
