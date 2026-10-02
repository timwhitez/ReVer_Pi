"""SQLite contention tests use actual local connections, never a paid provider."""
import asyncio
import contextlib
import json
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
from test_transport import raw, ByteStream, event


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


@pytest.mark.asyncio
async def test_submitted_business_job_wait_obeys_its_deadline(ledger):
    worker = AsyncLedger(ledger)
    entered = threading.Event()
    first_owner = Operation(time.monotonic() + 3)
    second_owner = Operation(time.monotonic() + 3)
    writer = sqlite3.connect(ledger.path, check_same_thread=False, isolation_level=None)
    writer.execute('BEGIN IMMEDIATE')
    safety_release = threading.Timer(.6, writer.rollback)
    safety_release.start()
    def blocked_claim():
        entered.set()
        return ledger.claim('blocking-owner', 'h', 'c')
    first = asyncio.create_task(worker.call(first_owner, blocked_claim))
    try:
        await wait_thread_event(entered)
        second_owner.deadline = time.monotonic() + .08
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await worker.call(second_owner, 'claim', 'queued-owner', 'h', 'c')
        assert time.monotonic() - start < .25
        job = second_owner.jobs[0]
        assert job in worker.jobs and not job.future.done() and not job.future.cancelled()
        assert job.cancelled.is_set() and not first_owner.cancelled
        writer.rollback()
        assert await first is None
        await worker.drain(second_owner)
        assert isinstance(job.future.exception(), TimeoutError)
    finally:
        safety_release.cancel()
        safety_release.join()
        writer.rollback()
        writer.close()
        await worker.close()
    with ledger.db() as db:
        assert [r[0] for r in db.execute('SELECT op FROM operations')] == ['blocking-owner']
    assert not worker.thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_caller', [False, True])
