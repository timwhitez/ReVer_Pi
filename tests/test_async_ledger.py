"""SQLite contention tests use actual local connections, never a paid provider."""
import asyncio
import sqlite3
import threading
import time

import httpx
import pytest

from reverpi.errors import LabError
from reverpi.protocols import Message
from reverpi.transport import APIClient
from reverpi.async_ledger import AsyncLedger, Operation
from reverpi.ledger import Ledger
from reverpi.transport_trace import record_transport_trace
from test_transport import raw


@pytest.mark.asyncio
async def test_external_writer_does_not_block_heartbeat_or_deadline(provider, ledger):
    p = provider.model_copy(update={'retry': provider.retry.model_copy(update={'total_seconds': .08})})
    client = APIClient(p, ledger, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw(p))))
    writer = sqlite3.connect(ledger.path, check_same_thread=False, isolation_level=None)
    writer.execute('BEGIN IMMEDIATE')
    # Release from a different thread: the regression must fail promptly even if
    # synchronous SQLite blocks the event loop and prevents an async release.
    release = threading.Timer(.35, writer.execute, args=('ROLLBACK',))
    release.start()
    ticks = [time.monotonic()]
    async def heartbeat():
        for _ in range(12):
            ticks.append(time.monotonic())
            await asyncio.sleep(.01)
    started = time.monotonic()
    pulse = asyncio.create_task(heartbeat())
    try:
        error = None
        try:
            await client.complete([Message('user', 'x')], op='locked', cell='c')
        except LabError as err:
            error = err
        elapsed = time.monotonic() - started
        await pulse
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < .12
        assert elapsed < .25
        assert error is not None and error.kind == 'total_timeout'
    finally:
        release.join()
        writer.close()
        await client.close()
    assert ledger.totals()['attempts'] == 0


