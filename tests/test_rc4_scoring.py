import json
import hashlib
import httpx
import pytest
from reverpi.errors import LabError
from reverpi.intervention_matrix import execute_matrix
from reverpi.intervention_scoring import score_matrix
from test_rc4_matrix import prep
from test_transport import raw

async def completed(tmp_path,provider):
    gold=tmp_path/'private_gold.json';gold.write_text(json.dumps({'answers':{'id':['unknown']}}))
    root,h=await prep(tmp_path,provider,repeats=1,gold_sha=hashlib.sha256(gold.read_bytes()).hexdigest())
    await execute_matrix(root,h,acknowledge_unsandboxed=True,
        transport=httpx.MockTransport(lambda _:httpx.Response(200,json=raw(provider,'{"answers":{"id":"unknown"}}'))))
    return root,h,gold

@pytest.mark.asyncio
async def test_exact_scoring_with_predeclared_gold_and_single_ledger(tmp_path,provider):
    root,h,gold=await completed(tmp_path,provider);r=score_matrix(root,h,gold)
    assert all(x['correct']==1 and x['allocated']==1 for x in r['arms'].values())
    assert r['reader_cost']['observed_tokens']==72
    assert r['contrasts'][0]['descriptive_interaction']==0
    assert not r['population_effect_established']
    assert not r['formal_gate_passed']
    assert r['gold_sha256'] not in (root/'parent.json').read_text() # commitment is scheduler-side

@pytest.mark.asyncio
async def test_scoring_refuses_posthoc_gold_commitment(tmp_path,provider):
    root,h=await prep(tmp_path,provider);gold=tmp_path/'gold';gold.write_text('{}')
    with pytest.raises(ValueError,match='pre-execution'):score_matrix(root,h,gold)

@pytest.mark.asyncio
async def test_mutated_gold_is_rejected(tmp_path,provider):
    root,h,gold=await completed(tmp_path,provider);gold.write_text('{"answers":{"id":["new"]}}')
    with pytest.raises(ValueError,match='differs'):score_matrix(root,h,gold)

@pytest.mark.asyncio
async def test_scorer_rejects_changed_terminal_answer(tmp_path,provider):
    root,h,gold=await completed(tmp_path,provider);p=next(root.glob('arms/*/result.json'))
    r=json.loads(p.read_text());r['answers']['id']='changed';p.write_text(json.dumps(r))
    with pytest.raises(LabError,match='modified'):score_matrix(root,h,gold)

@pytest.mark.asyncio
async def test_uncommitted_unit_stays_allocated_and_unknown(tmp_path,provider):
    root,h,gold=await completed(tmp_path,provider);next(root.glob('arms/*/COMMIT.json')).unlink()
    r=score_matrix(root,h,gold)
    assert sum(x['allocated'] for x in r['arms'].values())==4
    assert sum(x['unknown'] for x in r['arms'].values())==1
    assert r['contrasts'][0]['descriptive_interaction'] is None
    assert not r['pending_or_interrupted_units_relabelled_as_failures']
