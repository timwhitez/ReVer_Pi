import json
from pathlib import Path
import pytest
from reverpi.results import read_results,write_results,current_cost_view
from reverpi.util import bytes_digest
from reverpi.errors import LabError


def sealed(tmp_path,cell='safe',native=False):
    row={'cell':cell,'task_id':'task','source_group':'owned','method':'mask','repeat':0,'status':'complete','success':True}
    track='native_pi_harbor' if native else 'checkpoint_diagnostics_NOT_Pi_benchmark'
    (tmp_path/'answers').mkdir()
    p=tmp_path/'answers/safe.json';p.write_text('{}')
    if native:row.update(official_result_path=str(p.resolve()),official_result_sha=bytes_digest(p.read_bytes()))
    else:row['answer_sha']=bytes_digest(p.read_bytes())
    manifest={'track':track,'results_schema':2,'result_grid':[row.copy()]}
    write_results(tmp_path/'results.json',[row]);return manifest,row


def test_malformed_grid_object(tmp_path):
    m,r=sealed(tmp_path);m['result_grid']=[1]
    with pytest.raises(LabError,match='grid_changed'):read_results(tmp_path,m)

@pytest.mark.parametrize('cell',['../escape','/tmp/escape','',None])
def test_unsafe_cell_rejected(tmp_path,cell):
    m,r=sealed(tmp_path,cell)
    with pytest.raises(LabError,match='grid_changed'):read_results(tmp_path,m)


def test_diagnostic_symlink_rejected(tmp_path):
    m,r=sealed(tmp_path);p=tmp_path/'answers/safe.json';p.unlink();(tmp_path/'other').write_text('{}');p.symlink_to('../other')
    with pytest.raises(LabError,match='result_changed'):read_results(tmp_path,m)

@pytest.mark.parametrize('value',[None,'','relative/result.json'])
def test_native_path_never_guessed_from_cwd(tmp_path,value):
    m,r=sealed(tmp_path,native=True);r['official_result_path']=value;write_results(tmp_path/'results.json',[r])
    with pytest.raises(LabError,match='manifest_schema'):read_results(tmp_path,m)


def test_native_valid_absolute_path(tmp_path):
    m,r=sealed(tmp_path,native=True);assert read_results(tmp_path,m)[0]['success']

@pytest.mark.parametrize('manifest',[{'track':'native_pi_harbor'}, {'track':'checkpoint_diagnostics_NOT_Pi_benchmark'}, [],None])
def test_malformed_cost_manifest_has_domain_error(tmp_path,manifest):
    with pytest.raises(LabError,match='manifest_schema'):current_cost_view(tmp_path,manifest,[])

@pytest.mark.parametrize('value',[None,'','gateway-relative'])
def test_invalid_gateway_location(tmp_path,value):
    m={'track':'native_pi_harbor','gateway_run':value,'gateway_study':{},'plan_id':'p'}
    with pytest.raises(LabError,match='manifest_schema'):current_cost_view(tmp_path,m,[])
