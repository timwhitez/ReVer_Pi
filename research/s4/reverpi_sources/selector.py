"""Offline single-boundary policy search. No LLM, no task-name dispatch.

Learns at most one threshold from development paired continuations, then evaluates
that ONE frozen rule on source-disjoint calibration. A zero allowed loss cannot
receive a finite-sample zero-risk certificate. No real training is claimed until
valid source-separated records exist; synthetic tests never yield deployability.
"""
from __future__ import annotations
import math
from collections import defaultdict
from reverpi.paired_prefix import require
from reverpi.util import digest
FEATURES=('eligible_bytes','history_bytes','eligible_count','prefix_requests','last_cached_fraction')

def number(v):return type(v) in (float,int) and math.isfinite(v)

def validate(rows,split):
    require(type(rows) is list,'Records must be a list')
    ids=set();snapshot_groups={}
    for r in rows:
        require(type(r) is dict and set(r)=={'id','source_group','snapshot_sha256','split','model_fork','synthetic','features','full_correct','projected_correct','full_tokens','projected_tokens','status'},'Unknown record fields; outcome/text cannot become a feature')
        require(type(r['id']) is str and r['id'] and r['id'] not in ids,'Duplicate or missing record id');ids.add(r['id'])
        require(type(r['source_group']) is str and r['source_group'],'Missing source group')
        h=r['snapshot_sha256'];require(type(h) is str and len(h)==64 and all(c in '0123456789abcdef' for c in h),'Invalid snapshot digest')
        require(h not in snapshot_groups or snapshot_groups[h]==r['source_group'],'One snapshot renamed as multiple sources');snapshot_groups[h]=r['source_group']
        require(r['split']==split and r['model_fork'] in {'flash','luna'},'Wrong split/model fork')
        require(type(r['synthetic']) is bool,'Synthetic flag required')
        f=r['features'];require(type(f) is dict and set(f)==set(FEATURES),'Feature allowlist violated')
        require(all(number(v) and v>=0 for v in f.values()) and f['last_cached_fraction']<=1,'Invalid predecision features')
        require(r['status']=='complete_pair' and type(r['full_correct']) is bool and type(r['projected_correct']) is bool,'Incomplete/unknown pairs cannot be training labels')
        require(all(type(r[k]) is int and r[k]>0 for k in ['full_tokens','projected_tokens']),'Completed logical token costs required')
    require(len({r['model_fork'] for r in rows})<=1,'Do not pool model forks')
    return rows

def action(rule,features):
    require(type(features) is dict and set(features)==set(FEATURES),'Unexpected inference features')
    require(all(number(v) and v>=0 for v in features.values()) and features['last_cached_fraction']<=1,'Invalid inference features')
    require(type(rule) is dict,'Invalid policy')
    if rule.get('kind')=='constant':
        require(set(rule)=={'kind','project'} and type(rule['project']) is bool,'Invalid constant policy')
        return rule['project']
    require(set(rule)=={'kind','feature','threshold','direction'} and rule['kind']=='threshold' and rule['feature'] in FEATURES,'Invalid policy')
    require(number(rule['threshold']) and rule['threshold']>=0 and rule['direction'] in {'ge','le'},'Invalid threshold policy')
    x=features[rule['feature']]
    return x>=rule['threshold'] if rule['direction']=='ge' else x<=rule['threshold']

def objective(rule,rows):
    groups=defaultdict(list);harm=False
    for r in rows:
        selected=action(rule,r['features']);harm |= selected and r['full_correct'] and not r['projected_correct']
        groups[r['source_group']].append((r['projected_tokens'] if selected else r['full_tokens'])/r['full_tokens'])
    return sum(sum(v)/len(v) for v in groups.values())/len(groups),harm

