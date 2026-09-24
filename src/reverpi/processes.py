"""Bounded POSIX process-group cleanup, including descendants of an exited leader.

Only use for subprocesses created with start_new_session=True. This is not a
container boundary: detached descendants and remote Docker jobs require the
explicit environment reconciliation already enforced by the controller.
"""
from __future__ import annotations
import asyncio
import contextlib
import os
import signal
import time


async def stop_process_group(proc: asyncio.subprocess.Process | None, grace_seconds: float = 3.0) -> None:
    if proc is None:
        return
    pgid = proc.pid
    if os.name != "posix" or pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError("Refusing unsafe process-group cleanup")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        await proc.wait()
        return
    # Checking only proc.returncode misses live children after the leader exits.
    # Escalate even when the parent has already been reaped.
    deadline = time.monotonic() + grace_seconds
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            break
        await asyncio.sleep(min(.05, max(0, deadline-time.monotonic())))
    await asyncio.wait_for(proc.wait(), max(1.0, grace_seconds))
