from concurrent.futures import ThreadPoolExecutor
import time
import pytest
from reverpi.config import Budget
from reverpi.errors import LabError
from reverpi.ledger import Ledger


def test_idempotence(ledger):
    assert ledger.claim("a","h","cell") is None
    with pytest.raises(LabError,match="in_doubt"):ledger.claim("a","h","cell")
    ledger.finish("a",result={"ok":True})
    assert ledger.claim("a","h","cell")=={"ok":True}
    with pytest.raises(LabError,match="idempotency"):ledger.claim("a","changed","cell")

def test_failed_not_retried(ledger):
    ledger.claim("a","h","c");ledger.finish("a",error=LabError("x","failure"))
    with pytest.raises(LabError,match="failure"):ledger.claim("a","h","c")

def test_concurrent_budget(tmp_path):
    ledger=Ledger(tmp_path/"cost.db",Budget(max_total_tokens=1000,per_cell_tokens=1000,max_attempts=100))
    for i in range(20):ledger.claim(str(i),str(i),str(i))
    def one(i):
        try:ledger.reserve(str(i),str(i),"p",200,0);return True
        except LabError:return False
    with ThreadPoolExecutor(max_workers=10) as pool:results=list(pool.map(one,range(20)))
    assert sum(results)==5 and ledger.totals()["accounted_tokens"]==1000

def test_unknown_reserved(ledger):
    ledger.claim("a","h","c");a=ledger.reserve("a","c","p",500,.02)
    ledger.settle(a,tokens=None,usd=None)
    assert ledger.totals()["accounted_tokens"]==500 and ledger.totals()["unknown_attempts"]==1
    ledger.reconcile(a,123,.005,"provider invoice request #123")
    assert ledger.totals()["known_tokens"]==123
    with pytest.raises(LabError,match="in_doubt"):ledger.claim("a","h","c")

def test_currency_bound(tmp_path):
    l=Ledger(tmp_path/"d",Budget(max_total_usd=.1))
    l.claim("a","h","c")
    with pytest.raises(LabError,match="budget"):l.reserve("a","c","p",100,.2)

def test_double_settlement(ledger):
    ledger.claim("a","h","c");aid=ledger.reserve("a","c","p",100,0)
    ledger.settle(aid,tokens=20,usd=0)
    with pytest.raises(LabError,match="ledger_state"):ledger.settle(aid,tokens=20,usd=0)

@pytest.mark.parametrize("tokens,usd",[(101,0),(50,.2)])
def test_underestimate_latches(ledger,tokens,usd):
    ledger.claim("a","h","c");aid=ledger.reserve("a","c","p",100,.1)
    ledger.settle(aid,tokens=tokens,usd=usd)
    with pytest.raises(LabError,match="reservation_breach"):ledger.throttle_delay("p",1,1000,100000)

def test_frozen_profile(ledger):
    ledger.bind("profile",{"a":1})
    with pytest.raises(LabError,match="identity_changed"):ledger.bind("profile",{"a":2})

def test_model_drift(ledger):
    ledger.observed_model("p","a",None)
    with pytest.raises(LabError,match="model_drift"):ledger.observed_model("p","b",None)

def test_rate_and_circuit(ledger):
    ledger.claim("a","h","c");ledger.reserve("a","c","p",100,0)
    assert ledger.throttle_delay("p",1,1,1000)>0
    with pytest.raises(LabError,match="rate_configuration"):ledger.throttle_delay("p",2000,1,1000)
    ledger.cooldown("x",0,failure=True,threshold=1,circuit_seconds=3)
    assert ledger.throttle_delay("x",1,1000,100000)>2
