from pathlib import Path
import hashlib,json,os
import pytest
from reverpi.util import canonical,digest
from reverpi_sources.contracts import *
from reverpi.errors import LabError

@pytest.mark.parametrize('value',['../a','/a','a//b','a/./b','a/../b','a\\b','a:xx','a\x00b','','./a'])
def test_path_reject(value):
    with pytest.raises(LabError):relative(value)

def test_tree_ordinary(tmp_path):
    (tmp_path/'a').mkdir();(tmp_path/'a/x.py').write_text('x=1\n')
    assert tree_manifest(tmp_path)=={'a/x.py':hashlib.sha256(b'x=1\n').hexdigest()}

def test_tree_symlinks(tmp_path):
    (tmp_path/'file').write_text('s');(tmp_path/'link').symlink_to('file')
    with pytest.raises((OSError,LabError)):tree_manifest(tmp_path)

def test_tree_parent_symlink(tmp_path):
    (tmp_path/'d/sub').mkdir(parents=True);(tmp_path/'d/sub/x').write_text('x');(tmp_path/'sym').symlink_to(tmp_path/'d',target_is_directory=True)
    with pytest.raises(LabError):read_file(tmp_path/'sym/sub','x')

def test_tree_fifo(tmp_path):
    os.mkfifo(tmp_path/'fifo')
    with pytest.raises(LabError):read_file(tmp_path,'fifo')

def test_oversize(tmp_path):
    (tmp_path/'a').write_bytes(b'1234')
    with pytest.raises(LabError):read_file(tmp_path,'a',3)

def task_and_gold():
    task=load_task(S4/'data/tasks/cache-behavior.json');p=S4/'data/controller/cache-behavior.gold.json'
    return task,read_gold(p,task,hashlib.sha256(p.read_bytes()).hexdigest())

def test_all_actual_snapshots():
    for p in (S4/'data/tasks').glob('*.json'):
        t=load_task(p);assert tree_manifest(S4/'data/sources'/t['source_id'])==t['files']

def test_task_digest_tamper(tmp_path):
    t,g=task_and_gold();t['prompt']+=' extra';p=tmp_path/'x.json';p.write_text(canonical(t))
    with pytest.raises(LabError):load_task(p)

def test_gold_binding():
    t,g=task_and_gold()
    with pytest.raises(LabError):read_gold(S4/'data/controller/cache-behavior.gold.json',t,'0'*64)

def test_scoring_correct_and_wrong():
    t,g=task_and_gold();assert score_answer(canonical(g['expected']),t,g)['correct']
    v=dict(g['expected']);v['size_after_replace']=4;assert not score_answer(canonical(v),t,g)['correct']

@pytest.mark.parametrize('answer',['not json','{}','[]','{"x":1,"x":2}',None])
def test_invalid_answers(answer):
    t,g=task_and_gold();assert not score_answer(answer,t,g)['correct']

def test_bool_not_int():
    t=load_task(S4/'data/tasks/cache-keys.json');p=S4/'data/controller/cache-keys.gold.json';g=read_gold(p,t,hashlib.sha256(p.read_bytes()).hexdigest())
    v=dict(g['expected']);v['numeric_hash_equal']=1
    assert not score_answer(canonical(v),t,g)['correct']

def test_gold_not_in_public_workspace():
    for p in (S4/'data/tasks').glob('*.json'):
        t=load_task(p);assert not any('gold' in x.lower() or 'oracle' in x.lower() for x in t['files'])
        assert 'expected' not in t

def test_existing_out_rejected(tmp_path):
    with pytest.raises(LabError):new_output(tmp_path)
