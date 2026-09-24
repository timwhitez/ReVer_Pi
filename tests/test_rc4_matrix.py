import json,sys,hashlib
import httpx
import pytest
from reverpi.config import CompressionConfig,Budget
from reverpi.data import Checkpoint,Question
from reverpi.memory import Archive,Compressor
from reverpi.paired_interventions import freeze_intervention
from reverpi.intervention_matrix import prepare_matrix,execute_matrix,load_plan
from reverpi.errors import LabError
from test_rc3_mechanisms import records
from test_transport import raw

async def prep(tmp_path,provider,policy='halt',repeats=2,gold_sha=None):
    cfg=CompressionConfig(memory_bytes=2048,recent_records=1,recovery_search_mode='match')
    rs=records('unique token');mem=await Compressor(cfg,Archive(tmp_path/'a.sqlite',cfg)).compress(rs,'mask',cell='p')
    cp=Checkpoint(id='q',split='search',source_group='one-synthetic-cluster',provenance={'template':'test'},task='Answer.',records=rs,
                  questions=[Question(id='id',text='Historical value?',category='E')],synthetic=True)
    parent=tmp_path/'parent.json';h=freeze_intervention(parent,cp,mem,parent_cost={'known_tokens':0})
    w=tmp_path/'work';w.mkdir();(w/'check.py').write_text('print("checked")\n')
    reg=tmp_path/'registry.json';reg.write_text(json.dumps({'schema':1,'verifiers':[{
        'id':'check','argv':[sys.executable,'check.py'],'inputs':['check.py']}]}))
    root=tmp_path/'matrix'
    report=prepare_matrix(root,parent,h,provider,Budget(),cfg,workspace=w,registry=reg,
        registry_sha=hashlib.sha256(reg.read_bytes()).hexdigest(),repeats=repeats,interrupted_policy=policy,evaluation_gold_sha256=gold_sha)
    return root,report['plan_sha256']

@pytest.mark.asyncio
async def test_matrix_single_budget_resumes_commits_without_calls(tmp_path,provider):
    root,h=await prep(tmp_path,provider)
    calls=0
    def reply(_):
        nonlocal calls;calls+=1
        return httpx.Response(200,json=raw(provider,'{"answers":{"id":"unknown"}}'))
    kw={'acknowledge_unsandboxed':True,'transport':httpx.MockTransport(reply)}
    first=await execute_matrix(root,h,**kw);again=await execute_matrix(root,h,**kw)
    assert calls==8 and first['cost']['attempts']==again['cost']['attempts']==8
    assert first['visited_units']==8 and first['unvisited_units']==0
    assert all(u['reused_commit'] for u in again['units'])
    plans=[json.loads(p.read_text()) for p in root.glob('arms/*/plan.json')]
    assert len({p['cell'] for p in plans})==8 and len({p['memory_text_sha256'] for p in plans})==1
    assert not first['scored_by_this_runner'] and not first['formal_gate_passed']

@pytest.mark.asyncio
async def test_changed_terminal_result_rejected_not_replaced(tmp_path,provider):
    root,h=await prep(tmp_path,provider,repeats=1)
    transport=httpx.MockTransport(lambda _:httpx.Response(200,json=raw(provider,'{"answers":{"id":"unknown"}}')))
    await execute_matrix(root,h,acknowledge_unsandboxed=True,transport=transport)
    p=next(root.glob('arms/*/result.json'));p.write_text('{}')
    with pytest.raises(LabError,match='modified'):
        await execute_matrix(root,h,acknowledge_unsandboxed=True,transport=transport)

@pytest.mark.asyncio
@pytest.mark.parametrize('policy',['halt','quarantine_and_continue'])
async def test_interrupted_unit_never_replayed(tmp_path,provider,policy):
    root,h=await prep(tmp_path,provider,policy=policy,repeats=1)
    plan=load_plan(root,h);unit=plan['units'][0];(root/'arms'/unit['unit']).mkdir(parents=True)
    report=await execute_matrix(root,h,acknowledge_unsandboxed=True,
        transport=httpx.MockTransport(lambda _:httpx.Response(200,json=raw(provider,'{"answers":{"id":"unknown"}}'))))
    assert report['units'][0]['status']=='quarantined_interruption'
    assert report['cost']['attempts']==(0 if policy=='halt' else 3)
    assert not (root/'arms'/unit['unit']/'COMMIT.json').exists()

@pytest.mark.asyncio
async def test_template_change_rejected_before_ledger(tmp_path,provider):
    root,h=await prep(tmp_path,provider)
    (root/'template/check.py').write_text('different')
    with pytest.raises(ValueError,match='template changed'):await execute_matrix(root,h,acknowledge_unsandboxed=True)
    assert not (root/'ledger.sqlite').exists()

@pytest.mark.asyncio
async def test_matrix_requires_explicit_execution_acknowledgement(tmp_path,provider):
    root,h=await prep(tmp_path,provider)
    with pytest.raises(ValueError):await execute_matrix(root,h)
    assert not (root/'ledger.sqlite').exists()

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_documented_mock_matrix_finishes_with_unknown_not_invalid_answer(tmp_path,provider,protocol):
    p=provider.model_copy(update={'protocol':protocol})
    root,h=await prep(tmp_path,p,repeats=1)
    report=await execute_matrix(root,h,acknowledge_unsandboxed=True)
    assert report['visited_units']==4
    assert all(u['status']=='complete' for u in report['units'])
    for result in root.glob('arms/*/result.json'):
        obj=json.loads(result.read_text())
        assert obj['answers']=={'id':'unknown'}
        assert obj['recovery_calls']==obj['revalidation_calls']==0
    assert not report['formal_gate_passed']
