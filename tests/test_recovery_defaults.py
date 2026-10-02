"""Recovery defaults through real HTTP parsing; only local archived evidence."""
import asyncio
import json
import shutil
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from reverpi.config import CompressionConfig, StudyConfig
from reverpi.gateway import create_app


def setup_gateway(tmp_path, provider, cap, *, interface="legacy", calls=10):
    study = StudyConfig(
        compression=CompressionConfig(recovery_chars=cap, recovery_calls=calls),
        online_projection={"recovery_interface": interface},
    )
    app = create_app(provider, study, tmp_path / "gateway")
    token = app.state.sessions.create("one", "mask")
    # Non-BMP and Chinese characters make codepoint/UTF-16 confusion observable.
    content = "中😀needle" * 1000
    handle = app.state.archive.put("one", content)
    return app, {"Authorization": "Bearer " + token}, handle, content


@pytest.mark.parametrize("cap", [100, 999, 1999, 2000, 6000])
@pytest.mark.parametrize("lookup", ["handle", "query"])
async def test_omitted_chars_uses_session_cap(tmp_path, provider, cap, lookup):
    app, headers, handle, content = setup_gateway(tmp_path, provider, cap)
    body = {"op": "read", lookup: handle if lookup == "handle" else "needle"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.post("/recover", headers=headers, json=body)
            assert response.status_code == 200, response.text
            result = response.json()
            effective = min(2000, cap)
            if lookup == "handle":
                assert result == {"handle": handle, "start": 0, "end": effective,
                                  "total_chars": len(content), "text": content[:effective]}
            else:
                assert result == {"matches": [{"handle": handle, "excerpt": content[:min(200, effective)]}]}
            # Omission and its effective explicit default share one cache identity.
            replay = await client.post("/recover", headers=headers, json={**body, "chars": effective})
            assert replay.status_code == 200 and replay.json() == result
            changed = await client.post("/recover", headers=headers, json={**body, "chars": effective - 1})
            assert changed.status_code == 409 and changed.json()["error"]["kind"] == "idempotency_conflict"
        with app.state.archive.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM recovery").fetchone()[0] == 1
    finally:
        await app.state.client.close()


@pytest.mark.parametrize("cap", [100, 999, 1999, 2000, 6000])
async def test_explicit_chars_are_validated_not_clipped(tmp_path, provider, cap):
    app, headers, handle, content = setup_gateway(tmp_path, provider, cap)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            body = {"op": "read", "handle": handle, "start": 1}
            response = await client.post("/recover", headers=headers, json={**body, "chars": cap})
            assert response.status_code == 200 and response.json()["text"] == content[1:1 + cap]
            for invalid in [cap + 1, 0, "100", 1.5, True, None]:
                rejected = await client.post("/recover", headers=headers, json={**body, "op": "bad", "chars": invalid})
                assert rejected.status_code == 422, rejected.text
                assert rejected.json()["error"]["kind"] in {"recovery_arguments", "gateway_schema"}
        with app.state.archive.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM recovery").fetchone()[0] == 1
    finally:
        await app.state.client.close()


@pytest.mark.parametrize("interface", ["legacy", "split_v1"])
async def test_real_pi_loop_recovery_with_local_gateway(tmp_path, provider, interface):
    root = Path(__file__).resolve().parents[1]
    if not shutil.which("node") or not (root / "pi/node_modules").is_dir():
        pytest.skip("Pi gateway integration requires Node >=22.19 and pi/npm ci")
    app, headers, handle, content = setup_gateway(tmp_path, provider, 100, interface=interface, calls=2)
    # Keep the allocated socket open until Uvicorn owns it; no free-port race.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    process = None
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("local gateway did not start")
                await asyncio.sleep(0.01)
            process = await asyncio.create_subprocess_exec(
                "node", "--experimental-strip-types", str(root / "pi/tests/gateway-recovery-helper.mjs"),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            payload = {"url": f"http://127.0.0.1:{port}", "token": headers["Authorization"][7:],
                       "handle": handle, "interface": interface, "text": content[:100]}
            stdout, stderr = await process.communicate(json.dumps(payload).encode())
            assert process.returncode == 0, stderr.decode()
            assert json.loads(stdout) == {"successful": 2, "refused": 3, "replayed": True}
        with app.state.archive.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM recovery").fetchone()[0] == 2
        assert app.state.ledger.totals()["attempts"] == 0
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await task
        sock.close()
