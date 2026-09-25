"""Offline archive I/O measurement for one growing online-projection session.

Replays T turns through the real gateway with a MockTransport Provider (no
network, no credentials, synthetic usage). Each turn resends the full history,
as Pi does. Compares the batched archive path with the pre-batching behaviour
(one transaction per archive.put, tool results checked twice). Numbers are
machine-local measurements, not a product performance claim.

Usage: python scripts/bench_archive_io.py [--turns 60] [--tool-bytes 4000]
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from reverpi.config import Budget, Provider, StudyConfig  # noqa: E402
from reverpi.gateway import create_app  # noqa: E402
from reverpi.memory import Archive  # noqa: E402
from reverpi.online_projection import ObservationMeta  # noqa: E402
from reverpi.protocols import Message  # noqa: E402
from reverpi.util import digest  # noqa: E402


async def run(turns: int, tool_bytes: int, legacy: bool) -> dict:
    provider = Provider(name="bench", mock=True, base_url="http://127.0.0.1:1/v1", model="mock-reasoner",
                        requests_per_minute=10**6, tokens_per_minute=10**9)
    reply = {"id": "r", "model": "mock-reasoner", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
             "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(provider, StudyConfig(methods=["mask"], online_projection={"mode": "observe"},
                                               budget=Budget(max_total_tokens=10**12, per_cell_tokens=10**12,
                                                             max_attempts=10**5, per_cell_attempts=10**5)),
                         Path(tmp) / "g", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply)))
        archive: Archive = app.state.archive
        transactions = 0
        original_connect = archive.connect

        def counting_connect():
            nonlocal transactions
            transactions += 1
            return original_connect()
        archive.connect = counting_connect
        if legacy:
            # Old pattern, inside the request: projection put each tool result in its own
            # transaction, then the gateway put every non-empty message again, one by one.
            batched = archive.put_many
            pending: list[str] = []

            def per_item(namespace, contents):
                if not pending:          # First call of a request: projection.prepare.
                    pending.extend(contents or [""])
                    return [batched(namespace, [c])[0] for c in contents]
                tools = [c for c in pending if c]
                pending.clear()          # Second call: the gateway pass re-checked tool results too.
                return [batched(namespace, [c])[0] for c in tools + list(contents)][len(tools):]
            archive.put_many = per_item
        token = app.state.sessions.create("cell", "mask")
        messages = [Message("user", "goal")]
        meta: list[ObservationMeta] = []
        lag = 0.0
        stop = asyncio.Event()

        async def ticker():
            nonlocal lag
            while not stop.is_set():
                t = time.perf_counter()
                await asyncio.sleep(0.001)
                lag = max(lag, time.perf_counter() - t - 0.001)
        tick = asyncio.create_task(ticker())
        cpu, wall = time.process_time(), time.perf_counter()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"Authorization": "Bearer " + token}) as client:
            for turn in range(turns):
                call = f"c{turn}"
                content = f"turn {turn} " + ("x" * tool_bytes)
                messages += [Message("assistant", "", [{"id": call, "name": "read", "arguments": {"path": f"{turn}.txt"}}]),
                             Message("tool", content, call_id=call)]
                meta.append(ObservationMeta(call_id=call, tool_name="read", content_sha=digest(content), is_error=False))
                r = await client.post("/complete", json={"op": f"op{turn}", "messages": [asdict(m) for m in messages],
                                                         "observation_meta": [m.model_dump() for m in meta]})
                if r.status_code != 200 or "error" in r.json():
                    raise SystemExit(f"turn {turn} failed: {r.text[:200]}")
        cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
        stop.set()
        await tick
        await app.state.client.close()
        return {"path": "pre-batch" if legacy else "batched", "turns": turns, "archive_transactions": transactions,
                "cpu_seconds": round(cpu, 3), "wall_seconds": round(wall, 3), "max_event_loop_lag_ms": round(lag * 1000, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--turns", type=int, default=60)
    ap.add_argument("--tool-bytes", type=int, default=4000)
    args = ap.parse_args()
    results = [asyncio.run(run(args.turns, args.tool_bytes, legacy)) for legacy in (True, False)]
    print(json.dumps({"mock_provider": True, "measurement_is_machine_local": True, "results": results}, indent=2))


if __name__ == "__main__":
    main()
