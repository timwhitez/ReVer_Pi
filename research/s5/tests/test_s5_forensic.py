import pytest
from research.s5.forensic import read_intents

def calls():return {'a':{'name':'read','arguments':{'path':'/outside'}}}

def test_aborted_is_not_success():
    x=read_intents(calls(),{'a':{'is_error':True,'text':'Operation aborted'}},{'inside'})
    assert x['denied_results']==1 and not x['all_read_intents_within_contract']

def test_pending_stays_pending():
    x=read_intents(calls(),{},{'inside'});assert x['pending_without_result']==1 and not x['sandbox_certified']

@pytest.mark.parametrize('r',[{'is_error':False,'text':'Operation aborted'},{'is_error':True,'text':'Error plus SECRET'},{'is_error':True,'text':'content'}])
def test_flags_cannot_hide_data(r):
    with pytest.raises(ValueError):read_intents(calls(),{'a':r},{'inside'})

def test_allowed_not_flagged():assert read_intents(calls(),{},{'/outside'})['attempted_out_of_allowlist']==0
