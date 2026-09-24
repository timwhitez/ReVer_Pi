"""Recompute a resource frontier from audited raw Responses evidence.

The strict paired-prefix verifier must also pass. This module additionally reads
raw response usage and endpoint text; it does not certify source authenticity.
"""
from __future__ import annotations
from pathlib import Path
from .safeio import load,filehash,require,verify_tree
from .frontier import Usage,budget_curve,coefficients,normalized_cost,break_even,nondominated,standardized_cache_scenario,zero_harm_sample_size


def phase(run:Path,name:str):
    folder=run/name
    if not folder.exists():return {'status':'unvisited','reason':None,'rows':[],'usage':Usage(0,0,0),'tokens_to_correct_answer':None}
    meta=load(folder/'phase.json');wire=load(folder/'wire/index.json');rows=[];us=[]
    for w in wire:
        require(w['status']==200 and w['body_complete'] is True,'Incomplete wire requires explicit missingness support')
        for field in ('request_file','response_file'):
            from .safeio import relative_name
            relative_name(w[field]);require('/' not in w[field],'Wire member must be a basename')
        rp=folder/'wire'/w['response_file'];qp=folder/'wire'/w['request_file']
        require(filehash(rp)==w['response_sha256'] and filehash(qp)==w['request_sha256'],'Raw wire mismatch')
        r=load(rp);q=load(qp);u=r['usage'];details=u.get('input_tokens_details',{})
        require('cached_tokens' in details and 'cache_write_tokens' in details,'Missing cache classification is not zero usage')
        writes=details['cache_write_tokens']
        require(type(writes) is int and writes==0,'Nonzero/unknown write classification needs a different price model')
        x=Usage(u['input_tokens'],details.get('cached_tokens',0),u['output_tokens'])
        require(u.get('total_tokens',x.total)==x.total,'Total usage mismatch')
        text=''.join(c.get('text','') for m in r.get('output',[]) if m.get('type')=='message' for c in m.get('content',[]) if c.get('type')=='output_text')
        marker=load(run/'FIXTURE.json')['marker']
        calls=[m for m in r.get('output',[]) if m.get('type')=='function_call']
        require(not(text and calls),'Mixed final/tool response unsupported')
        row={'index':len(rows)+1,'usage':x.as_dict(),'has_final_text':bool(text),'final_answer_exact':text==marker if text else False,
             'tool_names':[c['name'] for c in calls],'input_items':len(q.get('input',[]))}
        rows.append(row);us.append(x)
    require(len(rows)==meta['completed_responses'],'Response count differs from phase')
    total=Usage.sum(us);require(total.total==meta['central_cost']['known_tokens'],'Phase usage differs')
    correct=bool(rows and rows[-1]['has_final_text'] and rows[-1]['final_answer_exact'])
    if meta['state']=='completed':require(meta['owned_marker_exact'] is correct,'Final score mismatch')
    return {'status':meta['state'],'reason':meta.get('reason'),'rows':rows,'usage':total,'tokens_to_correct_answer':total.total if meta['state']=='completed' and correct else None}


def analyze(evidence:Path):
    ev=verify_tree(evidence,'EVIDENCE_SHA256.json');rounds=[]
    for name in ('attempt_01','attempt_02'):
        run=evidence/name/'run';p=load(run/'PLAN.json');parts={s:phase(run,s) for s in ('capture','full','projected')}
        output={'round':name,'source_group':'owned-linked-note-generator','prospective_confirmatory':False,'fork':'luna',
                'cap':p['max_suffix_requests'],'phases':{},'completed_pair':False}
        for s,v in parts.items():
            output['phases'][s]={**{k:x for k,x in v.items() if k!='usage'},'usage':v['usage'].as_dict()}
            if s!='capture':output['phases'][s]['budget_curve']=budget_curve(v['rows'],frozen_cap=p['max_suffix_requests'],status=v['status'],reason=v['reason'])
        cap=parts['capture']['usage'];f=parts['full'];g=parts['projected']
        if all(v['tokens_to_correct_answer'] is not None for v in (f,g)):
            F=cap+f['usage'];G=cap+g['usage'];c=coefficients(F,G)
            points=[{'arm':a,'requests':len(parts[a]['rows']),'tokens':(cap+parts[a]['usage']).total,'correct':True} for a in ('full','projected')]
            scenarios=[]
            for r in (0,.1,.5,.6,1):
                for q in (0,5,20):
                    fc=normalized_cost(F,r,q);gc=normalized_cost(G,r,q)
                    scenarios.append({'cache_price_ratio':r,'output_price_ratio':q,'full_normalized':fc,'projected_normalized':gc,'delta':gc-fc,'saving_fraction':(fc-gc)/fc if fc else None,'hypothetical_not_invoice':True})
            output.update(completed_pair=True,logical_full=F.as_dict(),logical_projected=G.as_dict(),
                logical_saving_fraction=(F.total-G.total)/F.total,price_coefficients=c,
                price_break_even_q5=break_even(c,5),price_scenarios=scenarios,
                resource_points=points,nondominated_arms=nondominated(points),
                cache_standardizations=[standardized_cache_scenario(F,G,h,.1,5) for h in (0,.5,1)],
                record_note='K counts suffix requests; tokens include shared prefix once per logical arm')
        output['actual_experiment_usage']=Usage.sum(v['usage'] for v in parts.values()).as_dict()
        output['actual_experiment_requests']=sum(len(v['rows']) for v in parts.values())
        rounds.append(output)
    return {'schema':'reverpi.s3.reanalysis.v1','input_integrity':ev,'rounds':rounds,
        'actual_real_requests_reanalyzed':sum(x['actual_experiment_requests'] for x in rounds),
        'observed_tokens_reanalyzed':sum(x['actual_experiment_usage']['total_tokens'] for x in rounds),
        'new_model_calls':0,'population_effect_estimated':False,'usd':None,
        'source_clusters_with_completed_pair':1,'completed_pairs':sum(x['completed_pair'] for x in rounds),
        'scope':'posthoc_realized-resource_frontiers_not_new_experiments',
        'sample_size_illustrations':[{'hypothetical_margin':m,'zero_harm_independent_units_required':zero_harm_sample_size(m),'alpha':.05,'not_applied_to_adaptive_pilot':True} for m in (0,.01,.025,.05,.1)]}
