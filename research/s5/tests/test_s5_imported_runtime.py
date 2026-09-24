"""New entry points reject legacy permission before preflight/paid dispatch."""
import asyncio, importlib.util, json, sys
from pathlib import Path
import pytest
from research.s5.contracts import digest
ROOT=Path(__file__).resolve().parents[3]

def load_deploy():
    spec=importlib.util.spec_from_file_location('s5_test_deploy',ROOT/'research/s4c/deploy.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

@pytest.fixture
def isolated_roots():
    sys.path.insert(0,str(ROOT/'research/s4'))
    from reverpi_sources import contracts,runner
    old=contracts.S4,runner.S4
    yield
    contracts.S4,runner.S4=old


def test_runtime_legacy_gate_is_rejected_before_preflight(tmp_path,monkeypatch,isolated_roots):
    m=load_deploy()
    candidate={'model_fork':'flash','rule':{'kind':'constant','project':True},'synthetic':False,'development_groups':[]}
    candidate['candidate_sha256']=digest(candidate)
    cp=tmp_path/'candidate.json';cp.write_text(json.dumps(candidate))
    class P:task_id='example'
    monkeypatch.setattr(m.SourcePlan,'model_validate',lambda _:P())
    monkeypatch.setattr(m,'load_task',lambda _: {})
    monkeypatch.setattr(m,'preflight',lambda *a,**k:pytest.fail('must not reach preflight'))
    plan=tmp_path/'plan.json';plan.write_text('{}');out=tmp_path/'out'
    with pytest.raises(Exception,match='Legacy S4 calibration'):
        asyncio.run(m.run_deployed(plan,digest({}),out,candidate_path=cp,calibration_path=tmp_path/'legacy.json'))
    assert not out.exists()


def test_malformed_gate_refused_before_preflight(tmp_path,monkeypatch,isolated_roots):
    m=load_deploy()
    candidate={'model_fork':'flash','rule':{'kind':'constant','project':True},'synthetic':False,'development_groups':[]};candidate['candidate_sha256']=digest(candidate)
    cp=tmp_path/'candidate.json';cp.write_text(json.dumps(candidate));gp=tmp_path/'gate.json';gp.write_text('{"deployable":true}')
    class Provider:mock=False;model='deepseek-flash'
    class P:task_id='example';provider=Provider()
    monkeypatch.setattr(m.SourcePlan,'model_validate',lambda _:P())
    monkeypatch.setattr(m,'load_task',lambda _: {})
    monkeypatch.setattr(m,'preflight',lambda *a,**k:pytest.fail('must not reach preflight'))
    plan=tmp_path/'plan.json';plan.write_text('{}');out=tmp_path/'out'
    with pytest.raises(ValueError,match='gate_identity'):
        asyncio.run(m.run_deployed(plan,digest({}),out,candidate_path=cp,gate_path=gp,gate_sha256='a'*64))
    assert not out.exists()


def test_versioned_score_rejects_legacy_duplicate_bypass(isolated_roots):
    spec=importlib.util.spec_from_file_location('s5_test_shim',ROOT/'research/s4c/s4c.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    task={'answer_keys':['value']};gold={'expected':{'value':1}}
    assert m.score_extracted('{"value":0,"value":1}',task,gold)['correct']
    assert not m.score_strict_v2('{"value":0,"value":1}',task,gold)['correct']
    assert m.score_strict_v2('```json\n{"value":1}\n```',task,gold)['correct']
