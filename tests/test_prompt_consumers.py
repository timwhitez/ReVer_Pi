"""Current diagnostic entry points against real Pi and loopback scripted actors."""
from pathlib import Path
import asyncio
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import native_compaction_smoke as native
import online_projection_smoke as online
import paired_prefix_canary as paired
from reverpi.util import digest


@pytest.fixture
def rpc_spawns(monkeypatch):
    if not shutil.which("node") or not (ROOT / "pi/node_modules").is_dir():
        pytest.skip("real Pi diagnostics require pinned npm dependencies and Node >=22.19")
    original = asyncio.create_subprocess_exec
    calls = []

    async def spawn(*argv, **kwargs):
        if "--mode" in argv:
            assert argv[argv.index("--mode") + 1] == "rpc"
            assert "--print" not in argv
            assert not any(str(arg).startswith("@") for arg in argv)
            assert "REVER_PROMPT_TRANSPORT" not in kwargs["env"]
            extensions = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--extension"]
            assert str(ROOT / "pi/src/index.ts") in extensions
            assert all(Path(path).is_file() for path in extensions)
            calls.append(tuple(extensions))
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_native_diagnostic_rpc_consumer(tmp_path, rpc_spawns, protocol):
    row = await native.case_run(ROOT, tmp_path, protocol, "mask_checkpoint", "long")
    assert row["status"] == "passed"
    assert row["intervention_invoked"] and row["post_compaction_response_verified"]
    assert row["session_revoked"] and row["gateway_stopped"]
    assert row["paid_model_calls"] == 0 and not row["native_gate_eligible"]
    assert len(rpc_spawns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_online_diagnostic_rpc_consumer(tmp_path, rpc_spawns, protocol):
    row = await online.case_run(tmp_path, protocol, "online_apply", "long")
    assert row["status"] == "passed" and row["request_count"] == 8
    assert row["applied_requests"] > 0
    assert row["session_revoked"] and row["gateway_stopped"]
    assert row["real_model_calls"] == 0 and not row["quality_measured"]
    assert len(rpc_spawns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_paired_diagnostic_rpc_consumer(tmp_path, rpc_spawns, protocol):
    plan_dir = tmp_path / "plan"
    plan = paired.prepare(plan_dir, protocol, "long", seed="prompt-consumer-offline")
    result = await paired.run(plan_dir / "PLAN.json", digest(plan.model_dump()), tmp_path / "run")
    assert result["status"] == "paired_complete", result["phases"]
    assert result["real_model_calls"] == 0 and not result["quality_or_superiority_established"]
    assert result["source_unchanged"] and result["fixture_unchanged"]
    assert len(result["phases"]) == len(rpc_spawns) == 3
    assert all(row["session_revoked"] for row in result["phases"])
    assert all(row["prefix_replays"] > 0 for row in result["phases"][1:])
    assert all(str(ROOT / "pi/src/paired-readonly.ts") in extensions for extensions in rpc_spawns)
