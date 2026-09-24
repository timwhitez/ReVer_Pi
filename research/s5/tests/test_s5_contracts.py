import pytest
from research.s5.contracts import strict_loads, strict_object_v2, score_v2, legacy_extract_v1, ContractError

@pytest.mark.parametrize('value',['{"a":1,"a":2}','{"a":{"b":1,"b":2}}','{"x":NaN}','{"x":Infinity}','{"x":-Infinity}','{"x":1e999}'])
def test_ambiguous_or_nonfinite_rejected(value):
    with pytest.raises(ValueError):strict_loads(value)

@pytest.mark.parametrize('value',['{"a":1}','  {"a":1}  ','```json\n{"a":1}\n```','```\n{"a":1}\n```'])
def test_one_object_contract(value):assert score_v2(value,{'a':1})['correct']

@pytest.mark.parametrize('value',['before {"a":1}','{"a":0} {"a":1}','```json\n{"a":1}\n``` more', '{"outer":BROKEN,{"a":1}}','```json\n```json\n{"a":1}\n```\n```','[1]', '', 'null'])
def test_no_cherry_picked_inner_object(value):assert not score_v2(value,{'a':1})['correct']

def test_historical_duplicate_witness():
    text='{"a":0,"a":1}'
    assert score_v2(legacy_extract_v1(text),{'a':1})['correct']
    assert not score_v2(text,{'a':1})['correct']

@pytest.mark.parametrize('value',[True,1.0,'1',None])
def test_exact_typed_values(value):
    import json
    assert not score_v2(json.dumps({'a':value}),{'a':1})['correct']

def test_missing_answer_is_not_wrong_final_answer():
    assert score_v2(None,{'a':1})['status']=='no_final_answer'

def test_size_and_keys():
    assert not score_v2(' '*100001,{'a':1})['correct']
    assert score_v2('{"b":1}',{'a':1})['status']=='schema_mismatch'
