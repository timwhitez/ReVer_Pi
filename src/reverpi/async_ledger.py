"""Private bounded, single-worker bridge for the synchronous Ledger.

The loop owns dispatch. The worker owns a fresh SQLite connection per job. The
bridge retains the actual job future until the worker reports commit or rollback,
even if its caller's bounded wait has ended. A waiter timeout never certifies that
SQLite stopped or that its transaction rolled back.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import queue
import math
import threading
import time
from typing import Callable, Any

from .errors import LabError
from .ledger import Ledger, TransactionControl, transaction_control


@dataclass(eq=False)
class Operation:
    deadline: float
    cancelled: bool = False
    jobs: list[Job] = field(default_factory=list)
    cleanup_deadline: float | None = None
    interruptions: int = 0

    def cancel(self, *, interrupted=False):
        self.cancelled = True
        self.interruptions += int(interrupted)
        for job in self.jobs:
            if not job.cleanup:
                job.cancelled.set()

    def check(self):
        if self.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Total operation deadline exceeded")


@dataclass(eq=False)
class Job:
    fn: Callable
    args: tuple
    kwargs: dict
    future: asyncio.Future
    deadline: float
    cleanup: bool
    cancelled: threading.Event = field(default_factory=threading.Event)


class AsyncLedger:
    def __init__(self, ledger: Ledger, *, max_pending: int = 32, cleanup_seconds: float = 2):
        if type(max_pending) is not int or max_pending < 1 or not math.isfinite(cleanup_seconds) or cleanup_seconds <= 0:
            raise ValueError("Positive ledger queue and cleanup budget required")
        self.ledger, self.cleanup_seconds = ledger, cleanup_seconds
        self.slots = asyncio.Semaphore(max_pending)
        self.queue: queue.Queue[Job | None] = queue.Queue(maxsize=max_pending)
        self.loop = None
        self.thread = None
        self.done = None
        self.closing = False
        self.closed = False
        self.jobs: set[Job] = set()

    def _start(self):
        if self.loop is not None and self.loop is not asyncio.get_running_loop():
            raise LabError("client_loop_changed", "API client must stay on its owning event loop")
        if self.thread is None:
            self.loop = asyncio.get_running_loop()
            self.done = self.loop.create_future()
            self.thread = threading.Thread(target=self._worker, name="reverpi-ledger", daemon=False)
            self.thread.start()

    def _worker(self):
        try:
            while (job := self.queue.get()) is not None:
                try:
                    control = TransactionControl(job.deadline, job.cancelled)
                    control.check()
                    with transaction_control(control):
                        result = job.fn(*job.args, **job.kwargs)
                    error = None
                except BaseException as exc:
                    result, error = None, exc
                self.loop.call_soon_threadsafe(self._complete, job, result, error)
        finally:
            self.loop.call_soon_threadsafe(self.done.set_result, None)

    def _complete(self, job, result, error):
        self.jobs.remove(job)
        self.slots.release()
        if error is not None:
            job.future.set_exception(error)
            # Retain the exception for the owner/drain without unobserved-future warnings.
            job.future.exception()
        else:
            job.future.set_result(result)

    async def call(self, owner: Operation, fn: str | Callable, *args, cleanup=False, **kwargs) -> Any:
        if cleanup:
            task = asyncio.create_task(self._call(owner, fn, *args, cleanup=True, **kwargs))
            while True:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    owner.cancel(interrupted=True)
                    owner.cleanup_deadline = owner.cleanup_deadline or time.monotonic() + self.cleanup_seconds
        return await self._call(owner, fn, *args, **kwargs)

    async def _call(self, owner: Operation, fn: str | Callable, *args, cleanup=False, **kwargs) -> Any:
        if not cleanup:
            owner.check()
        if self.closed or (self.closing and not cleanup):
            raise LabError("client_closed", "Ledger client is closing")
        self._start()
        if cleanup:
            owner.cleanup_deadline = owner.cleanup_deadline or time.monotonic() + self.cleanup_seconds
        deadline = owner.cleanup_deadline if cleanup else owner.deadline
        # Queue admission, including backpressure, consumes the same absolute budget.
        try:
            async with asyncio.timeout_at(deadline):
                await self.slots.acquire()
        except TimeoutError:
            if cleanup:
                owner.cleanup_deadline = owner.cleanup_deadline or deadline
            raise
        try:
            if not cleanup:
                owner.check()
                if self.closing:
                    raise LabError("client_closed", "Ledger client is closing")
            job = Job(getattr(self.ledger, fn) if isinstance(fn, str) else fn,
                      args, kwargs, self.loop.create_future(), deadline, cleanup)
            owner.jobs.append(job)
            self.jobs.add(job)
            self.queue.put_nowait(job)
        except BaseException:
            self.slots.release()
            raise
        while True:
            try:
                # Admission and an already-submitted wait share the SAME
                # deadline. A future queued behind another owner is still owned,
                # but cannot extend this caller's execution/cleanup allowance.
                async with asyncio.timeout_at(deadline):
                    return await asyncio.shield(job.future)
            except TimeoutError:
                if job.future.done():
                    # Completion won the timeout race: accept its actual outcome.
                    return job.future.result()
                # Signal just this job, never cancel its real future or another
                # owner's transaction. The worker checks before BEGIN/COMMIT;
                # an already committed outcome is retained for drain/close.
                job.cancelled.set()
                if cleanup:
                    owner.cleanup_deadline = owner.cleanup_deadline or deadline
                raise
            except asyncio.CancelledError:
                owner.cancel(interrupted=True)
                owner.cleanup_deadline = owner.cleanup_deadline or time.monotonic() + self.cleanup_seconds
                if not cleanup:
                    raise
                # Necessary accounting owns this bounded job until its actual
                # result. Defer cancellation; the caller checks it before dispatch.

    async def drain(self, owner: Operation):
        """Wait for real outcomes before this owner submits finish/reconciliation."""
        owner.cleanup_deadline = owner.cleanup_deadline or time.monotonic() + self.cleanup_seconds
        pending = [j.future for j in owner.jobs if not j.future.done()]
        if pending:
            async def wait():
                async with asyncio.timeout_at(owner.cleanup_deadline):
                    await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
            task = asyncio.create_task(wait())
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    owner.cancel(interrupted=True)

    async def close(self):
        if self.closing:
            if self.done is not None:
                await asyncio.shield(self.done)
            return
        self.closing = True
        # Cleanup jobs retain their explicit deadline; business jobs roll back.
        for job in self.jobs:
            if not job.cleanup:
                job.cancelled.set()
        if self.thread is not None:
            await asyncio.gather(*(j.future for j in tuple(self.jobs)), return_exceptions=True)
            self.queue.put_nowait(None)
            await asyncio.shield(self.done)
            # done is set at the worker's return; join is now nonblocking.
            self.thread.join()
        self.closed = True
