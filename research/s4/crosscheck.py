#!/usr/bin/env python3
"""Numerical validation and explicit insufficient-real-data stop, no Provider."""
from pathlib import Path
import json,random,sys
R=Path(__file__).resolve().parent;ROOT=R.parents[1]
for p in (ROOT/'src',ROOT/'scripts',R):sys.path.insert(0,str(p))
from reverpi_sources.selector import binomial_upper,fit
from reverpi_sources.contracts import new_output
from reverpi.errors import LabError
from reverpi.util import canonical,atomic_create
from scipy.stats import beta

def check():
    count=0;maximum=0
    for n in [1,2,3,5,10,24,59,100,299,500]:
        for k in sorted({0,1,min(2,n),n//2,max(0,n-1),n}):
            for alpha in [.01,.025,.05,.1,.2]:
                actual=binomial_upper(k,n,alpha);expected=1. if k==n else float(beta.ppf(1-alpha,k+1,n-k))
                err=abs(actual-expected)
                if err>1e-10:raise ValueError((k,n,alpha,actual,expected))
                count+=1;maximum=max(maximum,err)
    try:fit([])
    except LabError as e:reason=str(e)
    else:raise ValueError('Empty real training set was accepted')
    return {'beta_quantile_crosschecks':count,'max_abs_error':maximum,'new_real_training_records':0,'real_fit':'blocked','reason':reason,'production_rule_activated':False,'real_model_calls':0}
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();r=check();atomic_create(new_output(a.out),canonical(r));print(canonical(r))
