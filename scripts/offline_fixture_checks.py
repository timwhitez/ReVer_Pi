#!/usr/bin/env python3
"""Run known offline contracts, using only explicit Mock providers.

Does not repair historical gold, grant paid authority, or approve a research gate.
The small owned fixtures are for plumbing, NOT benchmark measurements.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sys

if not __debug__:
    raise RuntimeError('Offline assertions require Python without optimization')

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from reverpi.config import CompressionConfig, Provider
from reverpi.data import Checkpoint, Question
from reverpi.memory import Archive, Compressor, Record
from reverpi.paired_interventions import freeze_intervention
from reverpi.preflight import certify_gold, budget_screen
from reverpi.lean import init_campaign, prepare_dev, prepare_campaign_review, run_slot, campaign_status
from reverpi.lean_export import export_run
from reverpi.intervention_scoring import score_matrix
from reverpi.revalidation import load_registry, execute_verifier, VerifierSpec
from reverpi.util import canonical, bytes_digest, atomic_write, atomic_create


def write(path:Path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    atomic_create(path, canonical(obj).encode('utf8'))


async def campaign_checks(out:Path):
    out.mkdir(parents=True,exist_ok=False)
    inputs=out/'controller-inputs';inputs.mkdir()
    cfg=CompressionConfig(memory_bytes=2048,recent_records=1)
    parents=[]
    for n in range(2):
        folder=inputs/f'source-{n}';folder.mkdir()
        records=[Record(id='goal',kind='goal',text='Report supported facts.'),
            Record(id='observation',kind='observation',text='History\n'+'irrelevant\n'*400+'historical value: item-'+str(n)+'\n'+'irrelevant\n'*400),
            Record(id='tail',kind='note',text='The current workspace is available.')]
        cp=Checkpoint(id=f'offline-{n}',split='search',source_group=f'owned-offline-{n}',
            provenance={'template':'rc5-2-interface-only'},task='Answer the given public questions.',
            records=records,questions=[Question(id='history',category='E',text='What was the historical value?'),
                Question(id='current',category='V',text='Does the current public check pass?')],synthetic=True)
        memory=await Compressor(cfg,Archive(folder/'archive.sqlite',cfg)).compress(records,'mask',cell=f'fixture-{n}')
        parent=folder/'parent.json';ps=freeze_intervention(parent,cp,memory,parent_cost={'observed_tokens':0,'model_calls':0},expected_generated=False)
        workspace=folder/'workspace';workspace.mkdir();(workspace/'check.py').write_text('assert True\n')
        registry=folder/'registry.json';write(registry,{'schema':1,'verifiers':[{'id':'public-check','argv':[sys.executable,'-B','check.py'],'inputs':['check.py'],'timeout_seconds':5}]})
        gold=folder/'evaluator-only-gold.json';write(gold,{'answers':{'history':[f'item-{n}'],'current':['yes']}})
        gs=bytes_digest(gold.read_bytes());cert=folder/'certificate.json';certify_gold(parent,ps,gold,gs,cert)
        parents.append((parent,ps,workspace,registry,bytes_digest(registry.read_bytes()),gold,gs,cert))
    rows=[]
    for protocol in ('chat_completions','responses'):
        provider=Provider(name='offline-only',model='deepseek-flash',mock=True,protocol=protocol,
            base_url='http://127.0.0.1:1/v1',concurrency=1,requests_per_minute=100000,
            tokens_per_minute=100000000,retry={'base_seconds':0,'cap_seconds':0,'total_seconds':30})
        assert provider.mock is True
        root=out/protocol;init=init_campaign(ROOT,root,provider)
        for i,(parent,ps,workspace,registry,rs,gold,gs,cert) in enumerate(parents):
            slot=f'dev-0{i+1}'
            prepare_dev(ROOT,root,init['campaign_sha256'],slot,parent=parent,parent_sha=ps,
                workspace=workspace,registry=registry,registry_sha=rs,gold_sha=gs,
                source_note='Owned offline interface fixture, not an independent research source.',gold_certificate=cert)
            first=await run_slot(ROOT,root,init['campaign_sha256'],slot,acknowledge_unsandboxed=True)
            before=campaign_status(ROOT,root,init['campaign_sha256'])['accounted_tokens']
            second=await run_slot(ROOT,root,init['campaign_sha256'],slot,acknowledge_unsandboxed=True)
            after=campaign_status(ROOT,root,init['campaign_sha256'])['accounted_tokens']
            assert first['visited_units']==4 and all(u['status']=='complete' for u in first['units'])
            assert before==after and all(u['reused_commit'] for u in second['units'])
            assert first['cost']['attempts']==second['cost']['attempts']
            stored=json.loads((root/slot/'plan.json').read_text())
            assert stored['provider']['protocol']==protocol
            score=score_matrix(root/slot,first['plan_sha256'],gold)
            write(out/f'{protocol}-{slot}-score.json',score)
            assert not score['formal_gate_passed']
            exported=export_run(root/slot,out/f'{protocol}-{slot}.zip')
            rows.append({'protocol':protocol,'slot':slot,'completed':4,'cached_replay_new_calls':second['cost']['attempts']-first['cost']['attempts'],
                'attempts':first['cost']['attempts'],'export_sha256':exported['zip_sha256'],
                'answers_are_mock_unknown':True,'quality_claim':False})
        prepare_campaign_review(ROOT,root,init['campaign_sha256'],[{'file':'src/reverpi/__init__.py'}])
        first=await run_slot(ROOT,root,init['campaign_sha256'],'review')
        second=await run_slot(ROOT,root,init['campaign_sha256'],'review')
        assert first['all_outputs_valid'] and not first['all_scoped_checks_pass']
        assert first['cost']['attempts']==second['cost']['attempts']==1
        write(out/f'{protocol}-status.json',campaign_status(ROOT,root,init['campaign_sha256'],write=True))
    result={'schema':1,'kind':'mock_interfaces_not_performance','matrix_cells':16,'matrix_rows':rows,
        'mock_scoped_review_jobs':2,'real_model_calls':0,'paid_authority_granted':False,
        'whole_tree_review_certified':False,'all_contracts_passed':True,
        'expected_research_gate_rejections_verified':True,'mock_dispatches_not_model_quality':True}
    write(out/'report.json',result)
    return result


def historical_checks(out:Path):
    from audit_rc5_campaign import audit
    out.mkdir(parents=True,exist_ok=False)
    delivery=ROOT/'provenance/rc5_delivery';result=audit(delivery)
    assert result['input_unchanged'] and result['all_live_observed_tokens']==86530
    assert result['campaign_observed_tokens']==48908 and result['all_live_unknown_reserved_tokens']==0
    write(out/'accounting.json',result)
    base=delivery/'flash-rc5-one-campaign';rows=[]
    for slot,source in (('dev-01','a'),('dev-02','b')):
        folder=base/slot;plan=json.loads((folder/'plan.json').read_text())
        cert=out/f'{slot}-gold-certificate.json'
        try:
            certify_gold(folder/'parent.json',plan['parent_sha256'],
                delivery/'evaluator-controller-side'/f'source-{source}.GOLD.json',
                plan['evaluation']['gold_sha256'],cert)
            valid=True
        except ValueError:
            valid=False
        assert valid == (source=='b') and cert.exists()==valid
        rows.append({'slot':slot,'schema_valid':valid,'certificate_written':cert.exists(),
                     'expected_outcome_verified':True,'official_gold_modified':False})
    screens=[]
    for slot in ('dev-01','dev-02','review'):
        folder=base/slot;plan=json.loads((folder/'plan.json').read_text())
        screen=budget_screen(folder,plan)
        assert not screen['stress_screen_passed']
        write(out/f'budget-{slot}.json',screen);screens.append(slot)
    report={'schema':1,'real_model_calls':0,'gold_checks':rows,
            'budget_expected_blocks':screens,'paid_ready':False,'all_expected_outcomes_verified':True}
    write(out/'report.json',report);return report


def validate_public_receipt(receipt:dict, expected:bool) -> None:
    """Distinguish the fixed negative-control test failures from broken execution.

    A missing pytest, timeout, changed input, or truncated output is NOT evidence
    that the deliberately regressed toml behavior was detected by public tests.
    """
    if (receipt.get('status') != 'completed'
            or receipt.get('output_truncated') is not False
            or receipt.get('observed_inputs_stable') is not True
            or receipt.get('passed') is not expected):
        raise ValueError('Public check did not complete under stable input conditions')
    text=receipt.get('output','')
    if bytes_digest(text.encode('utf8')) != receipt.get('output_sha256'):
        raise ValueError('Public check output hash mismatch')
    if expected:
        if receipt.get('returncode') != 0 or not re.search(r'96 passed, 1 skipped in ',text):
            raise ValueError('Expected the complete archived parse test suite')
    else:
        failures=set(re.findall(r'^FAILED (\S+)',text,re.MULTILINE))
        required={'tests/test_api.py::test__dict','tests/test_api.py::test_dict_decoder'}
        if (receipt.get('returncode') != 1 or failures != required
                or not re.search(r'2 failed, 19 passed, 2 deselected in ',text)):
            raise ValueError('Expected precisely the two injected toml regression failures')


async def full_review_cache(out:Path):
    """Exercise full scheduling/resume using synthetic replies, not a code reviewer."""
    from reverpi.config import load, StudyConfig
    from reverpi.review import run_reviews, check_review
    from reverpi.errors import LabError
    out.mkdir(parents=True,exist_ok=False)
    provider=load(ROOT/'configs/mock.chat.yaml',Provider)
    if provider.mock is not True:
        raise ValueError('Only an explicit Mock provider is allowed in this profile')
    budget=load(ROOT/'configs/review.yaml',StudyConfig).budget
    first=await run_reviews(ROOT,provider,budget,out/'run',rounds=3)
    write(out/'first-pass.json',first)
    second=await run_reviews(ROOT,provider,budget,out/'run',rounds=3)
    write(out/'cache-pass.json',second)
    jobs=first['planned_jobs']
    if (first['completed_jobs']!=jobs or second['cache_replays']!=jobs
            or first['cost']!=second['cost'] or not second['source_unchanged']
            or second['eligible_for_human_acceptance']):
        raise ValueError('Full Mock review scheduling or cache contract failed')
    try:
        check_review(out/'run/review_manifest.json',ROOT)
    except LabError as exc:
        if exc.kind!='review_evidence':raise
    else:
        raise AssertionError('Mock output was accepted as real review evidence')
    report={'schema':1,'kind':'mock_scheduler_not_independent_code_review',
        'rounds':3,'jobs':jobs,'completed':jobs,'cached_replays':jobs,
        'cache_added_attempts':0,'cache_added_accounted_tokens':0,
        'real_model_calls':0,'formal_gate_rejects_mock':True,'all_contracts_passed':True}
    write(out/'report.json',report)
    return report


def public_commands(out:Path):
    out.mkdir(parents=True,exist_ok=False)
    base=ROOT/'provenance/rc5_delivery/flash-rc5-one-campaign';rows=[]
    for slot,expected in (('dev-01',True),('dev-02',False)):
        folder=base/slot;workspace=out/slot;shutil.copytree(folder/'template',workspace)
        plan=json.loads((folder/'plan.json').read_text())
        specs=load_registry(folder/'registry.json',plan['registry_sha256'])
        assert set(specs)=={'public-tests'}
        original=specs['public-tests']
        # execute_verifier intentionally does NOT inherit an activated venv PATH.
        # Bind a NEW diagnostic-only spec to this environment's absolute Python.
        # Do not edit the historical registry or silently claim it was executable.
        guard=os.environ.get('LD_PRELOAD')
        guard_log=os.environ.get('REVER_OFFLINE_GUARD_LOG')
        if not guard or not guard_log:
            raise ValueError('Run this phase under verify_offline.py network guarding')
        argv=('/usr/bin/env',f'LD_PRELOAD={guard}',f'REVER_OFFLINE_GUARD_LOG={guard_log}',
              sys.executable,*original.argv[1:])
        spec=VerifierSpec(original.id,argv,original.inputs,original.timeout_seconds,original.max_output_bytes)
        write(out/f'{slot}-deployment-binding.json',{'original_registry_sha256':plan['registry_sha256'],
            'original_argv':list(original.argv),'diagnostic_argv':list(spec.argv),
            'reason':'Bind absolute Python and offline instrumentation; preserve original registry',
            'official_results_may_be_replaced':False})
        receipt=execute_verifier(spec,workspace)
        write(out/f'{slot}-receipt.json',receipt)
        validate_public_receipt(receipt,expected)
        rows.append({'slot':slot,'expected_passed':expected,'observed_passed':receipt['passed'],
            'original_registry_bytes_unchanged':True,'diagnostic_executable_rebound':True,
            'original_template_modified':False})
    report={'schema':1,'kind':'historical_public_commands_in_disposable_copies',
        'rows':rows,'real_model_calls':0,'all_expected_outcomes_verified':True,
        'official_results_regraded':False,'original_templates_modified':False}
    write(out/'report.json',report);return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase',choices=['campaign','historical','public-tests','review-cache'])
    p.add_argument('--out',required=True,type=Path);a=p.parse_args()
    if a.phase=='campaign':r=asyncio.run(campaign_checks(a.out))
    elif a.phase=='historical':r=historical_checks(a.out)
    elif a.phase=='review-cache':r=asyncio.run(full_review_cache(a.out))
    else:r=public_commands(a.out)
    print(json.dumps(r,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
