"""Revocation must stop queued work before any upstream attempt (issue #9).

Every Provider here is an httpx.MockTransport double; usage figures are synthetic.
"""
import asyncio
import sqlite3
import time
import threading
import httpx
import pytest
from reverpi.config import StudyConfig
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.transport import APIClient, DISPATCH_GUARD
from reverpi.protocols import Message
from test_transport import raw


def queued_app(provider, tmp_path, early):
    """Concurrency 1; session 'a' holds the only slot until `release` is set."""
    seen, release, holding = [], asyncio.Event(), asyncio.Event()

    async def handler(request):
        cell = request.headers.get('x-test-cell') or ('a' if not seen else 'b')
        seen.append(cell)
        if cell == 'a':
            holding.set()
            await release.wait()
        return httpx.Response(200, json=raw(provider))

    app = create_app(provider.model_copy(update={'concurrency': 1}),
                     StudyConfig(methods=['mask'], early_response_headers=early),
                     tmp_path / 'g', transport=httpx.MockTransport(handler))
    tokens = {sid: app.state.sessions.create(sid, 'mask') for sid in ('a', 'b')}
    return app, tokens, seen, release, holding


def body(op):
    return {'op': op, 'messages': [{'role': 'user', 'content': op}]}


def attempts_for(app, cell):
    with sqlite3.connect(app.state.ledger.path) as db:
        return db.execute('SELECT COUNT(*) FROM attempts WHERE cell=?', (cell,)).fetchone()[0]


async def wait_queued(app):
    # B has been claimed in the ledger (so it passed authentication) but holds no attempt.
    for _ in range(200):
        with sqlite3.connect(app.state.ledger.path) as db:
            if db.execute("SELECT COUNT(*) FROM operations WHERE cell='b'").fetchone()[0]:
                return
        await asyncio.sleep(0.01)
    raise AssertionError('B never reached the dispatch queue')


@pytest.mark.asyncio
@pytest.mark.parametrize('early', [False, True])
@pytest.mark.parametrize('how', ['revoke', 'expire'])
async def test_queued_request_of_revoked_session_is_never_dispatched(provider, tmp_path, early, how):
    app, tokens, seen, release, holding = queued_app(provider, tmp_path, early)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
            first = asyncio.create_task(c.post('/complete', json=body('one'),
                                               headers={'Authorization': 'Bearer ' + tokens['a']}))
            await holding.wait()
            second = asyncio.create_task(c.post('/complete', json=body('two'),
                                                headers={'Authorization': 'Bearer ' + tokens['b']}))
            await wait_queued(app)
            assert seen == ['a']
            if how == 'revoke':
                app.state.sessions.disable('b')
            else:
                with app.state.sessions.db() as db:
                    db.execute("UPDATE sessions SET expires=? WHERE id='b'", (time.time() - 1,))
            release.set()
            ok, refused = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert ok.json()['text'] == 'ok'
        assert refused.json()['error']['kind'] == 'session_revoked'
        assert seen == ['a']
        assert attempts_for(app, 'b') == 0 and attempts_for(app, 'a') == 1
    finally:
        await app.state.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('early', [False, True])
async def test_queued_request_of_live_session_still_completes(provider, tmp_path, early):
    app, tokens, seen, release, holding = queued_app(provider, tmp_path, early)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
            first = asyncio.create_task(c.post('/complete', json=body('one'),
                                               headers={'Authorization': 'Bearer ' + tokens['a']}))
            await holding.wait()
            second = asyncio.create_task(c.post('/complete', json=body('two'),
                                                headers={'Authorization': 'Bearer ' + tokens['b']}))
            await wait_queued(app)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert [r.json()['text'] for r in results] == ['ok', 'ok']
        assert seen == ['a', 'b']
    finally:
        await app.state.client.close()


@pytest.mark.asyncio
async def test_guard_rechecked_after_rate_limit_wait(provider, ledger):
    """A session revoked while its request sleeps for a rate slot gets no attempt."""
    seen, revoked = [], []

    def handler(request):
        seen.append(1)
        return httpx.Response(200, json=raw(provider))

    def guard():
        if revoked:
            raise LabError('session_revoked', 'revoked before dispatch')

    async def sleeper(delay):
        revoked.append(True)  # Revocation lands while the request waits for a slot.

    limited = provider.model_copy(update={'requests_per_minute': 1})
    async with APIClient(limited, ledger, transport=httpx.MockTransport(handler), sleeper=sleeper) as client:
        token = DISPATCH_GUARD.set(guard)
        try:
            await client.complete([Message('user', 'first')], op='o1', cell='c')
            with pytest.raises(LabError) as err:
                await client.complete([Message('user', 'second')], op='o2', cell='c')
        finally:
            DISPATCH_GUARD.reset(token)
    assert err.value.kind == 'session_revoked' and not err.value.ambiguous
    assert len(seen) == 1 and ledger.totals()['attempts'] == 1


