"""Credential-free delay fixture: local diagnostic, never forwards to a Provider.

Compare the same fixture through direct and sidecar routes. Local results alone
are not a native-agent result or proof of a historical sidecar root cause.
"""
from __future__ import annotations
import argparse
import hashlib
import http.server
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


class Fixture(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, port, delay, mode, log):
        super().__init__((host, port), Handler)
        self.delay, self.mode, self.log = delay, mode, log
        self.lock = threading.Lock()
        self.completed = threading.Event()

    def event(self, **event):
        with self.lock:
            with self.log.open('a', encoding='utf8') as f:
                f.write(json.dumps({'time_ns': time.time_ns(), **event}) + '\n')


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != '/session':
            self.send_error(404); return
        rid = self.headers.get('X-Diagnostic-ID', '')
        if not rid or len(rid) > 80 or not all(c.isalnum() or c == '-' for c in rid):
            self.send_error(400); return
        attempt = str(uuid.uuid4())
        body = json.dumps({'id': rid, 'mock_only': True,
                           'payload': 'fixed-diagnostic-payload'}, sort_keys=True).encode()
        s = self.server
        def event(**fields):
            s.event(id=rid, server_attempt_id=attempt, **fields)
        event(event='received', delay_seconds=s.delay, mode=s.mode)
        try:
            if s.mode == 'headers': time.sleep(s.delay)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.flush()
            event(event='headers_sent')
            if s.mode == 'body': time.sleep(s.delay)
            self.wfile.write(body); self.wfile.flush()
            event(event='body_write_completed', body_sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        except OSError as exc:
            event(event='write_error', exception=type(exc).__name__, errno=exc.errno)
        finally:
            # Signal only AFTER closing the log write. Client completion can race it.
            s.completed.set()


def validate_client_result(value, rid):
    if not isinstance(value, dict) or type(value.get('ok')) is not bool or value.get('id') != rid:
        raise ValueError('Malformed diagnostic client result')
    if value['ok'] and (value.get('status') != 200 or value.get('phase') != 'complete'
                        or not isinstance(value.get('body_sha256'), str)
                        or len(value['body_sha256']) != 64):
        raise ValueError('Incomplete success result')
    return value


def run_local(out: Path, node: str, delays: list[float]):
    out.mkdir(parents=True, exist_ok=False)
    def cell(delay, mode):
        rid = str(uuid.uuid4()); log = out / (rid + '.server.jsonl')
        server = Fixture('127.0.0.1', 0, delay, mode, log)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        result = {'id': rid, 'ok': False, 'delay_seconds': delay, 'mode': mode,
                  'server_completion_matching': False}
        try:
            args = [node, str(Path(__file__).with_name('net_client.mjs')),
                    f'http://127.0.0.1:{server.server_port}/session', rid,
                    str(int((max(delays) + 15) * 1000))]
            proc = subprocess.run(args, capture_output=True, text=True, timeout=max(delays) + 20)
            (out / (rid + '.client.stdout')).write_text(proc.stdout, encoding='utf8')
            (out / (rid + '.client.stderr')).write_text(proc.stderr, encoding='utf8')
            result.update(validate_client_result(json.loads(proc.stdout), rid), exit_code=proc.returncode)
            # Failed/malformed clients remain failed rows rather than aborting the whole matrix.
            if result['ok'] and proc.returncode == 0:
                server.completed.wait(timeout=5)
                records = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
                result['server_completion_matching'] = any(
                    x.get('event') == 'body_write_completed' and x.get('id') == rid
                    and x.get('body_sha256') == result.get('body_sha256') for x in records)
            else:
                result['ok'] = False
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result.update(ok=False, diagnostic_error=type(exc).__name__)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        return result
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda pair: cell(*pair), [(d,m) for d in delays for m in ('headers','body')]))
    result = {'schema': 2, 'route': 'local_direct_only', 'sidecar_tested': False,
              'model_calls': 0, 'model_calls_basis': 'no provider code or forwarding in this fixture',
              'rows': rows, 'all_complete': bool(rows) and all(r['ok'] and r['server_completion_matching'] for r in rows)}
    (out / 'matrix.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='mode', required=True)
    s = sub.add_parser('serve'); s.add_argument('--host', default='127.0.0.1'); s.add_argument('--port', type=int, default=8879)
    s.add_argument('--delay', type=float, default=25); s.add_argument('--delay-at', choices=['headers','body'], default='headers')
    s.add_argument('--log', type=Path, required=True); s.add_argument('--allow-nonloopback', action='store_true')
    l = sub.add_parser('local'); l.add_argument('--out', type=Path, required=True); l.add_argument('--node', default='node'); l.add_argument('--delays', default='0,25')
    a = p.parse_args()
    if a.mode == 'local':
        try: ds = [float(v) for v in a.delays.split(',')]
        except ValueError: p.error('Use comma-separated numeric delays')
        if not ds or len(ds)>10 or any(not 0<=d<=60 for d in ds): p.error('Use 1..10 delays in [0,60]')
        return 0 if run_local(a.out, a.node, ds)['all_complete'] else 1
    if not 0<=a.delay<=600: p.error('Delay must be in [0,600]')
    if a.host not in ['127.0.0.1','localhost'] and not a.allow_nonloopback: p.error('Explicit nonloopback opt-in required')
    a.log.parent.mkdir(parents=True, exist_ok=True)
    with a.log.open('x'): pass
    server = Fixture(a.host, a.port, a.delay, a.delay_at, a.log)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
