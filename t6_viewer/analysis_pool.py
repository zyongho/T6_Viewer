"""Priority job queue served by a resizable set of analysis processes.

The UI decides *how many* processes may run (from CPU count and the memory
budget) with ``set_target``; the pool never grows on its own. Pending work
can be reprioritized at any time without disturbing jobs already running.
``shed`` kills processes immediately to give memory back, re-queueing the
interrupted jobs so no work is lost.
"""

from __future__ import annotations

import bisect
import ctypes
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass(order=True)
class Job:
    priority: tuple
    key: str = field(compare=False)
    kind: str = field(compare=False)
    args: tuple = field(compare=False)
    meta: object = field(default=None, compare=False)


def _lower_own_priority() -> None:
    if os.name != "nt":
        return
    try:
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
    except (AttributeError, OSError):
        pass


def _worker_main(conn) -> None:
    """Entry point of a spawned analysis process."""
    _lower_own_priority()
    from .analysis_jobs import HANDLERS

    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            break
        if message is None:
            break
        kind, args = message
        try:
            result = HANDLERS[kind](*args)
        except Exception as exc:  # report, keep the process serving
            result = {"level": "unknown", "intervals": [], "reason": str(exc)}
        try:
            conn.send(result)
        except (OSError, ValueError):
            break


class _Slot:
    def __init__(self, context) -> None:
        self.conn, child_conn = context.Pipe()
        self.process = context.Process(target=_worker_main, args=(child_conn,), daemon=True)
        self._child_conn = child_conn
        self.started_at = time.monotonic()
        self.retiring = False
        self.killed = False
        self.job: Job | None = None


class AnalysisPool:
    def __init__(self, on_result: Callable[[Job, object], None]):
        self._on_result = on_result
        self._context = multiprocessing.get_context("spawn")
        self._cond = threading.Condition()
        self._pending: dict[str, list[Job]] = {}
        self._running: dict[str, Job] = {}
        self._slots: list[_Slot] = []
        self._closed = False

    # -- queue -------------------------------------------------------------
    def replace(self, kind: str, jobs: list[Job]) -> None:
        """Replace every pending job of ``kind``; running jobs are kept."""
        with self._cond:
            seen: set[str] = set()
            pending = []
            for job in sorted(jobs):
                if job.key not in self._running and job.key not in seen:
                    seen.add(job.key)
                    pending.append(job)
            self._pending[kind] = pending
            self._cond.notify_all()

    def clear(self) -> None:
        with self._cond:
            self._pending.clear()

    def pending_count(self, kind: str | None = None) -> int:
        with self._cond:
            if kind is not None:
                return len(self._pending.get(kind, ()))
            return sum(len(jobs) for jobs in self._pending.values())

    def running_count(self) -> int:
        with self._cond:
            return len(self._running)

    def _pop_best(self) -> Job | None:
        best_kind = None
        for kind, jobs in self._pending.items():
            if jobs and (best_kind is None or jobs[0] < self._pending[best_kind][0]):
                best_kind = kind
        return self._pending[best_kind].pop(0) if best_kind is not None else None

    def _has_pending(self) -> bool:
        return any(self._pending.values())

    # -- processes ---------------------------------------------------------
    def worker_count(self) -> int:
        with self._cond:
            return sum(1 for slot in self._slots if not slot.retiring)

    def starting_count(self, window: float = 3.0) -> int:
        """Workers too young to show their steady-state memory yet."""
        now = time.monotonic()
        with self._cond:
            return sum(1 for slot in self._slots
                       if not slot.retiring and now - slot.started_at < window)

    def set_target(self, count: int) -> None:
        """Grow to ``count`` processes, or retire extras after their job.

        Processes are only started while there is work for them.
        """
        with self._cond:
            if self._closed:
                return
            live = [slot for slot in self._slots if not slot.retiring]
            wanted = min(max(0, count), len(live) + self._backlog())
            if len(live) > max(0, count):
                # Retire idle processes first, then the newest busy ones.
                extra = len(live) - max(0, count)
                for slot in sorted(live, key=lambda item: (item.job is not None, -item.started_at))[:extra]:
                    slot.retiring = True
                self._cond.notify_all()
                return
            for _ in range(wanted - len(live)):
                slot = _Slot(self._context)
                self._slots.append(slot)
                threading.Thread(target=self._serve, args=(slot,), daemon=True,
                                 name="analysis-slot").start()

    def _backlog(self) -> int:
        idle = sum(1 for slot in self._slots if not slot.retiring and slot.job is None)
        return max(0, sum(len(jobs) for jobs in self._pending.values()) - idle)

    def shed(self, count: int) -> int:
        """Kill up to ``count`` processes now (newest first); return killed."""
        with self._cond:
            victims = sorted((slot for slot in self._slots if not slot.killed),
                             key=lambda item: -item.started_at)[:max(0, count)]
            for slot in victims:
                slot.retiring = True
                slot.killed = True
            self._cond.notify_all()
        for slot in victims:
            try:
                if slot.process.is_alive():
                    slot.process.kill()
            except (OSError, ValueError, AttributeError):
                pass
        return len(victims)

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            self._pending.clear()
            count = len(self._slots)
        self.shed(count)

    def _requeue(self, job: Job) -> None:
        bisect.insort(self._pending.setdefault(job.kind, []), job)

    def _serve(self, slot: _Slot) -> None:
        try:
            slot.process.start()
            slot._child_conn.close()
        except (OSError, RuntimeError, ValueError):
            with self._cond:
                if slot in self._slots:
                    self._slots.remove(slot)
            return
        try:
            while True:
                with self._cond:
                    while not (self._closed or slot.retiring or self._has_pending()):
                        self._cond.wait()
                    if self._closed or slot.retiring:
                        break
                    job = self._pop_best()
                    if job is None:
                        continue
                    self._running[job.key] = job
                    slot.job = job
                try:
                    slot.conn.send((job.kind, job.args))
                    while not slot.conn.poll(0.2):
                        if slot.killed or not slot.process.is_alive():
                            raise EOFError
                    result = slot.conn.recv()
                except (EOFError, OSError, ValueError):
                    with self._cond:
                        self._running.pop(job.key, None)
                        slot.job = None
                        if not self._closed:
                            self._requeue(job)
                            self._cond.notify_all()
                    break
                with self._cond:
                    self._running.pop(job.key, None)
                    slot.job = None
                try:
                    self._on_result(job, result)
                except Exception:
                    pass
        finally:
            with self._cond:
                if slot in self._slots:
                    self._slots.remove(slot)
            try:
                if not slot.killed:
                    slot.conn.send(None)
                    slot.process.join(2)
                if slot.process.is_alive():
                    slot.process.kill()
                slot.process.join(2)
                slot.conn.close()
            except (OSError, ValueError, AttributeError):
                pass
