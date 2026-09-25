"""Per-request archive batching (issue #13). Local SQLite and MockTransport only."""
import sqlite3
from dataclasses import asdict
import httpx
import pytest
from reverpi.config import Budget, CompressionConfig, StudyConfig
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.memory import Archive
from reverpi.online_projection import ObservationMeta
from reverpi.protocols import Message
from reverpi.util import digest
from test_transport import raw


def legacy_put(archive: Archive, namespace: str, content: str) -> str:
    """The pre-batching Archive.put, kept verbatim as the equivalence reference."""
    handle = digest(content)
    n = len(content.encode("utf-8"))
    with archive.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT content,bytes FROM blobs WHERE namespace=? AND handle=?", (namespace, handle)).fetchone()
        if old:
            if old[0] != content or old[1] != n:
                raise LabError("archive_corruption", "Existing archive blob no longer matches its content identity")
            return handle
        used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM blobs WHERE namespace=?", (namespace,)).fetchone()[0]
        if used + n > archive.config.archive_bytes:
            raise LabError("archive_quota", "Archive capacity exceeded; no old data silently evicted")
        db.execute("INSERT INTO blobs VALUES (?,?,?,?)", (namespace, handle, content, n))
    return handle


def rows(archive):
    with sqlite3.connect(archive.path) as db:
        return sorted(db.execute("SELECT namespace,handle,content,bytes FROM blobs"))


def history(n):
    calls = [{'id': f'c{i}', 'name': 'read', 'arguments': {'path': f'f{i}.txt'}} for i in range(n)]
    tools = [Message('tool', f'result {i} ' + 'x' * (i * 37 % 500) + ' 中😀', call_id=f'c{i}') for i in range(n)]
    messages = [Message('user', 'goal'), Message('assistant', '', calls), *tools]
    meta = [ObservationMeta(call_id=f'c{i}', tool_name='read', content_sha=digest(t.content), is_error=False)
            for i, t in enumerate(tools)]
    return messages, meta


class Counter:
    """Counts archive identity checks and write transactions on a live Archive."""
    def __init__(self, archive, monkeypatch):
        self.checked, self.transactions = [], 0
        original_many, original_connect = archive.put_many, archive.connect

        def put_many(namespace, contents):
            self.checked.extend(contents)
            return original_many(namespace, contents)

        def connect():
            self.transactions += 1
            return original_connect()
        monkeypatch.setattr(archive, 'put_many', put_many)
        monkeypatch.setattr(archive, 'connect', connect)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['observe', 'apply', 'off'])
async def test_each_content_is_checked_once_per_request(provider, tmp_path, monkeypatch, mode):
    app = create_app(provider, StudyConfig(methods=['mask'], online_projection={'mode': mode},
                                           budget=Budget(max_total_tokens=10_000_000, per_cell_tokens=5_000_000)),
                     tmp_path / 'g', transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw(provider))))
    archive = app.state.archive
    counter = Counter(archive, monkeypatch)
    messages, meta = history(40)
    token = app.state.sessions.create('cell', 'mask')
    body = {'op': 'o1', 'messages': [asdict(m) for m in messages]}
    if mode != 'off':
        body['observation_meta'] = [m.model_dump() for m in meta]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
            r = await c.post('/complete', json=body, headers={'Authorization': 'Bearer ' + token})
        assert r.status_code == 200 and 'error' not in r.json(), r.text
    finally:
        await app.state.client.close()
    distinct = {m.content for m in messages if m.content}
    assert len(counter.checked) == len(distinct) == 41   # Previously 81 separate put() calls.
    assert counter.transactions <= 2                     # Independent of the 40 tool results.
    assert {h for _, h, _, _ in rows(archive)} == {digest(c) for c in distinct}


@pytest.mark.parametrize('seed', range(5))
def test_put_many_matches_sequential_put(tmp_path, seed):
    import random
    rng = random.Random(seed)
    pool = ['', 'a', 'shared', '中😀' * 3] + [''.join(rng.choice('ab中') for _ in range(rng.randint(1, 40))) for _ in range(20)]
    batch = [rng.choice(pool) for _ in range(60)]
    cfg = CompressionConfig()
    old, new = Archive(tmp_path / 'old.sqlite', cfg), Archive(tmp_path / 'new.sqlite', cfg)
    for archive in (old, new):
        legacy_put(archive, 'n', 'shared')      # Some content already present.
    expected = [legacy_put(old, 'n', c) for c in batch]
    assert new.put_many('n', batch) == expected
    assert rows(new) == rows(old)
    assert new.put_many('n', []) == []
    assert new.put('n', 'a') == digest('a')


def test_recovery_results_identical(tmp_path):
    cfg = CompressionConfig()
    old, new = Archive(tmp_path / 'old.sqlite', cfg), Archive(tmp_path / 'new.sqlite', cfg)
    texts = ['alpha needle one', 'beta NEEDLE two 中😀', 'gamma']
    for t in texts:
        legacy_put(old, 'n', t)
    new.put_many('n', texts)
    for op, kwargs in [('r1', {'handle': digest(texts[1]), 'start': 3, 'chars': 9}), ('s1', {'query': 'needle'})]:
        assert new.recover('n', op, **kwargs) == old.recover('n', op, **kwargs)


def test_tampered_existing_blob_rejects_and_rolls_back_the_batch(tmp_path):
    archive = Archive(tmp_path / 'a.sqlite', CompressionConfig())
    archive.put('n', 'original')
    with sqlite3.connect(archive.path) as db:
        db.execute("UPDATE blobs SET content='tampered' WHERE handle=?", (digest('original'),))
    before = rows(archive)
    with pytest.raises(LabError) as err:
        archive.put_many('n', ['new first', 'original', 'new last'])
    assert err.value.kind == 'archive_corruption'
    assert rows(archive) == before


def test_namespaces_stay_isolated(tmp_path):
    archive = Archive(tmp_path / 'a.sqlite', CompressionConfig())
    archive.put_many('a', ['secret'])
    with pytest.raises(LabError) as err:
        archive.recover('b', 'op', handle=digest('secret'))
    assert err.value.kind == 'archive_not_found'
    archive.put_many('b', ['secret'])
    assert archive.used('a') == archive.used('b') == len(b'secret')


def test_cumulative_new_bytes_over_quota_rejected_atomically(tmp_path):
    archive = Archive(tmp_path / 'a.sqlite', CompressionConfig(archive_bytes=1024))
    archive.put('n', 'x' * 400)
    # Each new item fits alone; together with what is stored they do not.
    with pytest.raises(LabError) as err:
        archive.put_many('n', ['y' * 400, 'z' * 400])
    assert err.value.kind == 'archive_quota'
    assert archive.used('n') == 400
    # Duplicates inside one batch are counted once.
    assert archive.put_many('n', ['y' * 300, 'y' * 300]) == [digest('y' * 300)] * 2
    assert archive.used('n') == 700
