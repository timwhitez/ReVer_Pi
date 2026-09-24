from pathlib import Path
import sys,json,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from reverpi_study.analysis import phase

def setup(p,details=None,text='ok',tools=False):
    w=p/'full'/'wire';w.mkdir(parents=True)
    response={'usage':{'input_tokens':10,'input_tokens_details':details if details is not None else {'cached_tokens':0,'cache_write_tokens':0},'output_tokens':2,'total_tokens':12},'output':[]}
    if text:response['output'].append({'type':'message','content':[{'type':'output_text','text':text}]})
    if tools:response['output'].append({'type':'function_call','name':'read'})
    q=json.dumps({'input':[]}).encode();r=json.dumps(response).encode()
    (w/'q.json').write_bytes(q);(w/'r.json').write_bytes(r)
    (w/'index.json').write_text(json.dumps([{'status':200,'body_complete':True,'request_file':'q.json','response_file':'r.json','request_sha256':hashlib.sha256(q).hexdigest(),'response_sha256':hashlib.sha256(r).hexdigest()}]))
    (p/'FIXTURE.json').write_text(json.dumps({'marker':'ok'}))
    (p/'full'/'phase.json').write_text(json.dumps({'completed_responses':1,'central_cost':{'known_tokens':12},'state':'completed','owned_marker_exact':True}))
    return p

def test_read_exact_fixture(tmp_path):
    x=phase(setup(tmp_path),'full')
    assert x['tokens_to_correct_answer']==12

@pytest.mark.parametrize('details',[{}, {'cached_tokens':0}, {'cache_write_tokens':0}, {'cached_tokens':0,'cache_write_tokens':1}])
def test_unknown_or_nonzero_cache_is_not_normalized(tmp_path,details):
    with pytest.raises(ValueError):phase(setup(tmp_path,details),'full')

def test_rejected_mixed_final_and_tool(tmp_path):
    with pytest.raises(ValueError):phase(setup(tmp_path,tools=True),'full')

def test_scoring_mismatch(tmp_path):
    with pytest.raises(ValueError):phase(setup(tmp_path,text='wrong'),'full')