async def test_queued_cleanup_deadline_isolated_from_other_sqlite_owner(provider, ledger, monkeypatch, cancel_caller):
    response_ready, send_response = asyncio.Event(), asyncio.Event()
    blocking_claim = threading.Event()
    seen, owners = [], {}
    original_claim = ledger.claim
    def claim(op, *args):
        if op == 'blocking':
            blocking_claim.set()
        return original_claim(op, *args)
    monkeypatch.setattr(ledger, 'claim', claim)
    async def handler(request):
        text = json.loads(request.content)['messages'][0]['content']
        seen.append(text)
        if text == 'response':
            response_ready.set()
            await send_response.wait()
        return httpx.Response(200, json=raw(provider))
    client = APIClient(provider, ledger, transport=httpx.MockTransport(handler))
    original_call = client.async_ledger.call
    async def record_owner(owner, method, *args, **kwargs):
        if method == 'claim':
            owners[args[0]] = owner
        return await original_call(owner, method, *args, **kwargs)
    monkeypatch.setattr(client.async_ledger, 'call', record_owner)
    response = asyncio.create_task(client.complete([Message('user', 'response')], op='response', cell='c'))
    writer, safety_release, blocking = None, None, None
    try:
        await asyncio.wait_for(response_ready.wait(), 3)
        writer = sqlite3.connect(ledger.path, check_same_thread=False, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        safety_release = threading.Timer(3, writer.rollback)
        safety_release.start()
        blocking = asyncio.create_task(client.complete([Message('user', 'blocking')], op='blocking', cell='c'))
        await wait_thread_event(blocking_claim)
        start = time.monotonic()
        send_response.set()
        if cancel_caller:
            while not any(j.fn == ledger.settle for j in owners['response'].jobs):
                await asyncio.sleep(.002)
            response.cancel()
            with pytest.raises(asyncio.CancelledError):
                await response
        else:
            with pytest.raises(LabError) as err:
                await response
            assert err.value.kind == 'ledger_cleanup_timeout' and err.value.ambiguous
        assert time.monotonic() - start < client.async_ledger.cleanup_seconds + .35
        pending = [j for j in owners['response'].jobs if not j.future.done()]
        assert pending and all(j in client.async_ledger.jobs and not j.future.cancelled() for j in pending)
        settlement = next(j for j in pending if j.fn == ledger.settle)
        # Received usage is still owned as job evidence, but it is not durable.
        assert settlement.kwargs['tokens'] == 18 and settlement.cancelled.is_set()
        assert not owners['blocking'].cancelled and not blocking.done()
        with ledger.db() as db:
            operation = db.execute("SELECT state FROM operations WHERE op='response'").fetchone()[0]
            attempt = db.execute("SELECT state,actual_tokens FROM attempts WHERE op='response'").fetchone()
        assert operation == 'running' and attempt['state'] == 'reserved' and attempt['actual_tokens'] is None
        writer.rollback()
        assert (await blocking).text == 'ok'
        with pytest.raises(LabError, match='in_doubt'):
            await client.complete([Message('user', 'response')], op='response', cell='c')
        assert seen == ['response', 'blocking']
        assert (await client.complete([Message('user', 'later')], op='later', cell='c')).text == 'ok'
    finally:
        send_response.set()
        if safety_release:
            safety_release.cancel()
            safety_release.join()
        if writer:
            writer.rollback()
            writer.close()
        await client.close()
    assert not client.async_ledger.thread.is_alive() and not client.async_ledger.jobs
    attempts = {a['op']: a for a in ledger.attempts()}
    assert attempts['response']['state'] == 'reserved' and attempts['response']['actual_tokens'] is None
    assert attempts['blocking']['actual_tokens'] == 18 and attempts['later']['actual_tokens'] == 18


@pytest.mark.asyncio
async def test_cleanup_expiry_after_commit_keeps_actual_future_and_close_joins(provider, ledger, monkeypatch):
    committed, release = threading.Event(), threading.Event()
    original_finish = ledger.finish
    def finish(*args, **kwargs):
        result = original_finish(*args, **kwargs)
        committed.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(ledger, 'finish', finish)
    client = APIClient(provider, ledger, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw(provider))))
    owner = None
    original_call = client.async_ledger.call
    async def call(op_owner, method, *args, **kwargs):
        nonlocal owner
        owner = op_owner
        return await original_call(op_owner, method, *args, **kwargs)
    monkeypatch.setattr(client.async_ledger, 'call', call)
    task = asyncio.create_task(client.complete([Message('user', 'x')], op='postcommit', cell='c'))
    try:
        await wait_thread_event(committed)
        with pytest.raises(LabError) as err:
            await task
        assert err.value.kind == 'ledger_cleanup_timeout' and err.value.ambiguous
        job = next(j for j in owner.jobs if j.fn == ledger.finish)
        assert job in client.async_ledger.jobs and not job.future.done() and not job.future.cancelled()
        # The waiter timed out after COMMIT. Durable results remain authoritative;
        # no second finish, zeroing or automatic paid replay is allowed.
        with ledger.db() as db:
            row = db.execute("SELECT state,payload_sha FROM operations WHERE op='postcommit'").fetchone()
        assert row['state'] == 'complete' and ledger.totals()['known_tokens'] == 18
        assert ledger.claim('postcommit', row['payload_sha'], 'c')['text'] == 'ok'
        close = asyncio.create_task(client.close())
        await asyncio.sleep(.01)
        assert not close.done()  # The noncooperative post-COMMIT job is still real.
        release.set()
        await close
        assert job.future.done() and job.future.exception() is None
        assert not client.async_ledger.thread.is_alive() and not client.async_ledger.jobs
    finally:
        release.set()
        await client.close()


@pytest.mark.asyncio
async def test_atomic_drift_stop_rollback_is_not_reported_as_committed(ledger, monkeypatch):
    worker = AsyncLedger(ledger, cleanup_seconds=.15)
    owner = Operation(time.monotonic() + 3)
    written, release = threading.Event(), threading.Event()
    original_db = ledger.db
    @contextlib.contextmanager
    def before_commit(*args, **kwargs):
        with original_db(*args, **kwargs) as db:
            yield db
            if db.in_transaction and db.execute("SELECT 1 FROM meta WHERE key='operator_stop'").fetchone():
                written.set()
                assert release.wait(3)
    monkeypatch.setattr(ledger, 'db', before_commit)
    task = asyncio.create_task(worker.call(owner, 'observed_model', 'p', 'wrong', 'expected',
                                           stop_on_drift=True, cleanup=True))
    try:
        await wait_thread_event(written)
        with pytest.raises(TimeoutError):
            await task
        job = owner.jobs[0]
        assert not job.future.done() and job in worker.jobs
        assert ledger.stop_reason() is None  # Uncommitted stop is not durable.
    finally:
        release.set()
        await worker.close()
    assert isinstance(job.future.exception(), TimeoutError)
    assert ledger.stop_reason() is None and not worker.thread.is_alive()


