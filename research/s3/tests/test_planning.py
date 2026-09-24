from pathlib import Path
import sys, copy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from reverpi_study.planning import *


def row(i=0,group='repo-A',split='development',inspected=True):
    return dict(instance_id='example-'+str(i),source_group=group,revision='fixed-v1',content_sha256=hashlib.sha256(str(i).encode()).hexdigest(),provenance='synthetic test metadata, not external evidence',split=split,stratum='unknown_recovery_need',replay_scope='readonly_snapshot',inspected=inspected)

def test_repeats_not_independent_sources():
    x=validate_registry([row(0),row(1)])
    assert x['instances']==2 and x['source_groups']==1 and x['independence_proven'] is False

def test_no_cross_split_group():
    with pytest.raises(ValueError):validate_registry([row(),row(1,split='external',inspected=False)])

def test_identical_snapshot_not_new_source():
    a=row();b=row(1,'repo-B');b['content_sha256']=a['content_sha256']
    with pytest.raises(ValueError):validate_registry([a,b])

@pytest.mark.parametrize('field,value',[('split','test'),('inspected','false'),('replay_scope','writable'),('content_sha256','not-hash'),('revision','')])
def test_reject_bad_field(field,value):
    a=row();a[field]=value
    with pytest.raises(ValueError):validate_registry([a])

def test_inspected_cannot_be_blind():
    with pytest.raises(ValueError):validate_registry([row(split='external')])

def test_outcome_not_in_ordering_metadata():
    a=row();a['gold']='the answer'
    with pytest.raises(ValueError):validate_registry([a])

def test_duplicate_id():
    with pytest.raises(ValueError):validate_registry([row(),row()])

def test_plan_balances_without_outcomes():
    rows=[row(i) for i in range(7)]
    p=plan(rows,seed='freeze-2026-09-24',source_sha256='a'*64)
    q=plan(list(reversed(rows)),seed='freeze-2026-09-24',source_sha256='a'*64)
    assert p==q and verify_plan(p)
    a=sum(u['branch_order'][0]=='full' for u in p['units'])
    assert abs(a-(len(rows)-a))==1
    assert p['paid_authorized'] is False and p['runtime_plan'] is False
    assert p['authorized_token_budget']==0 and p['output_reservation']==65536
    assert p['registry_summary']['source_groups']==1

def test_separate_forks():
    p=plan([row()],seed='fixed123',source_sha256='a'*64,fork='flash')
    assert p['model_label']=='deepseek-flash'
    assert p['fork']=='flash'

def test_rehashed_order_tamper_still_rejected():
    p=plan([row()],seed='fixed123',source_sha256='a'*64)
    p['units'][0]['branch_order'].reverse()
    p['design_sha256']=digest({k:v for k,v in p.items() if k!='design_sha256'})
    with pytest.raises(ValueError):verify_plan(p)

def test_no_rehashed_authorization():
    p=plan([row()],seed='fixed123',source_sha256='a'*64)
    p['paid_authorized']=True
    p['design_sha256']=digest({k:v for k,v in p.items() if k!='design_sha256'})
    with pytest.raises(ValueError):verify_plan(p)

@pytest.mark.parametrize('kwargs',[dict(fork='another'),dict(interface='legacy'),dict(prefix_cap=0),dict(suffix_cap=31),dict(seed='x')])
def test_plan_strict(kwargs):
    args=dict(seed='fixed123',source_sha256='a'*64);args.update(kwargs)
    with pytest.raises(ValueError):plan([row()],**args)
