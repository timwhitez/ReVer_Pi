"""Real pinned Pi CLI, local gateway, scripted provider; no live credentials."""
import asyncio
import json
import shutil
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from reverpi.config import Budget, StudyConfig
from reverpi.gateway import create_app
from reverpi.launcher import launch
from reverpi import launcher


ROOT = Path(__file__).resolve().parents[1]
TEXTS = [
    "Read inventory.txt and report its count.",
    "\t\n  中文😀\r\nsecond\u2028third\u2029  \n\t",
    "@not-an-attachment --provider NOT_A_PROVIDER /literal\n",
    "/llama is literal task text, not an extension command.\n",
    "start\n" + "中文😀x\r\n" * 25000 + "\nend  ",
]


@asynccontextmanager
async def prompt_gateway(tmp_path, provider, reply=None):
    if not shutil.which("node") or not (ROOT / "pi/node_modules").is_dir():
        pytest.skip("real Pi CLI requires pinned npm dependencies and Node >=22.19")
    captured = []
    def upstream(request):
        captured.append(json.loads(request.content))
        if reply is not None:
            return reply(request, captured[-1])
        return httpx.Response(200, json={"id": "synthetic", "model": provider.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OFFLINE_OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    profile = provider.model_copy(update={"context_window": 4000000})
    app = create_app(profile, StudyConfig(budget=Budget(per_cell_tokens=2000000)),
                     tmp_path / "runs/gw", transport=httpx.MockTransport(upstream))
    token = app.state.sessions.create("synthetic", "mask")
    token_file = tmp_path / "runs/gw/synthetic.token"
    token_file.write_text(token)
    work = tmp_path / "owned-workspace"; work.mkdir()
    (work / "inventory.txt").write_text("count=13\n")
    (tmp_path / "runs/gw/neighbor.token").write_text("NOT_A_REAL_CREDENTIAL")
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done(): await serving
                await asyncio.sleep(.01)
        yield app, captured, work, token_file, f"http://127.0.0.1:{sock.getsockname()[1]}", profile
    finally:
        server.should_exit = True
        await serving
        sock.close()


@pytest.mark.parametrize("text", TEXTS, ids=["plain", "whitespace-unicode", "argv", "slash", "long"])
async def test_real_pi_launcher_preserves_task_without_archive_path(tmp_path, provider, text):
    async with prompt_gateway(tmp_path, provider) as (app, captured, work, token_file, url, _):
        prompt = tmp_path / "task.txt"; prompt.write_bytes(text.encode("utf-8"))
        out = tmp_path / "runs/pi2"
        status = await launch(ROOT, work, prompt, out, url,
                              token_file, wall_seconds=30, acknowledge_unsandboxed=True)
        assert status["status"] == "completed", (out / "stderr.log").read_text()
        assert len(captured) == 1
        user = next(m for m in captured[0]["messages"] if m["role"] == "user")
        same_text = user["content"] == text
        assert same_text, "model-visible task differs from the original text"
        assert str(out) not in user["content"] and "<file name=" not in user["content"]
        assert (out / "prompt.txt").read_bytes() == prompt.read_bytes()
        assert app.state.ledger.totals()["attempts"] == 1
        assert status["workspace"] == str(work)
        assert (tmp_path / "runs/gw/neighbor.token").read_text() == "NOT_A_REAL_CREDENTIAL"
        with pytest.raises(ValueError, match="session is not fresh"):
            await launch(ROOT, work, prompt, out / "different-run", url, token_file,
                         wall_seconds=30, acknowledge_unsandboxed=True)
        assert len(captured) == 1


@pytest.mark.parametrize("encoded", ["SYNTHETIC_PRIVATE_TASK", '{"schema":2,"prompt":"SYNTHETIC_PRIVATE_TASK"}',
                                   '{"schema":1,"prompt":"SYNTHETIC_PRIVATE_TASK","extra":true}'])
async def test_real_pi_invalid_transport_never_dispatches(tmp_path, provider, monkeypatch, encoded):
    async with prompt_gateway(tmp_path, provider) as (app, captured, work, token_file, url, _):
        prompt = tmp_path / "task.txt"; prompt.write_text("SYNTHETIC_PRIVATE_TASK")
        monkeypatch.setattr(launcher, "prompt_input", lambda _: encoded)
        out = tmp_path / "invalid-run"
        status = await launch(ROOT, work, prompt, out, url, token_file,
                              wall_seconds=30, acknowledge_unsandboxed=True)
        assert status["status"] == "execution_failed" and status["return_code"] == 1
        assert not captured and app.state.ledger.totals()["attempts"] == 0
        assert not status["events"]["agent_end"]
        error = (out / "stderr.log").read_text()
        assert "invalid_prompt_transport" in error and "SYNTHETIC_PRIVATE_TASK" not in error


def test_command_and_prompt_input_keep_body_out_of_argv():
    body = "@file --print \n中文😀\r\n  " + "x" * 150000
    assert json.loads(launcher.prompt_input(body)) == {"schema": 1, "prompt": body}
    argv = launcher.command("node", "pi-cli", "extension", "session", "model", "low")
    assert argv[-1] == "--print" and body not in argv and not any(x.startswith("@") for x in argv)
    rpc = launcher.command("node", "pi-cli", "extension", "session", "model", "low", mode="rpc")
    assert rpc[rpc.index("--mode") + 1] == "rpc" and "--print" not in rpc
    env = launcher.isolated_env(Path("out"), "http://127.0.0.1:1", "SYNTHETIC", 2, 2)
    assert "REVER_PROMPT_TRANSPORT" not in env  # acceptance RPC stays ordinary
    for bad in ["", "  \r\n", "x" * 1000001]:
        with pytest.raises(ValueError): launcher.prompt_input(bad)


async def test_real_pi_rpc_acceptance_keeps_read_compact_continue(tmp_path, provider):
    from reverpi.acceptance import native_acceptance
    def reply(request, body):
        # Script only protocol/tool behavior, never claim autonomous quality.
        need_read = not any(m['role'] == 'tool' for m in body['messages']) and 'Use the read tool' in body['messages'][-1].get('content', '')
        message = {'role': 'assistant', 'content': 'OFFLINE_READY'}
        if need_read:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'synthetic-read', 'type': 'function',
                       'function': {'name': 'read', 'arguments': json.dumps({'path': 'probe.txt'})}}]}
        return httpx.Response(200, json={'id': 'synthetic', 'model': provider.model,
            'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if need_read else 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    async with prompt_gateway(tmp_path, provider, reply) as (app, captured, _, _, url, _):
        report = await native_acceptance(ROOT, tmp_path / 'runs/gw', url, tmp_path / 'rpc-acceptance', ['mask'],
                                         deadline=30, acknowledge_unsandboxed=True)
        row = report['methods'][0]
        assert row['accepted'], row
        assert row['read_tool_replay'] and row['compaction_committed'] and row['post_compaction_response']
        assert report['mock'] and not report['eligible_for_native_gate']
        assert captured and all('<file name=' not in m.get('content', '') for body in captured for m in body['messages'] if m['role'] == 'user')
