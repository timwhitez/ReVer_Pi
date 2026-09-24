"""Real process contention on one SQLite file; no network/model requests."""
import multiprocessing as mp
from reverpi.config import Budget
from reverpi.ledger import Ledger


def reserve_worker(path, ready, results, i):
    try:
        ledger=Ledger(path,Budget(min_free_disk_bytes=0,max_total_tokens=1000000,max_attempts=100))
        ledger.claim(f'op{i}',f'payload{i}',f'cell{i}')
        ready.wait(timeout=15)
        aid,delay=ledger.reserve_gated(f'op{i}',f'cell{i}','shared-provider',100,0.0,3,100000)
        results.put({'admitted':aid is not None,'delay':delay})
    except BaseException as exc:
        results.put({'error':type(exc).__name__})


def test_eight_real_processes_reserve_only_three_provider_slots(tmp_path):
    path=tmp_path/'budget.sqlite'
    Ledger(path,Budget(min_free_disk_bytes=0,max_total_tokens=1000000,max_attempts=100))
    ctx=mp.get_context('spawn');barrier=ctx.Barrier(8);results=ctx.Queue()
    workers=[ctx.Process(target=reserve_worker,args=(path,barrier,results,i)) for i in range(8)]
    try:
        for worker in workers:worker.start()
        rows=[results.get(timeout=25) for _ in workers]
        for worker in workers:worker.join(timeout=10)
        assert all(worker.exitcode==0 for worker in workers)
        assert all('error' not in row for row in rows),rows
        assert sum(row['admitted'] for row in rows)==3
        assert all(row['delay']>0 for row in rows if not row['admitted'])
        assert len(Ledger(path,Budget(min_free_disk_bytes=0,max_total_tokens=1000000,max_attempts=100)).attempts())==3
    finally:
        for worker in workers:
            if worker.pid is not None and worker.is_alive():worker.terminate();worker.join(timeout=5)
        results.close();results.join_thread()
