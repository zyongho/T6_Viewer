"""Conservative Windows memory admission for playback, preload and analysis.

The budget covers the whole application: the UI process plus every process it
started (analysis workers, QtWebEngine helpers).
It is a soft ceiling enforced by admission and shedding, not an OS limit.
Priority when memory is short: current playback > next-clip preload >
background analysis.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time


MIB = 1024 * 1024
TOTAL_BUDGET = 4608 * MIB  # 4.5 GiB for the whole process tree.
PROCESS_BUDGET = TOTAL_BUDGET  # compatibility name
SHED_MARGIN = 128 * MIB  # start releasing optional memory this close to the ceiling
# New optional work is only admitted below this, leaving hysteresis between
# admitting and shedding so the two never oscillate.
GROWTH_CEILING = TOTAL_BUDGET - 2 * SHED_MARGIN
# Measured on 2896x1876 H.264 Tesla clips (hardware decode): one paused
# QMediaPlayer holding its first frame costs ~125 MiB.
PRELOAD_ESTIMATE = 160 * MIB
PRELOAD_RESERVE = 384 * MIB
# One analysis process (~70 MiB with a single BLAS thread and YOLOX loaded)
# plus its keyframe-only ffmpeg decoder (~60-95 MiB).
WORKER_ESTIMATE = 192 * MIB
LOW_SYSTEM_MEMORY = 1536 * MIB
CRITICAL_SYSTEM_MEMORY = 512 * MIB


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260),
    ]


_TH32CS_SNAPPROCESS = 0x2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_api = None
# Enumerating every process on the system costs ~20 ms; the set of our
# descendants changes rarely, so it is refreshed at most this often.
_TREE_REFRESH = 1.0
_SNAPSHOT_REUSE = 0.15
_cache_lock = threading.Lock()
_tree_cache: tuple[float, list[int]] = (0.0, [])
_snapshot_cache: tuple[float, tuple[int | None, int | None]] = (0.0, (None, None))


def _windows_api():
    global _api
    if _api is None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel.CreateToolhelp32Snapshot.argtypes = (ctypes.c_ulong, ctypes.c_ulong)
        kernel.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))
        kernel.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_ulong)
        _api = kernel, psapi
    return _api


def _private_bytes(psapi, handle) -> int | None:
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return int(counters.PrivateUsage)
    return None


def _descendant_pids(kernel, root: int) -> list[int]:
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE:
        return []
    children: dict[int, list[int]] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ProcessID != entry.th32ParentProcessID:
                children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    found: list[int] = []
    pending = [root]
    seen = {root}
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in seen:
                seen.add(child)
                found.append(child)
                pending.append(child)
    return found


def memory_snapshot(max_age: float | None = None) -> tuple[int | None, int | None]:
    """Return private bytes of this process *and all its descendants*, and
    the machine's available physical RAM.

    With ``start_sampler()`` running the latest background reading is
    returned without measuring on the calling thread. Otherwise, or with an
    explicit ``max_age``, a reading younger than ``max_age`` is reused.
    """
    global _snapshot_cache
    if os.name != "nt":
        return None, None
    now = time.monotonic()
    with _cache_lock:
        stamp, cached = _snapshot_cache
        if max_age is None and _sampler is not None and stamp:
            return cached
        if now - stamp < (_SNAPSHOT_REUSE if max_age is None else max_age):
            return cached
    result = _measure()
    with _cache_lock:
        _snapshot_cache = (time.monotonic(), result)
    return result


_SAMPLER_INTERVAL = 0.25
_sampler: threading.Thread | None = None


def start_sampler() -> None:
    """Measure in a background thread so the GUI thread never waits for it.

    Enumerating processes can take 100+ ms on a busy system; a stalled GUI
    thread makes Qt's video pipeline buffer frames (see main_window).
    """
    global _sampler
    if os.name != "nt" or _sampler is not None:
        return

    def run() -> None:
        global _snapshot_cache
        while True:
            result = _measure()
            with _cache_lock:
                _snapshot_cache = (time.monotonic(), result)
            time.sleep(_SAMPLER_INTERVAL)

    _sampler = threading.Thread(target=run, name="memory-sampler", daemon=True)
    _sampler.start()


def _measure() -> tuple[int | None, int | None]:
    global _tree_cache
    try:
        kernel, psapi = _windows_api()
        total = _private_bytes(psapi, kernel.GetCurrentProcess())
        if total is not None:
            now = time.monotonic()
            with _cache_lock:
                stamp, pids = _tree_cache
            if now - stamp >= _TREE_REFRESH:
                pids = _descendant_pids(kernel, os.getpid())
                with _cache_lock:
                    _tree_cache = (now, pids)
            for pid in pids:
                handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid)
                if not handle:
                    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not handle:
                    continue  # exited between snapshot and open
                try:
                    total += _private_bytes(psapi, handle) or 0
                finally:
                    kernel.CloseHandle(handle)
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        available = int(status.ullAvailPhys) if kernel.GlobalMemoryStatusEx(ctypes.byref(status)) else None
        return total, available
    except (AttributeError, OSError, ValueError):
        return None, None


def playback_reserve(rate: float, channels: int) -> int:
    """Headroom kept for decoder buffer growth of the playing channels.

    Measured peaks over steady state with six channels: ~200 MiB at 1x,
    ~540 MiB at 8x and over 1 GiB at 16x.
    """
    per_channel = 48 if rate <= 2 else 96 if rate <= 4 else 128 if rate <= 8 else 192
    return max(1, channels) * per_channel * MIB


def headroom(used: int | None, available: int | None, reserve: int = 0) -> int:
    """Bytes that may still be allocated by optional work."""
    room = GROWTH_CEILING - reserve - (used or 0)
    if available is not None:
        room = min(room, available - LOW_SYSTEM_MEMORY)
    return max(0, int(room))


def preload_slots(process_bytes: int | None, available_bytes: int | None, wanted: int,
                  reserve: int = PRELOAD_RESERVE) -> int:
    """Admit only decoders with room for their buffers and a safety margin."""
    if wanted <= 0:
        return 0
    if available_bytes is not None and available_bytes <= LOW_SYSTEM_MEMORY:
        return 0
    return max(0, min(wanted, headroom(process_bytes, available_bytes, reserve) // PRELOAD_ESTIMATE))


def worker_slots(used: int | None, available: int | None, running: int, maximum: int,
                 reserve: int = 0, starting: int = 0) -> int:
    """Target number of analysis processes.

    ``starting`` workers were admitted recently and have not reached their
    steady-state footprint in ``used`` yet, so their estimate is charged now.
    Never shrinks here; shedding is decided by ``over_budget``.
    """
    room = headroom(used, available, reserve) - starting * WORKER_ESTIMATE
    grow = max(0, room // WORKER_ESTIMATE)
    return max(0, min(maximum, running + grow))


def over_budget(used: int | None, available: int | None) -> bool:
    return bool((used is not None and used >= TOTAL_BUDGET - SHED_MARGIN)
                or (available is not None and available <= LOW_SYSTEM_MEMORY // 2))


def critical(used: int | None, available: int | None) -> bool:
    return bool((used is not None and used >= TOTAL_BUDGET + 256 * MIB)
                or (available is not None and available < CRITICAL_SYSTEM_MEMORY))

