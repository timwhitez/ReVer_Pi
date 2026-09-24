import importlib.util
from reverpi_sources.contracts import S4

def test_all_six_manual_oracles():
    spec=importlib.util.spec_from_file_location('s4oracle',S4/'verify_sources.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    report=m.verify()
    assert len(report['tasks'])==6 and len(report['sources'])==3 and report['real_model_calls']==0
