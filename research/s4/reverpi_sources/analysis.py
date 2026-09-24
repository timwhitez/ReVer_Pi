"""Audited source-pair outcomes and predecision feature export, without imputation."""
from pathlib import Path
from reverpi.util import canonical,strict_json_loads,digest,unseal_cache
from reverpi.protocols import usage_parts
from .contracts import SourcePlan,load_task,read_gold,score_answer
from .audit import audit

def cache_fraction(usage):
    # Missing cache categories must not become an invented observed zero.
    if not isinstance(usage,dict):return None
    present=('prompt_cache_hit_tokens' in usage or any(isinstance(usage.get(k),dict) and 'cached_tokens' in usage[k] for k in ['input_tokens_details','prompt_tokens_details']))
    parts=usage_parts(usage)
    if not present or parts is None or parts[0]==0:return None
    return parts[2]/parts[0]

def analyze(run:Path,gold_path:Path):
    audited=audit(run)
    summary=strict_json_loads((run/'summary.json').read_bytes());task=load_task(run/'TASK.json')
    plan=SourcePlan.model_validate(strict_json_loads((run/'PLAN.json').read_bytes()))
    gold=read_gold(gold_path,task,plan.gold_sha256)
    phases={x['phase']:x for x in summary['phases']};prefix=phases['capture']['central_cost']['known_tokens']
    outcomes=[]
    for name,phase in phases.items():
        if name=='capture' and phase['state']=='boundary_captured':continue
        scored=score_answer(phase.get('answer') if phase['state']=='completed' else None,task,gold)
        known=phase['central_cost']['known_tokens'];unknown=phase['central_cost']['unknown_attempts']
        outcomes.append({'phase':name,'state':phase['state'],'reason':phase.get('reason'),'score':scored,
                         'suffix_or_capture_observed_tokens':known,'unknown_attempts':unknown,
                         'logical_observed_tokens':known+(prefix if name!='capture' else 0),
                         'logical_correct_completion_tokens':(known+(prefix if name!='capture' else 0)) if scored['correct'] and not unknown else None,
                         'units':'synthetic_mock_tokens' if plan.provider.mock else 'reported_tokens'})
    features=None;record=None;reason='no_eligible_prefix'
    if (run/'boundary.json').is_file():
        b=unseal_cache((run/'boundary.json').read_text());tape=unseal_cache((run/'tape.json').read_text())['entries']
        cf=cache_fraction(tape[-1]['response'].get('usage')) if tape else None
        features={'eligible_bytes':sum(x['original_bytes'] for x in b['prepared']['observations'] if x['eligible']),
                  'history_bytes':len(canonical(b['source']['messages']).encode('utf-8')),
                  'eligible_count':b['prepared']['eligible_count'],'prefix_requests':len(tape),'last_cached_fraction':cf}
        reason='scripted_data_not_real_training' if plan.provider.mock else 'incomplete_or_unknown_pair'
        by={x['phase']:x for x in outcomes}
        if not plan.provider.mock and summary['status']=='paired_complete' and {'full','projected'}<=set(by) and not audited['unknown_attempts']:
            reason='missing_cache_feature' if cf is None else 'ready_for_development_only'
            if cf is not None:
                record={'id':digest({'plan':audited['plan_sha'],'task':task['task_sha256']}),'source_group':task['source_group'],'snapshot_sha256':task['snapshot_sha256'],
                        'split':'development','model_fork':'flash' if plan.provider.model=='deepseek-flash' else 'luna','synthetic':False,'features':features,
                        'full_correct':by['full']['score']['correct'],'projected_correct':by['projected']['score']['correct'],
                        'full_tokens':by['full']['logical_observed_tokens'],'projected_tokens':by['projected']['logical_observed_tokens'],'status':'complete_pair'}
    return {'schema':'reverpi.s4.analysis.v1','task_id':task['task_id'],'source_group':task['source_group'],'plan_sha256':audited['plan_sha'],
            'run_status':summary['status'],'audited':True,'mock':plan.provider.mock,'new_calls':audited['experiment_new_call_count'],
            'common_prefix_observed_tokens':prefix,'experiment_observed_tokens':audited['experiment_observed_tokens'],'unknown_attempts':audited['unknown_attempts'],
            'currency_cost':None,'outcomes':outcomes,'predecision_features':features,'candidate_training_record':record,'training_record_status':reason,
            'denominator_warning':'Do not filter no-eligible, stopped or missing runs then claim population effects. Calibration completeness needs a frozen sampling frame.',
            'no_real_policy_trained':True,'general_quality_or_cost_improvement_established':False}