@pytest.mark.asyncio
async def test_guard_rechecked_before_each_retry(provider, ledger):
    """Retries are new dispatches: revocation between attempts stops the next one."""
    seen, revoked = [], []

    def handler(request):
        seen.append(1)
        revoked.append(True)
        return httpx.Response(429, json={'error': {'message': 'slow down'}})

    def guard():
        if revoked:
            raise LabError('session_revoked', 'revoked before dispatch')

    retrying = provider.model_copy(update={'retry': provider.retry.model_copy(update={'max_attempts': 3})})
    async with APIClient(retrying, ledger, transport=httpx.MockTransport(handler)) as client:
        token = DISPATCH_GUARD.set(guard)
        try:
            with pytest.raises(LabError) as err:
                await client.complete([Message('user', 'x')], op='o', cell='c')
        finally:
            DISPATCH_GUARD.reset(token)
    assert err.value.kind == 'session_revoked'
    assert len(seen) == 1 and ledger.totals()['attempts'] == 1


@pytest.mark.asyncio
async def test_revocation_after_dispatch_keeps_the_actual_record(provider, tmp_path):
    """Already-dispatched work keeps its known usage; nothing is zeroed or re-bought."""
    app = None

    async def handler(request):
        app.state.sessions.disable('s')
        return httpx.Response(200, json=raw(provider))

    app = create_app(provider, StudyConfig(methods=['mask']), tmp_path / 'g', transport=httpx.MockTransport(handler))
    token = app.state.sessions.create('s', 'mask')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
            r = await c.post('/complete', json=body('x'), headers={'Authorization': 'Bearer ' + token})
        assert r.json()['error']['kind'] == 'session_revoked'
        totals = app.state.ledger.totals()
        assert totals['attempts'] == 1 and totals['known_tokens'] == 18
    finally:
        await app.state.client.close()


@pytest.mark.asyncio
async def test_refused_retry_keeps_earlier_ambiguous_attempt_ambiguous(provider, ledger):
    """A retry refused after an ambiguous dispatch must not claim that nothing was sent."""
    seen, revoked = [], []

    def handler(request):
        seen.append(1)
        revoked.append(True)
        raise httpx.ReadError('connection reset after send', request=request)

    def guard():
        if revoked:
            raise LabError('session_revoked', 'Session was revoked before dispatch; no upstream request was sent')

    retry = provider.retry.model_copy(update={'max_attempts': 3, 'retry_ambiguous': True})
    async with APIClient(provider.model_copy(update={'retry': retry}), ledger,
                         transport=httpx.MockTransport(handler)) as client:
        token = DISPATCH_GUARD.set(guard)
        try:
            with pytest.raises(LabError) as err:
                await client.complete([Message('user', 'x')], op='o', cell='c')
        finally:
            DISPATCH_GUARD.reset(token)
    assert err.value.kind == 'session_revoked' and err.value.ambiguous is True
    assert 'no upstream request was sent' not in err.value.message
    assert len(seen) == 1
    totals = ledger.totals()
    assert totals['attempts'] == 1 and totals['unknown_attempts'] == 1


@pytest.mark.asyncio
async def test_guard_rechecked_after_async_ledger_commit(provider, ledger, monkeypatch):
    """Revocation during worker admission cannot send a just-reserved attempt."""
    reserved, release = threading.Event(), threading.Event()
    revoked, seen = [], []
    original = ledger.reserve_gated
    def delayed(*args, **kwargs):
        value = original(*args, **kwargs)
        reserved.set()
        assert release.wait(2)
        return value
    monkeypatch.setattr(ledger, 'reserve_gated', delayed)
    def guard():
        if revoked:
            raise LabError('session_revoked', 'revoked before dispatch')
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=raw(provider))
    async with APIClient(provider, ledger, transport=httpx.MockTransport(handler)) as client:
        token = DISPATCH_GUARD.set(guard)
        try:
            task = asyncio.create_task(client.complete([Message('user', 'x')], op='guard', cell='c'))
        finally:
            DISPATCH_GUARD.reset(token)
        async with asyncio.timeout(2):
            while not reserved.is_set():
                await asyncio.sleep(.002)
        revoked.append(True)
        release.set()
        with pytest.raises(LabError) as err:
            await task
    assert err.value.kind == 'session_revoked' and not err.value.ambiguous
    assert seen == []
    attempt = ledger.attempts()[0]
    assert attempt['actual_tokens'] == 0 and attempt['error_kind'] == 'not_dispatched'