async def wait_thread_event(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(.002)


@pytest.mark.asyncio
async def test_actual_claim_lock_keeps_heartbeat_and_queued_cancel_live(ledger):
    worker = AsyncLedger(ledger)
    writer = sqlite3.connect(ledger.path, check_same_thread=False, isolation_level=None)
    writer.execute('BEGIN IMMEDIATE')
    release = threading.Timer(.35, writer.execute, args=('ROLLBACK',))
    release.start()
    start = time.monotonic()
    ticks, cancellation = [start], []
    sleeper = asyncio.create_task(asyncio.sleep(10))
    async def beat():
        for _ in range(15):
            ticks.append(time.monotonic())
            await asyncio.sleep(.01)
    async def cancel_queued():
        await asyncio.sleep(.02)
        sleeper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sleeper
        cancellation.append(time.monotonic() - start)
    pulse, cancel = asyncio.create_task(beat()), asyncio.create_task(cancel_queued())
    owner = Operation(start + .08)
    try:
        with pytest.raises(TimeoutError):
            await worker.call(owner, 'claim', 'locked-claim', 'h', 'c')
        assert time.monotonic() - start < .25
        await asyncio.gather(pulse, cancel)
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < .12
        assert cancellation[0] < .12
    finally:
        release.join()
        writer.close()
        await worker.close()
    with ledger.db() as db:
        assert db.execute("SELECT COUNT(*) FROM operations WHERE op='locked-claim'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_release_before_deadline_claims_and_dispatches_once(provider, ledger):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=raw(provider))
    async with APIClient(provider, ledger, transport=httpx.MockTransport(handler)) as client:
        writer = sqlite3.connect(ledger.path, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        task = asyncio.create_task(client.complete([Message('user', 'x')], op='once', cell='c'))
        await asyncio.sleep(.04)
        writer.execute('ROLLBACK')
        writer.close()
        assert (await task).text == 'ok'
        assert (await client.complete([Message('user', 'x')], op='once', cell='c')).text == 'ok'
    assert len(seen) == 1
    assert ledger.totals()['attempts'] == 1 and ledger.totals()['known_tokens'] == 18


@pytest.mark.asyncio
async def test_op_lock_wait_is_in_total_budget(provider, ledger):
    p = provider.model_copy(update={'retry': provider.retry.model_copy(update={'total_seconds': .06})})
    async with APIClient(p, ledger) as client:
        lock = asyncio.Lock()
        client.op_locks['same'] = lock
        await lock.acquire()
        start = time.monotonic()
        with pytest.raises(LabError) as err:
            await client.complete([Message('user', 'x')], op='same', cell='c')
        assert time.monotonic() - start < .2
        assert err.value.kind == 'total_timeout'
        lock.release()
    with ledger.db() as db:
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cancel_waiting_begin_is_isolated_and_worker_remains_live(provider, ledger):
    async with APIClient(provider, ledger) as client:
        writer = sqlite3.connect(ledger.path, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        cancelled = asyncio.create_task(client.complete([Message('user', 'x')], op='cancel', cell='c'))
        survivor = asyncio.create_task(client.complete([Message('user', 'x')], op='survive', cell='c'))
        await asyncio.sleep(.04)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, .2)
        writer.execute('ROLLBACK')
        writer.close()
        assert (await survivor).text
        assert (await client.complete([Message('user', 'x')], op='later', cell='c')).text
    assert {a['op'] for a in ledger.attempts()} == {'survive', 'later'}
    assert client.async_ledger.thread is not None and not client.async_ledger.thread.is_alive()


@pytest.mark.asyncio
async def test_bounded_queue_cancel_before_start_and_backpressure(ledger):
    worker = AsyncLedger(ledger, max_pending=2)
    entered, release = threading.Event(), threading.Event()
    owners = [Operation(time.monotonic() + 2) for _ in range(3)]
    def hold():
        entered.set()
        assert release.wait(2)
    first = asyncio.create_task(worker.call(owners[0], hold))
    await wait_thread_event(entered)
    cancelled = asyncio.create_task(worker.call(owners[1], 'claim', 'cancel', 'h', 'c'))
    survivor = asyncio.create_task(worker.call(owners[2], 'claim', 'survive', 'h', 'c'))
    await asyncio.sleep(.02)
    assert len(worker.jobs) == 2 and len(owners[2].jobs) == 0
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    await first
    await worker.drain(owners[1])
    assert await survivor is None
    await worker.close()
    with ledger.db() as db:
        assert [r['op'] for r in db.execute('SELECT op FROM operations')] == ['survive']
    with pytest.raises(LabError, match='closing'):
        await worker.call(Operation(time.monotonic() + 1), 'totals')
    assert not worker.thread.is_alive()


@pytest.mark.asyncio
async def test_cancel_after_begin_rolls_back_before_another_op(ledger):
    worker = AsyncLedger(ledger)
    owner = Operation(time.monotonic() + 2)
    begun, release = threading.Event(), threading.Event()
    def transaction():
        with ledger.db(True) as db:
            db.execute("INSERT INTO meta VALUES ('cancel-test','true')")
            begun.set()
            assert release.wait(2)
    task = asyncio.create_task(worker.call(owner, transaction))
    await wait_thread_event(begun)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await worker.drain(owner)
    assert isinstance(owner.jobs[0].future.exception(), TimeoutError)
    await worker.call(Operation(time.monotonic() + 1), 'claim', 'other', 'h', 'c')
    await worker.close()
    with ledger.db() as db:
        assert db.execute("SELECT value FROM meta WHERE key='cancel-test'").fetchone() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['claim', 'reserve_gated', 'finish'])
async def test_cancel_after_commit_accepts_own_actual_outcome(provider, ledger, monkeypatch, stage):
    committed, release = threading.Event(), threading.Event()
    original = getattr(ledger, stage)
    def delayed(*args, **kwargs):
        value = original(*args, **kwargs)
        committed.set()
        assert release.wait(2)
        return value
    monkeypatch.setattr(ledger, stage, delayed)
    client = APIClient(provider, ledger,
                       transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw(provider))))
    task = asyncio.create_task(client.complete([Message('user', 'x')], op='committed', cell='c'))
    try:
        await wait_thread_event(committed)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()  # It retains the committed job until the result is known.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await client.close()
    with ledger.db() as db:
        state = db.execute("SELECT state FROM operations WHERE op='committed'").fetchone()[0]
    assert state == ('complete' if stage == 'finish' else 'failed')
    attempts = ledger.attempts()
    assert len(attempts) == (0 if stage == 'claim' else 1)
    if stage == 'reserve_gated':
        assert attempts[0]['actual_tokens'] is None and attempts[0]['state'] == 'unknown'
    if stage == 'finish':
        assert attempts[0]['actual_tokens'] == 18


