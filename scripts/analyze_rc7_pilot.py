#!/usr/bin/env python3
"""Audit then summarize a sealed paired run. Reads only; never contacts a model.

Reports completion under the frozen cap separately from unrestricted task
completion, and does not label a capped partial trajectory as an efficiency win.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]; sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
import jsonschema
from reverpi.util import atomic_create,canonical,strict_json_loads,contained_regular_file
from reverpi.paired_metrics import counts,budgeted_outcomes,price_delta
from reverpi.paired_prefix import decode_recorded_body
from verify_paired_prefix import audit,load


def analyze(run:Path):
    checked=audit(run); plan=load(run,'PLAN.json'); summary=load(run,'summary.json')
    phases={}; requests=[]; witness=[]; outcomes=[]
    for p in summary['phases']:
        phase=p['phase']; folder=run/phase; rows=[]
        for item in load(folder,'wire/index.json'):
            if not item.get('body_complete'):
                raise ValueError('Partial raw response cannot supply exact descriptive token totals')
            body=strict_json_loads(contained_regular_file(folder/'wire',item['request_file']).read_bytes())
            raw=contained_regular_file(folder/'wire',item['response_file']).read_bytes()
            decoded=decode_recorded_body(raw,item.get('content_encoding',''),plan['provider']['max_response_bytes'])
            response=strict_json_loads(decoded); usage=response['usage']
            if plan['provider']['protocol']=='responses':
                row={'input_tokens':usage['input_tokens'],'output_tokens':usage['output_tokens'],
                     'cached_input_tokens':usage.get('input_tokens_details',{}).get('cached_tokens',0)}
                calls=[x for x in response.get('output',[]) if x.get('type')=='function_call']
                tools={t['name']:t['parameters'] for t in body.get('tools',[]) if t.get('type')=='function'}
            else:
                row={'input_tokens':usage['prompt_tokens'],'output_tokens':usage['completion_tokens'],
                     'cached_input_tokens':usage.get('prompt_tokens_details',{}).get('cached_tokens',0)}
                calls=[{'name':t['function']['name'],'arguments':t['function']['arguments']}
                       for c in response.get('choices',[]) for t in c['message'].get('tool_calls',[])]
                tools={t['function']['name']:t['function']['parameters'] for t in body.get('tools',[])}
            rows.append(row); requests.append({'phase':phase,'index':item['index'],**row})
            for call in calls:
                if call['name']!='recover_evidence':continue
                args=strict_json_loads(call['arguments'])
                if 'handle' in args and 'query' in args:
                    jsonschema.Draft202012Validator(tools['recover_evidence']).validate(args)
                    witness.append({'phase':phase,'index':item['index'],
                                    'legal_under_sent_schema':True,
                                    'contains_both_selectors':True,
                                    'actual_runtime_rejection_count':next(x for x in checked['phases'] if x['phase']==phase)['rejected_recovery_calls'],
                                    'causal_effect_of_interface_fix':None})
        phases[phase]=counts(rows)
        if phase!='capture':outcomes.append({'phase':phase,'state':p['state'],'reason':p.get('reason'),
                         'completed_requests':len(rows),'marker_exact':p.get('owned_marker_exact')})
    total=sum(x['total_tokens'] for x in phases.values())
    if total!=checked['experiment_observed_tokens']:raise ValueError('Raw response totals differ from ledger audit')
    summary_out={'schema':1,'audit':checked,'phase_usage':phases,'request_usage':requests,
         'experiment_observed_tokens':total,'budgeted_outcomes':budgeted_outcomes(outcomes,plan['max_suffix_requests']),
         'schema_runtime_witnesses':witness,'usd':None,'new_model_calls_by_analysis':0,
         'population_quality_or_cost_superiority':False,'receipt_count_includes_prepared_requests':True}
    cap=phases.get('capture',{}).get('total_tokens',0)
    summary_out['logical_arm_totals']={p['phase']:{'observed_tokens_including_prefix':cap+phases[p['phase']]['total_tokens'],
        'completed_task_tokens':cap+phases[p['phase']]['total_tokens'] if p['state']=='completed' else None}
        for p in summary['phases'] if p['phase']!='capture'}
    if {'full','projected'}<=phases.keys():
        summary_out['observed_suffix_price_coefficients']=price_delta(phases['full'],phases['projected'])
        first={p:next(x for x in requests if x['phase']==p) for p in ('full','projected')}
        summary_out['first_request_price_coefficients']=price_delta(counts([first['full']]),counts([first['projected']]))
    return summary_out


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();r=analyze(a.run);atomic_create(a.out,canonical(r));print(canonical({'audit':'passed','tokens':r['experiment_observed_tokens'],'usd':None}));return 0
if __name__=='__main__':raise SystemExit(main())
