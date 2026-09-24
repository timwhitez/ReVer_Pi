from pathlib import Path
import sys, math,random
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from reverpi_study.frontier import *


def test_600_crosschecks_against_scipy():
    from scipy.stats import beta
    count=0
    for alpha in (.05,.01):
        for n in range(1,301):
            # Clopper-Pearson one-sided upper bound with zero harms.
            upper=beta.ppf(1-alpha,1,n)
            formula=-math.expm1(math.log(alpha)/n)
            assert upper==pytest.approx(formula,abs=2e-14)
            count+=1
    assert count==600

def test_1000_price_sign_checks():
    rng=random.Random(24819)
    for _ in range(1000):
        f=Usage(rng.randint(1,100000),0,rng.randint(0,500));g=Usage(rng.randint(1,100000),0,rng.randint(0,500))
        f=Usage(f.input_tokens,rng.randint(0,f.input_tokens),f.output_tokens)
        g=Usage(g.input_tokens,rng.randint(0,g.input_tokens),g.output_tokens)
        r=rng.random()*2;q=rng.random()*30;c=coefficients(f,g)
        delta=normalized_cost(g,r,q)-normalized_cost(f,r,q)
        algebra=c['uncached']+c['cached']*r+c['output']*q
        assert delta==pytest.approx(algebra,abs=1e-8)
        b=break_even(c,q)
        predicted=(b['relation']=='all' or b['relation']=='greater_than' and r>b['threshold'] or b['relation']=='less_than' and r<b['threshold'])
        assert predicted==(delta<0)
