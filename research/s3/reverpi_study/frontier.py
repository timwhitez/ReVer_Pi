"""Descriptive resource-to-answer frontiers, price sensitivity and design algebra.

No counterfactual model outputs are inferred. Resource thresholds below describe
prefixes of realized trajectories, not reruns under smaller admission budgets.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
import math
from .safeio import require


def integer(v,name,minimum=0):
    require(type(v) is int and v>=minimum,name+' must be an integer >= '+str(minimum));return v


def finite(v,name,lo=None,hi=None):
    require(type(v) in (int,float) and math.isfinite(v),name+' must be finite')
    require(lo is None or v>=lo,name+' below range');require(hi is None or v<=hi,name+' above range');return float(v)


@dataclass(frozen=True)
class Usage:
    input_tokens:int
    cached_tokens:int
    output_tokens:int
    def __post_init__(self):
        for k in ('input_tokens','cached_tokens','output_tokens'):integer(getattr(self,k),k)
        require(self.cached_tokens<=self.input_tokens,'Cached tokens are an input subset')
    @property
    def total(self):return self.input_tokens+self.output_tokens
    @property
    def uncached(self):return self.input_tokens-self.cached_tokens
    def __add__(self,o):return Usage(self.input_tokens+o.input_tokens,self.cached_tokens+o.cached_tokens,self.output_tokens+o.output_tokens)
    @classmethod
    def sum(cls,values):
        out=cls(0,0,0)
        for x in values:out=out+x
        return out
    def as_dict(self):return dict(input_tokens=self.input_tokens,cached_tokens=self.cached_tokens,uncached_tokens=self.uncached,output_tokens=self.output_tokens,total_tokens=self.total)


def first_answer(rows):
    """A final textual message marks termination; tool-only replies do not.

    Callers must have verified the endpoint and final-answer scoring separately.
    """
    found=[i for i,r in enumerate(rows,1) if r.get('has_final_text') is True]
    require(len(found)<=1,'Multiple final messages are unsupported by this profile')
    if found:
        require(found[0]==len(rows),'Requests after a final answer require a separate profile')
        require(type(rows[-1].get('final_answer_exact')) is bool,'Missing final score')
        return found[0],rows[-1]['final_answer_exact']
    return None,None


def budget_curve(rows,*,frozen_cap:int,status:str,reason:str|None):
    """S(k) on observed prefixes. Unobserved completion remains null.

    Token-admission stops do not identify request-only S(k) after the last
    observed response. Rows must be complete, ordered, real suffix responses.
    """
    integer(frozen_cap,'frozen_cap',1);require(len(rows)<=frozen_cap,'Responses exceed cap')
    require(status in {'completed','stopped','failed','unvisited'},'Unsupported terminal state')
    answer,exact=first_answer(rows)
    require((status=='completed')==(answer is not None),'Endpoint/text mismatch')
    if status=='unvisited':require(not rows,'Unvisited arm has responses')
    if reason=='suffix_cap':require(len(rows)==frozen_cap and answer is None,'Cap state inconsistent')
    result=[]
    for k in range(1,frozen_cap+1):
        if answer is not None and k>=answer:value=int(exact);basis='observed_final_answer'
        elif k<=len(rows):value=0;basis='observed_no_final_yet'
        else:value=None;basis='unobserved_after_stop'
        result.append({'k':k,'correct_by_k':value,'basis':basis})
    return {'curve':result,'first_final_request':answer,'final_exact':exact,
        'statistical_unit':'source_cluster_not_requests','independent_sample_count':None,'inference':False,'interpretation':'prefix_description_not_smaller_budget_reruns',
        'completed_cost_observed':status=='completed','correct_completion_cost_observed':status=='completed' and exact is True}


def nondominated(points):
    """Minimize observed resources among correctly completed trajectories only."""
    names=[x['arm'] for x in points];require(len(names)==len(set(names)),'Duplicate arms')
    for x in points:
        require(x['correct'] is True,'Cannot compare incomplete/incorrect cost as successful cost')
        integer(x['requests'],'requests',1);integer(x['tokens'],'tokens',1)
    return [x['arm'] for x in points if not any(
        y['requests']<=x['requests'] and y['tokens']<=x['tokens'] and
        (y['requests']<x['requests'] or y['tokens']<x['tokens']) for y in points)]


def coefficients(full:Usage,projected:Usage):
    return {'uncached':projected.uncached-full.uncached,'cached':projected.cached_tokens-full.cached_tokens,
            'output':projected.output_tokens-full.output_tokens}


def normalized_cost(usage:Usage,cache_price_ratio,output_price_ratio):
    """Normalize uncached price to 1; result is NOT an invoice or dollar value."""
    finite(cache_price_ratio,'cache price ratio',0);finite(output_price_ratio,'output price ratio',0)
    return float(Decimal(usage.uncached)+Decimal(usage.cached_tokens)*Decimal(str(cache_price_ratio))+Decimal(usage.output_tokens)*Decimal(str(output_price_ratio)))


def break_even(coeff,output_price_ratio):
    """Solve du+dc*r+do*q < 0 for nonnegative r. Boundary is strict."""
    finite(output_price_ratio,'output ratio',0)
    for k in ('uncached','cached','output'):require(type(coeff[k]) is int,'Integer coefficients required')
    a=Decimal(coeff['uncached'])+Decimal(coeff['output'])*Decimal(str(output_price_ratio));b=coeff['cached']
    if b==0:return {'relation':'all' if a<0 else 'none','threshold':None}
    t=float(-a/Decimal(b))
    return {'relation':'greater_than' if b<0 else 'less_than','threshold':t,'domain':'r>=0','strict':True}


def standardized_cache_scenario(full:Usage,projected:Usage,share,cache_price_ratio,output_price_ratio):
    """Algebraic relabelling ONLY; does not identify true cache-counterfactual behavior."""
    h=finite(share,'common cache share',0,1);r=finite(cache_price_ratio,'cache ratio',0);q=finite(output_price_ratio,'output ratio',0)
    unit=(1-h)+h*r
    f=full.input_tokens*unit+full.output_tokens*q;p=projected.input_tokens*unit+projected.output_tokens*q
    return {'common_cache_share':h,'normalized_full':f,'normalized_projected':p,'delta':p-f,
        'mode':'algebraic_sensitivity_not_cache_experiment','model_behavior_held_fixed_by_assumption':True}


def coverage_interval(eligible:int,ineligible:int,missing:int):
    for k,v in [('eligible',eligible),('ineligible',ineligible),('missing',missing)]:integer(v,k)
    n=eligible+ineligible+missing;require(n>0,'Empty coverage population')
    return {'assigned':n,'eligible':eligible,'ineligible':ineligible,'missing':missing,
        'lower':eligible/n,'upper':(eligible+missing)/n,'interval_type':'missing-data_identification_not_confidence'}


def deployment_difference(overhead_mean,eligible_probability,conditional_suffix_difference):
    """Law of total expectation under an explicit, unverified coupling assumption.

    H is matched-control overhead against the product baseline. E is measured
    under the same pre-intervention reference policy. Full/projected coincide
    outside E. This identity fails when pre-boundary tools/prompts already differ.
    """
    h=finite(overhead_mean,'overhead');p=finite(eligible_probability,'eligibility',0,1);d=finite(conditional_suffix_difference,'conditional effect')
    return {'difference':h+p*d,'experimental_estimate':False,
        'requires':['same reference pre-policy','identical noneligible continuation','representative eligible effects','overhead measured against product baseline']}


def zero_harm_sample_size(margin,alpha=.05):
    """One-sided fixed-sample zero-harm binomial illustration, NOT a release gate."""
    d=finite(margin,'margin',0,1);a=finite(alpha,'alpha',0,1)
    require(0<a<1 and d<1,'Require 0<alpha<1 and 0<=margin<1')
    if d==0:return None
    n=max(1,math.ceil(math.log(a)/math.log1p(-d)))
    # Defend rounding at exact integer boundaries.
    while -math.expm1(math.log(a)/n)>d:n+=1
    while n>1 and -math.expm1(math.log(a)/(n-1))<=d:n-=1
    return n
