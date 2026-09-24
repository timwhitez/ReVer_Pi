import pytest
from reverpi_sources.analysis import cache_fraction
@pytest.mark.parametrize('value',[None,{}, {'input_tokens':100,'output_tokens':10}, {'input_tokens':100,'output_tokens':10,'input_tokens_details':{}}, {'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':101}}, {'input_tokens':0,'output_tokens':0,'input_tokens_details':{'cached_tokens':0}}])
def test_cache_absent_or_invalid_is_unknown(value):assert cache_fraction(value) is None
@pytest.mark.parametrize('field',['input_tokens_details','prompt_tokens_details'])
def test_cache_recorded(field):assert cache_fraction({'input_tokens':100,'output_tokens':10,field:{'cached_tokens':0}})==0

def test_cache_valid():assert cache_fraction({'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':50}})==.5

def test_contradictory_aliases():assert cache_fraction({'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':50},'prompt_tokens_details':{'cached_tokens':40}}) is None
