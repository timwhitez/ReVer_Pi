"""Documented commands are contracts (issue #12).

1. Every `reverpi ...` command in the product docs must parse with the current CLI.
2. Blocks marked `<!-- doc-contract: NAME -->` are executed end to end in a fresh
   temporary directory with the mock Provider (no credentials, loopback only), and
   the artifacts they promise are opened and read, not just exit codes checked.
"""
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import pytest
from reverpi.cli import parser

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "agents-install.md", ROOT / "pi" / "README.md",
        ROOT / "docs" / "configuration.md", ROOT / "docs" / "getting-started.md"]
PI_READY = (ROOT / "pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js").exists()


def fenced_commands(text: str):
    """(marker, [command, ...]) for each fenced shell block; continuations joined."""
    blocks = []
    for match in re.finditer(r"(?:<!-- doc-contract: ([\w-]+) -->\n)?```(?:bash|sh)\n(.*?)```", text, re.S):
        joined = re.sub(r"\\\n\s*", " ", match.group(2))
        commands = []
        for line in joined.splitlines():
            line = re.sub(r"\s+#.*$", "", line).strip()
            if line.startswith("reverpi "):
                commands.append(line)
        blocks.append((match.group(1), commands))
    return blocks


def inline_commands(text: str):
    # Inline code with flags is a runnable command; a bare `reverpi costs` names a subcommand.
    return [m for m in re.findall(r"`(reverpi [^`]+)`", text) if " --" in m]


def placeholders(command: str) -> str:
    # Optional flags shown as [--flag] and <angle> placeholders are template syntax.
    command = re.sub(r"\[(--[\w-]+(?: [A-Z_0-9]+)?)\]", r"\1", command)
    return re.sub(r"<[^>]+>", "PLACEHOLDER", command)


def all_documented():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for _, commands in fenced_commands(text):
            for c in commands:
                yield doc.relative_to(ROOT).as_posix(), c
        for c in inline_commands(text):
            yield doc.relative_to(ROOT).as_posix(), c


DOCUMENTED = sorted(set(all_documented()))


def test_docs_contain_commands():
    assert len(DOCUMENTED) >= 10


@pytest.mark.parametrize("doc,command", DOCUMENTED, ids=[f"{d}:{c[:60]}" for d, c in DOCUMENTED])
def test_documented_command_parses(doc, command, capsys):
    argv = shlex.split(placeholders(command))[1:]
    try:
        parser().parse_args(argv)
    except SystemExit as exc:
        pytest.fail(f"{doc}: `{command}` is rejected by the CLI: {capsys.readouterr().err.strip()}")


def test_pi_run_has_no_paid_flag_in_docs():
    """Paid authority belongs to the gateway; pi-run only holds a session token."""
    for doc, command in DOCUMENTED:
        if command.startswith("reverpi pi-run"):
            assert "--allow-paid" not in command, doc


def contract_block(name: str):
    text = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    for marker, commands in fenced_commands(text):
        if marker == name:
            return commands
    raise AssertionError(f"doc-contract block {name!r} missing from getting-started.md")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DocRunner:
    def __init__(self, tmp: Path, substitutions: dict[str, str]):
        self.tmp, self.subs, self.gateways = tmp, substitutions, []
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        for key in [k for k in self.env if k.startswith("REVER_")]:
            self.env.pop(key)

    def argv(self, command: str):
        for old, new in self.subs.items():
            command = command.replace(old, new)
        return [sys.executable, "-B", "-m", "reverpi", *shlex.split(command)[1:]]

    def run(self, command: str):
        argv = self.argv(command)
        if argv[4] == "gateway":
            log = open(self.tmp / f"gateway-{len(self.gateways)}.log", "w")
            proc = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=log, stderr=subprocess.STDOUT)
            self.gateways.append((proc, log))
            port = argv[argv.index("--port") + 1]
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                        assert json.loads(r.read()) == {"ok": True, "schema": 1}
                        return None
                except OSError:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            raise AssertionError("documented gateway did not become healthy: " +
                                 (self.tmp / f"gateway-{len(self.gateways)-1}.log").read_text()[-2000:])
        r = subprocess.run(argv, cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, f"`{command}` exited {r.returncode}: {r.stderr[-2000:]}"
        return json.loads(r.stdout) if r.stdout.strip() else None

    def close(self):
        for proc, log in self.gateways:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()


@pytest.fixture
def runner(tmp_path):
    made = []

    def make(subs):
        r = DocRunner(tmp_path, subs)
        made.append(r)
        return r
    yield make
    for r in made:
        r.close()


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies not installed (cd pi && npm ci)")
def test_product_path_runs_offline_with_mock_provider(tmp_path, runner):
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("doc contract fixture\n")
    (tmp_path / "task.md").write_text('Reply with exactly one JSON object: {"status":"ready"}\n')
    port = str(free_port())
    run = runner({"configs/provider.local.yaml": "configs/mock.chat.yaml", "runs/demo": str(tmp_path / "runs/demo"),
                  "/path/to/project": str(project), "task.md": str(tmp_path / "task.md"), "8765": port})
    outputs = [run.run(c) for c in contract_block("product-path")]
    commands = [c.split()[1] for c in contract_block("product-path")]
    assert commands == ["gateway", "session-create", "pi-run", "costs"]
    gateway_run, pi_out = tmp_path / "runs/demo", tmp_path / "runs/demo/pi"
    for name in ("sessions.sqlite", "ledger.sqlite", "archive.sqlite"):
        assert (gateway_run / name).is_file(), name
    assert (gateway_run / "demo-1.token").read_text().strip()
    task = json.loads((pi_out / "task.json").read_text())
    assert task["return_code"] == 0 and task["events"]["agent_end"] and task["mock_provider"] is True
    events = [json.loads(line) for line in (pi_out / "events.jsonl").read_text().splitlines() if line.strip()]
    assert any(e.get("type") == "agent_end" for e in events)
    assert (pi_out / "session.jsonl").stat().st_size > 0
    costs = outputs[-1]
    assert costs["attempts"] >= 1 and costs["known_tokens"] > 0 and costs["unknown_attempts"] == 0
    assert costs["currency_is_fully_known"] is False   # The mock has no price; unknown is not zero.


@pytest.mark.skipif(not PI_READY, reason="Pi dependencies not installed (cd pi && npm ci)")
def test_online_projection_example_enables_projection(tmp_path, runner):
    port = str(free_port())
    run = runner({"runs/online": str(tmp_path / "runs/online"), "8765": port})
    for c in contract_block("online-path"):
        run.run(c)
    token = (tmp_path / "runs/online/online-1.token").read_text().strip()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/session", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=5) as r:
        info = json.loads(r.read())
    assert info["method"] == "mask" and info["online_projection"]["mode"] == "apply"
    assert info["recovery"]["interface"] == "split_v1"
    assert (tmp_path / "runs/online/projection.sqlite").is_file()


def test_research_path_runs_offline(tmp_path, runner):
    run = runner({"data/fixtures": str(tmp_path / "fixtures"), "runs/study": str(tmp_path / "study")})
    outputs = [run.run(c) for c in contract_block("research-path")]
    assert (tmp_path / "study" / "manifest.json").is_file()
    report = json.loads((tmp_path / "study" / "analysis.json").read_text())
    assert report == outputs[-1] and report["mock"] is True and "full" in report["table"]
