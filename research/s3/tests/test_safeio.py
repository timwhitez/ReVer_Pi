from pathlib import Path
import sys,json,os
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from reverpi_study.safeio import *

@pytest.mark.parametrize('raw',['{"a":1,"a":2}','{"a":NaN}','{"x":Infinity}'])
def test_noncanonical_input_rejected(raw):
    with pytest.raises(ValueError):parse(raw)

@pytest.mark.parametrize('s',['../x','/tmp/x','a/../b','a//b','a/./b','a\\b','a/',''])
def test_path_escape(s):
    with pytest.raises(ValueError):relative_name(s)

def make_tree(p):
    p.mkdir();(p/'x').write_bytes(b'original')
    (p/'M.json').write_bytes(canonical({'files':{'x':filehash(p/'x')}}))
    return p

def test_manifest_and_tamper(tmp_path):
    p=make_tree(tmp_path/'e')
    assert verify_tree(p,'M.json')['verified_files']==1
    (p/'x').write_bytes(b'changed')
    with pytest.raises(ValueError):verify_tree(p,'M.json')

def test_extra_file(tmp_path):
    p=make_tree(tmp_path/'e');(p/'z').write_text('extra')
    with pytest.raises(ValueError):verify_tree(p,'M.json')

def test_symlink(tmp_path):
    p=make_tree(tmp_path/'e');(p/'x').unlink();(tmp_path/'outside').write_bytes(b'original');(p/'x').symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):verify_tree(p,'M.json')

def test_ancestor_symlink(tmp_path):
    p=make_tree(tmp_path/'e');(tmp_path/'link').symlink_to(p, target_is_directory=True)
    with pytest.raises(ValueError):load(tmp_path/'link'/'M.json')

def test_fifo_does_not_block(tmp_path):
    p=tmp_path/'fifo';os.mkfifo(p)
    with pytest.raises(ValueError):filehash(p)

def test_output_exclusive(tmp_path):
    p=tmp_path/'out.json';exclusive_json(p,{'x':1})
    with pytest.raises(ValueError):exclusive_json(p,{'x':2})
    assert load(p)=={'x':1}

def test_output_symlink(tmp_path):
    (tmp_path/'dir').mkdir();(tmp_path/'link').symlink_to(tmp_path/'dir',target_is_directory=True)
    with pytest.raises(ValueError):exclusive_json(tmp_path/'link'/'new.json',{})

def test_nan_cannot_serialize():
    with pytest.raises(ValueError):canonical({'n':float('nan')})

def test_manifest_name_escape(tmp_path):
    p=make_tree(tmp_path/'e')
    with pytest.raises(ValueError):verify_tree(p,'../M.json')


def test_failed_serialization_does_not_publish(tmp_path):
    p=tmp_path/'bad.json'
    with pytest.raises(ValueError):exclusive_json(p,{'n':float('nan')})
    assert not p.exists()
