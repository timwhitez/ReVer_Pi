"""Separate exact-match development evaluator. Never imported by an actor.

Reads only sealed terminal results and a gold file whose hash was committed
before execution. Pending/interrupted units stay in the allocation denominator
but are not relabeled as observed failures. No inferential quality claim.
"""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from .intervention_matrix import read_committed
from .paired_interventions import read_intervention
from .research_audit import readonly_db
from .util import bytes_digest, digest, strict_json_loads


def score_matrix(root:Path, plan_sha:str, gold:Path) -> dict:
    root=root.resolve(strict=True);gold=gold.resolve(strict=True)
    if any(gold.is_relative_to(root/p) for p in ('template','workspaces','arms')):
        raise ValueError('Gold must remain outside actor-visible workspaces')
    plan=strict_json_loads((root/'plan.json').read_bytes())
    if digest(plan)!=plan_sha or plan.get('kind')!='development_same_parent_matrix':
        raise ValueError('Matrix plan differs from the evaluation commitment')
    evaluation=plan.get('evaluation',{})
    if evaluation.get('grader')!='exact_strings_v1' or not evaluation.get('gold_sha256'):
        raise ValueError('No pre-execution exact-match gold commitment; do not add one after observing results')
    raw=gold.read_bytes()
    from .preflight import validate_gold_mapping
    parent=read_intervention(root/'parent.json',plan['parent_sha256'])
    expected={q['id'] for q in parent['checkpoint']['questions']}
    allowed=validate_gold_mapping(raw,evaluation['gold_sha256'],list(expected))
    units=[];arms=defaultdict(lambda:{'allocated':0,'terminal':0,'scorable':0,'correct':0,'unknown':0})
    for unit in plan['units']:
        folder=root/'arms'/unit['unit'];name=unit['arm']['name'];summary=arms[name];summary['allocated']+=1
        row={'unit':unit['unit'],'cell':unit['cell'],'trial_id':unit['trial_id'],'arm':name,'correct':None,
             'status':'not_committed','result_sha256':None}
        if (folder/'COMMIT.json').exists():
            result=read_committed(folder,unit,plan_sha);row['status']=result['status'];summary['terminal']+=1
            row['result_sha256']=bytes_digest((folder/'result.json').read_bytes())
            row['recovery_calls']=result.get('recovery_calls');row['revalidation_calls']=result.get('revalidation_calls')
            if result.get('status')=='complete' and isinstance(result.get('answers'),dict) and set(result['answers'])==expected:
                row['correct']=all(type(result['answers'][k]) is str and result['answers'][k] in allowed[k] for k in expected)
                summary['scorable']+=1;summary['correct']+=int(row['correct'])
        if row['correct'] is None:summary['unknown']+=1
        units.append(row)
    # One ledger, including unknown reservations, and no summing repeated snapshots.
    with readonly_db(root/'ledger.sqlite',immutable_snapshot=True) as db:
        bound=db.execute("SELECT value FROM meta WHERE key='paired_matrix'").fetchone()
        if not bound or strict_json_loads(bound['value'])!=plan:raise ValueError('Wrong matrix ledger')
        r=db.execute('SELECT COUNT(*) attempts,COALESCE(SUM(actual_tokens),0) observed_tokens,'
            'COALESCE(SUM(CASE WHEN actual_tokens IS NULL THEN reserve_tokens ELSE 0 END),0) unknown_reserved_tokens FROM attempts').fetchone()
        costs=dict(r)
    costs['accounted_tokens']=costs['observed_tokens']+costs['unknown_reserved_tokens'];costs['currency_cost']=None
    pairs=defaultdict(dict)
    for u in units:pairs[u['trial_id']][u['arm']]=u['correct']
    contrasts=[]
    for trial,y in sorted(pairs.items()):
        complete=set(y)=={'noop','recover','revalidate','both'} and all(v is not None for v in y.values())
        contrasts.append({'trial_id':trial,'fully_scorable':complete,
            'descriptive_interaction':int(y['both'])-int(y['recover'])-int(y['revalidate'])+int(y['noop']) if complete else None})
    for row in arms.values():
        row['allocation_success_lower_bound']=row['correct']/row['allocated']
        row['allocation_success_upper_bound']=(row['correct']+row['unknown'])/row['allocated']
    return {'schema':1,'grader':'exact_strings_v1','plan_sha256':plan_sha,'gold_sha256':evaluation['gold_sha256'],
        'parent_sha256':plan['parent_sha256'],'memory_text_sha256':plan['memory_text_sha256'],
        'source_group':plan['source_group'],'unique_parents':1,'units':units,'arms':dict(arms),'reader_cost':costs,
        'parent_compression_cost_separate':plan['parent_compression_cost'],'contrasts':contrasts,
        'pending_or_interrupted_units_relabelled_as_failures':False,'formal_gate_passed':False,
        'population_effect_established':False,'repeats_are_not_independent_source_clusters':True,
        'causal_note':'Availability-arm contrasts include agent behavior. Tool calls alone do not establish benefit.'}