@pytest.mark.asyncio
async def test_cancel_during_known_settlement_keeps_usage(provider, ledger, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = ledger.settle
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(ledger, 'settle', delayed)
    client = APIClient(provider, ledger, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw(provider))))
    task = asyncio.create_task(client.complete([Message('user', 'x')], op='usage', cell='c'))
    try:
        await wait_thread_event(entered)
        task.cancel()
        task.cancel()  # Repeated cancellation does not abandon the accounting job.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await client.close()
    assert ledger.totals()['known_tokens'] == 18 and ledger.totals()['unknown_attempts'] == 0


@pytest.mark.asyncio
async def test_cleanup_deadline_preserves_unknown_restart_in_doubt(provider, ledger):
    writer, cleanup_started = None, None
    def handler(request):
        nonlocal writer, cleanup_started
        writer = sqlite3.connect(ledger.path, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        cleanup_started = time.monotonic()
        raise httpx.ReadError('mock interruption')
    client = APIClient(provider, ledger, transport=httpx.MockTransport(handler), ledger_cleanup_seconds=.08)
    try:
        with pytest.raises(LabError) as err:
            await client.complete([Message('user', 'x')], op='unknown', cell='c')
        assert err.value.kind == 'ledger_cleanup_timeout'
        assert time.monotonic() - cleanup_started < .25
    finally:
        writer.execute('ROLLBACK')
        writer.close()
        await client.close()
    reopened = Ledger.open_existing(ledger.path)
    assert reopened.totals()['unknown_attempts'] == 1 and reopened.totals()['accounted_tokens'] > 0
    with reopened.db() as db:
        payload = db.execute("SELECT payload_sha FROM operations WHERE op='unknown'").fetchone()[0]
    with pytest.raises(LabError, match='in_doubt'):
        reopened.claim('unknown', payload, 'c')


@pytest.mark.asyncio
async def test_close_cancels_owned_requests_drains_and_rejects_new(provider, ledger):
    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.Event().wait()
    client = APIClient(provider, ledger, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(client.complete([Message('user', 'x')], op='close', cell='c'))
    await entered.wait()
    await client.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(LabError, match='closing'):
        await client.complete([Message('user', 'x')], op='new', cell='c')
    await client.close()
    assert not client.async_ledger.thread.is_alive() and not client.async_ledger.jobs
    assert ledger.totals()['unknown_attempts'] == 1


@pytest.mark.asyncio
async def test_reserve_wait_has_execution_deadline_and_separate_cleanup(provider, ledger, monkeypatch):
    p = provider.model_copy(update={'retry': provider.retry.model_copy(update={'total_seconds': .3})})
    writer, release = None, None
    original = ledger.claim
    def claim(*args, **kwargs):
        nonlocal writer, release
        value = original(*args, **kwargs)
        writer = sqlite3.connect(ledger.path, check_same_thread=False, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        release = threading.Timer(.6, writer.execute, args=('ROLLBACK',))
        release.start()
        return value
    monkeypatch.setattr(ledger, 'claim', claim)
    client = APIClient(p, ledger, transport=httpx.MockTransport(lambda r: pytest.fail('no dispatch')),
                       ledger_cleanup_seconds=1)
    start = time.monotonic()
    try:
        with pytest.raises(LabError) as err:
            await client.complete([Message('user', 'x')], op='reserve', cell='c')
        # Durable finish needs the separate cleanup allowance until the external
        # writer releases. Execution is still bounded well below SQLite's 30s.
        assert .45 < time.monotonic() - start < p.retry.total_seconds + 1 + .2
        assert err.value.kind == 'total_timeout'
    finally:
        if release:
            release.join()
            writer.close()
        await client.close()
    assert ledger.totals()['attempts'] == 0
    with ledger.db() as db:
        assert db.execute("SELECT state FROM operations WHERE op='reserve'").fetchone()[0] == 'failed'


@pytest.mark.asyncio
async def test_claim_time_is_not_reset_for_network_phase(provider, ledger, monkeypatch):
    original = ledger.claim
    def delayed(*args, **kwargs):
        time.sleep(.2)
        return original(*args, **kwargs)
    monkeypatch.setattr(ledger, 'claim', delayed)
    p = provider.model_copy(update={'retry': provider.retry.model_copy(update={'total_seconds': .5})})
    async def handler(request):
        await asyncio.sleep(.4)
        return httpx.Response(200, json=raw(p))
    async with APIClient(p, ledger, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LabError) as err:
            await client.complete([Message('user', 'x')], op='one-budget', cell='c')
        assert err.value.kind == 'total_timeout'
    assert ledger.totals()['attempts'] == 1 and ledger.totals()['unknown_attempts'] == 1


@pytest.mark.asyncio
async def test_queue_backpressure_uses_owner_deadline(ledger):
    worker = AsyncLedger(ledger, max_pending=1)
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(2)
    first = asyncio.create_task(worker.call(Operation(time.monotonic() + 2), hold))
    await wait_thread_event(entered)
    waiting = Operation(time.monotonic() + .04)
    try:
        with pytest.raises(TimeoutError):
            await worker.call(waiting, 'claim', 'over-capacity', 'h', 'c')
        assert len(worker.jobs) == 1 and waiting.jobs == []
    finally:
        release.set()
        await first
        await worker.close()


@pytest.mark.asyncio
async def test_cancelled_close_retains_its_drain_and_join(provider, ledger):
    entered = asyncio.Event()
    writer = None
    async def handler(request):
        nonlocal writer
        writer = sqlite3.connect(ledger.path, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        entered.set()
        await asyncio.Event().wait()
    client = APIClient(provider, ledger, transport=httpx.MockTransport(handler))
    request = asyncio.create_task(client.complete([Message('user', 'x')], op='closing', cell='c'))
    await entered.wait()
    close = asyncio.create_task(client.close())
    await asyncio.sleep(.01)
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert client.close_task is not None and not client.close_task.done()
    with pytest.raises(LabError, match='closing'):
        await client.complete([Message('user', 'x')], op='new', cell='c')
    writer.execute('ROLLBACK')
    writer.close()
    await client.close()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not client.async_ledger.thread.is_alive()


@pytest.mark.asyncio
async def test_cancel_during_settle_preserves_received_model_drift(provider, ledger, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = ledger.settle
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(ledger, 'settle', delayed)
    p = provider.model_copy(update={'expected_response_model': provider.model})
    response = raw(p)
    response['model'] = 'wrong-mock-model'
    client = APIClient(p, ledger, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response)))
    task = asyncio.create_task(client.complete([Message('user', 'x')], op='drift-cancel', cell='c'))
    try:
        await wait_thread_event(entered)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await client.close()
    assert ledger.totals()['known_tokens'] == 18
    assert ledger.stop_reason() == 'model_drift'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 403])
async def test_accounting_calls_share_one_cleanup_budget(provider, ledger, monkeypatch, status):
    """Three actual lock waits cannot each restart the cleanup allowance."""
    response = raw(provider) if status == 200 else {'error': {'message': 'mock permission'}}
    client = APIClient(provider, ledger, ledger_cleanup_seconds=1,
                       transport=httpx.MockTransport(lambda r: httpx.Response(status, json=response)))
    original = client._ledger_call
    writers, timers = [], []
    async def blocked(method, *args, **kwargs):
        if method in ('settle', 'finish') or method is record_transport_trace:
            writer = sqlite3.connect(ledger.path, check_same_thread=False, isolation_level=None)
            writer.execute('BEGIN IMMEDIATE')
            timer = threading.Timer(.4, writer.execute, args=('ROLLBACK',))
            writers.append(writer)
            timers.append(timer)
            timer.start()
        return await original(method, *args, **kwargs)
    monkeypatch.setattr(client, '_ledger_call', blocked)
    try:
        with pytest.raises(LabError) as err:
            await client.complete([Message('user', 'x')], op='shared-cleanup', cell='c')
        assert err.value.kind == 'ledger_cleanup_timeout'
    finally:
        for timer in timers:
            timer.join()
        for writer in writers:
            writer.close()
        await client.close()
    assert ledger.attempts()[0]['actual_tokens'] == (18 if status == 200 else 0)
    with ledger.db() as db:
        assert db.execute("SELECT state FROM operations WHERE op='shared-cleanup'").fetchone()[0] == 'running'