def fit(rows,min_sources=3):
    validate(rows,'development');require(type(min_sources) is int and min_sources>=3,'Do not fit the historical single source')
    require(len({r['source_group'] for r in rows})>=min_sources,'insufficient_development_sources')
    candidates=[{'kind':'constant','project':False},{'kind':'constant','project':True}]
    for feature in FEATURES:
        values=sorted({float(r['features'][feature]) for r in rows})
        # Deterministic bounded search grid drawn ONLY from development features.
        if len(values)>32:values=[values[round(i*(len(values)-1)/31)] for i in range(32)]
        for t in values:
            for direction in ['ge','le']:candidates.append({'kind':'threshold','feature':feature,'threshold':t,'direction':direction})
    scored=[]
    for i,r in enumerate(candidates):
        cost,harm=objective(r,rows)
        if not harm:scored.append((cost,i,r))
    loss,_,rule=min(scored,key=lambda x:(x[0],x[1]))
    body={'schema':'reverpi.s4.rule.v1','rule':rule,'development_groups':sorted({r['source_group'] for r in rows}),'development_snapshots':sorted({r['snapshot_sha256'] for r in rows}),
          'data_sha256':digest(rows),'model_fork':rows[0]['model_fork'],'synthetic':any(r['synthetic'] for r in rows),'development_cost_ratio':loss,'candidates_searched':len(candidates),
          'calibrated':False,'deployable':False,'operating_point':'one-shot first-eligible-boundary; not per-turn control','risk_guarantee':'none'}
    return {**body,'candidate_sha256':digest(body)}

def binomial_upper(k,n,alpha=.05):
    require(type(k) is int and type(n) is int and 0<=k<=n and number(alpha) and 0<alpha<1,'Invalid binomial arguments')
    if n==0 or k==n:return 1.
    if k==0:return -math.expm1(math.log(alpha)/n)
    def cdf(p):
        logs=[math.lgamma(n+1)-math.lgamma(j+1)-math.lgamma(n-j+1)+j*math.log(p)+(n-j)*math.log1p(-p) for j in range(k+1)]
        mx=max(logs);return math.exp(mx)*sum(math.exp(x-mx) for x in logs)
    lo,hi=0.,1.
    for _ in range(80):
        mid=(lo+hi)/2
        if not 0<mid<1:break  # float-rounding at extreme confidence must remain conservative
        if cdf(mid)>alpha:lo=mid
        else:hi=mid
    return hi

def calibrate(candidate,rows,*,risk_margin=None,alpha=.05):
    body=dict(candidate);h=body.pop('candidate_sha256',None);require(digest(body)==h,'Candidate changed after fit')
    validate(rows,'calibration')
    require(not ({r['source_group'] for r in rows}&set(candidate['development_groups'])),'Source leakage into calibration')
    require(not ({r['snapshot_sha256'] for r in rows}&set(candidate['development_snapshots'])),'Snapshot leakage into calibration')
    require(all(r['model_fork']==candidate['model_fork'] for r in rows),'Model mismatch')
    require(risk_margin is None or (number(risk_margin) and 0<=risk_margin<1),'Risk margin must be explicit or absent')
    grouped=defaultdict(list)
    for r in rows:grouped[r['source_group']].append(action(candidate['rule'],r['features']) and r['full_correct'] and not r['projected_correct'])
    k=sum(any(v) for v in grouped.values());n=len(grouped);upper=binomial_upper(k,n,alpha)
    synthetic=candidate['synthetic'] or any(r['synthetic'] for r in rows)
    passed=risk_margin is not None and upper<=risk_margin and n>0
    result={'schema':'reverpi.s4.calibration.v1','candidate_sha256':h,'data_sha256':digest(rows),'sources':n,'sources_with_observed_harm':k,'upper_bound':upper,
            'risk_margin':risk_margin,'alpha':alpha,'assumption':'Fixed independent source-group sample; at least one selected-case harm per group. This is not a task-level guarantee.',
            'calibration_passed':passed,'deployable':passed and not synthetic,'synthetic':synthetic,
            'default_action':'full','zero_loss_certified':False,'reason':'no_explicit_risk_margin' if risk_margin is None else 'risk_not_certified' if not passed else 'synthetic_only' if synthetic else 'conditional_gate_passed',
            'population':'submitted fully adjudicated eligible pairs only; not natural deployment',
            'risk_is_not_noninferiority_margin':True,'runtime_policy_integrated':False}
    return {**result,'calibration_sha256':digest(result)}

def decide(candidate,calibration,features):
    body=dict(candidate);h=body.pop('candidate_sha256',None)
    cbody=dict(calibration);ch=cbody.pop('calibration_sha256',None)
    require(digest(body)==h and digest(cbody)==ch and calibration.get('candidate_sha256')==h,'Policy/calibration identity mismatch')
    if not calibration.get('deployable'):return 'full'
    return 'projected' if action(candidate['rule'],features) else 'full'
