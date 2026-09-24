from __future__ import annotations
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from native_compaction_smoke import make_fixture, wire_view, reply, MARKER, CONDITIONS, PROTOCOLS, PRESSURES, require
from native_compaction_audit import expected_cells,validate_header
from remaining_local_tasks import clean_host_environment
from reverpi.config import Provider
from reverpi.protocols import parse_completion


def header(protocols=None):
    protocols=list(PROTOCOLS) if protocols is None else protocols
    rows=[{'protocol':p,'condition':c,'pressure':s} for p,c,s in sorted(expected_cells(protocols))]
    return {'schema':1,'kind':'explicit_boundary_compaction_contract','status':'passed','protocols':protocols,
        'rows':rows,'paid_model_calls':0,'scripted_actor':True,'native_gate_eligible':False,
        'benchmark_or_quality_claim':False,'manual_checkpoint_not_default_agent_policy':True}

@pytest.mark.parametrize('protocols,count',[(list(PROTOCOLS),14),(['chat_completions'],7),(['responses'],7)])
def test_full_predetermined_denominator(protocols,count):
    assert len(validate_header(header(protocols)))==count

@pytest.mark.parametrize('bad',[[],['chat_completions','chat_completions'],['live'],'chat_completions',None])
def test_bad_protocol_coverage_rejected(bad):
    with pytest.raises((ValueError,TypeError)):expected_cells(bad)

@pytest.mark.parametrize('key,value',[('status','failed'),('paid_model_calls',True),('paid_model_calls',1),
    ('scripted_actor',False),('native_gate_eligible',True),('benchmark_or_quality_claim',True),
    ('manual_checkpoint_not_default_agent_policy',False)])
def test_no_mock_result_promoted_to_research_gate(key,value):
    h=header();h[key]=value
    with pytest.raises(ValueError):validate_header(h)

@pytest.mark.parametrize('mutation',['missing','duplicate','unknown'])
def test_rows_not_dropped_or_replaced(mutation):
    h=header()
    if mutation=='missing':h['rows'].pop()
    elif mutation=='duplicate':h['rows'].append(deepcopy(h['rows'][0]))
    else:h['rows'][0]['condition']='favorable_replacement'
    with pytest.raises(ValueError):validate_header(h)

@pytest.mark.parametrize('pressure,count',[('short',1),('long',16),('oversized_recent',8)])
def test_owned_fixtures_have_one_mid_observation_marker(tmp_path,pressure,count):
    names=make_fixture(tmp_path,pressure);assert len(names)==count
    texts=[(tmp_path/n).read_text() for n in names]
    assert sum(t.count(MARKER) for t in texts)==1
    if pressure!='short':
        assert all(len(t.encode())<48*1024 for t in texts)
        assert MARKER not in texts[0][:400] and MARKER not in texts[0][-400:]
    assert not list(tmp_path.glob('*.gold*'))

@pytest.mark.parametrize('protocol',PROTOCOLS)
@pytest.mark.parametrize('tool',[False,True])
def test_scripted_reply_obeys_both_protocols(protocol,tool):
    p=Provider(mock=True,protocol=protocol,model='mock-reasoner')
    call={'id':'call_1','name':'read','arguments':{'path':'page_00.txt'}} if tool else None
    r=reply(protocol,1,call,'CONTINUED')
    result=parse_completion(p,r)
    assert result.calls==([call] if tool else [])
    assert result.model=='mock-reasoner'

@pytest.mark.parametrize('protocol',PROTOCOLS)
def test_wire_tool_names_are_protocol_equivalent(protocol):
    body={'messages':[],'input':[], 'tools':([{'function':{'name':'read'}}] if protocol=='chat_completions' else [{'name':'read'}])}
    rows,names=wire_view(body,protocol);assert rows==[] and names==['read']


def test_no_credentials_proxy_or_paid_authority_in_install_environment(tmp_path,monkeypatch):
    for name in ['REVER_API_KEY','OPENAI_API_KEY','AWS_SECRET_ACCESS_KEY','HTTP_PROXY','NPM_TOKEN','PYTHONPATH','NODE_OPTIONS','LD_PRELOAD']:
        monkeypatch.setenv(name,'SHOULD_NOT_PROPAGATE')
    env=clean_host_environment(tmp_path)
    assert 'SHOULD_NOT_PROPAGATE' not in json.dumps(env)
    assert env['HOME']==str(tmp_path)


def test_assert_optimization_does_not_disable_contract_checks():
    p=subprocess.run([sys.executable,'-O','-c',
        "import sys;sys.path.insert(0,'scripts');from native_compaction_smoke import require;require(False,'must stop')"],
        cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert p.returncode!=0 and 'must stop' in p.stderr


def test_local_driver_has_no_paid_option(tmp_path):
    p=subprocess.run([sys.executable,str(ROOT/'scripts/remaining_local_tasks.py'),'--out',str(tmp_path/'no-created'),'--allow-paid'],
        cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert p.returncode!=0 and not (tmp_path/'no-created').exists()
