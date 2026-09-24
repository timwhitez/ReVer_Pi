import pytest
from reverpi.config import Provider,Budget
from reverpi.ledger import Ledger

@pytest.fixture
def provider():
    return Provider(name="test",mock=True,base_url="http://127.0.0.1:1/v1",model="mock-reasoner",
                    requests_per_minute=100000,tokens_per_minute=100000000,
                    retry={"base_seconds":0,"cap_seconds":0,"total_seconds":10})

@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path/"ledger.sqlite",Budget(max_total_tokens=10000000,per_cell_tokens=2000000,max_attempts=1000))