@pytest.mark.asyncio
async def test_atomic_drift_stop_committed_before_late_error_result(ledger, monkeypatch):
    worker = AsyncLedger(ledger, cleanup_seconds=.3)
    owner = Operation(time.monotonic() + 3)
    committed, release = threading.Event(), threading.Event()
    original_observe = ledger.observed_model
    def observe(*args, **kwargs):
        try:
            return original_observe(*args, **kwargs)
        except LabError as err:
            assert err.kind == 'model_drift'
            committed.set()  # The combined action raises only after stop COMMIT.
            assert release.wait(3)
            raise
    monkeypatch.setattr(ledger, 'observed_model', observe)
    task = asyncio.create_task(worker.call(owner, 'observed_model', 'p', 'wrong', 'expected',
                                           stop_on_drift=True, cleanup=True))
    try:
        await wait_thread_event(committed)
        with pytest.raises(TimeoutError):
            await task
        job = owner.jobs[0]
        assert job in worker.jobs and not job.future.done() and not job.future.cancelled()
        assert ledger.stop_reason() == 'model_drift'
    finally:
        release.set()
        await worker.close()
    assert isinstance(job.future.exception(), LabError) and job.future.exception().kind == 'model_drift'
    assert ledger.stop_reason() == 'model_drift' and not worker.thread.is_alive()


@pytest.mark.asyncio
async def test_parser_drift_survives_cancel_during_http_stream_close(provider, ledger):
    closing, release_close = asyncio.Event(), asyncio.Event()
    class DriftStream(ByteStream):
        async def aclose(self):
            closing.set()
            await release_close.wait()
    data = event({'model':provider.model,'choices':[]}) + event({'model':'changed','choices':[]})
    # Force a full read chunk so parser drift precedes EOF auto-close/flush.
    data += b': padding ' + b'x' * 65536 + b'\n\n'
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=DriftStream(data, cut=65536))
    client = APIClient(provider, ledger, transport=httpx.MockTransport(handler))
    try:
        task = asyncio.create_task(client.complete([Message('user','x')], op='drift', cell='c'))
        await asyncio.wait_for(closing.wait(), 3)
        assert client.model_drift_detected
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert ledger.stop_reason() == 'model_drift'
        with pytest.raises(LabError) as stopped:
            await client.complete([Message('user','later')], op='later', cell='c')
        assert stopped.value.kind == 'run_stopped' and len(seen) == 1
        assert ledger.totals()['unknown_attempts'] == 1
    finally:
        release_close.set()
        await client.close()


@pytest.mark.asyncio
async def test_parser_drift_storage_timeout_retains_local_latch_and_reserved_usage(provider, ledger):
    writer = sqlite3.connect(ledger.path, isolation_level=None)
    class DriftStream(ByteStream):
        async def aclose(self):
            writer.execute('BEGIN IMMEDIATE')
    data = event({'model':provider.model,'choices':[]}) + event({'model':'changed','choices':[]})
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=DriftStream(data))
    client = APIClient(provider, ledger, transport=httpx.MockTransport(handler), ledger_cleanup_seconds=.15)
    try:
        started = time.monotonic()
        with pytest.raises(LabError) as interrupted:
            await client.complete([Message('user','x')], op='drift', cell='c')
        assert interrupted.value.kind == 'ledger_cleanup_timeout'
        assert time.monotonic() - started < .7
        assert client.model_drift_detected and ledger.stop_reason() is None
        attempt = ledger.attempts()[0]
        assert attempt['state'] == 'reserved' and attempt['actual_tokens'] is None
        writer.rollback()
        with pytest.raises(LabError) as stopped:
            await client.complete([Message('user','later')], op='later', cell='c')
        assert stopped.value.kind == 'run_stopped' and len(seen) == 1
        assert ledger.stop_reason() is None  # Local refusal never fabricates a durable stop.
    finally:
        writer.rollback()
        writer.close()
        await client.close()
