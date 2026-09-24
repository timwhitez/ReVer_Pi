from pathlib import Path
import pytest
from research.s5.census import safe_read,usage_counts

def test_cache_is_subset_and_missing_is_unknown():
    x=usage_counts({'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':90},'total_tokens':110})
    assert x['observed_tokens']==110 and x['uncached_input_tokens']==10
    assert usage_counts({'input_tokens':5,'output_tokens':3})['cached_input_tokens']is None

@pytest.mark.parametrize('u',[{'input_tokens':True,'output_tokens':1},{'input_tokens':1,'output_tokens':-1},
 {'input_tokens':10,'output_tokens':1,'input_tokens_details':{'cached_tokens':11}},
 {'input_tokens':10,'output_tokens':1,'total_tokens':21},
 {'input_tokens':10,'output_tokens':1,'output_tokens_details':{'reasoning_tokens':2}}])
def test_bad_usage(u):
    with pytest.raises(ValueError):usage_counts(u)

@pytest.mark.parametrize('p',['../outside','/etc/passwd','a//b','a/./b','a/../b','a\\b'])
def test_path_rejected(tmp_path,p):
    with pytest.raises(ValueError):safe_read(tmp_path,p)

def test_symlink_and_size(tmp_path):
    (tmp_path/'a').write_bytes(b'123');(tmp_path/'b').symlink_to(tmp_path/'a')
    with pytest.raises(OSError):safe_read(tmp_path,'b')
    with pytest.raises(ValueError):safe_read(tmp_path,'a',maximum=2)
    assert safe_read(tmp_path,'a')==b'123'
