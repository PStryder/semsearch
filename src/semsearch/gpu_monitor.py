"""GPU courtesy: is anyone else using the bulk GPU right now?

Reads the Windows performance counters ``\\GPU Engine(*)\\Utilization Percentage``. Each
instance is named ``pid_<pid>_luid_0x<hi>_0x<lo>_phys_<n>_eng_<n>_engtype_<Type>``, so the
utilization of a given adapter (by LUID) by processes other than our own can be summed. This
works for every vendor and needs no vendor tooling. Sampling is cheap but the counter needs
two reads a short interval apart; the monitor keeps a persistent query and samples at most
every ``min_interval_s``.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)
_INST = re.compile(r"pid_(\d+)_luid_0x([0-9a-f]+)_0x([0-9a-f]+)_phys_\d+_eng_\d+_engtype_(\w+)", re.I)


def parse_instance(name: str) -> tuple[int, str, str] | None:
    """(pid, luid 'hi-lo' as devices.py formats it, engine type) or None."""
    m = _INST.search(name)
    if not m:
        return None
    pid, hi, lo, eng = m.groups()
    return int(pid), f"{int(hi, 16):08x}-{int(lo, 16):08x}", eng


def busy_by_others(samples: list[tuple[str, float]], luid: str, my_pid: int) -> float:
    """Sum of utilization (percent) on `luid` from other processes' 3D/Compute engines."""
    total = 0.0
    for name, value in samples:
        p = parse_instance(name)
        if p is None:
            continue
        pid, inst_luid, eng = p
        if inst_luid != luid.lower() or pid == my_pid:
            continue
        if eng.lower() in ("3d", "compute", "compute_0", "compute_1", "cuda", "graphics_1", "copy"):
            total += float(value)
    return total


class GpuMonitor:
    def __init__(self, min_interval_s: float = 5.0):
        self.min_interval_s = min_interval_s
        self._lock = threading.Lock()
        self._last_t = 0.0
        self._last: dict[str, float] = {}
        self._hq = None
        self._hc = None
        self._ok = True

    def _open(self) -> bool:
        if self._hq is not None:
            return True
        try:
            import win32pdh
            self._hq = win32pdh.OpenQuery()
            self._hc = win32pdh.AddEnglishCounter(self._hq, r"\GPU Engine(*)\Utilization Percentage")
            win32pdh.CollectQueryData(self._hq)  # first sample has no rate yet
            time.sleep(0.2)
            return True
        except Exception as e:  # noqa: BLE001
            log.info("GPU utilization counters unavailable (%s); GPU courtesy disabled", e)
            self._ok = False
            return False

    def sample(self) -> list[tuple[str, float]]:
        """[(instance name, utilization %)] for all GPU engine instances; cached per interval."""
        with self._lock:
            now = time.time()
            if now - self._last_t < self.min_interval_s and self._last:
                return list(self._last.items())
            if not self._ok or not self._open():
                return []
            try:
                import win32pdh
                win32pdh.CollectQueryData(self._hq)
                res = win32pdh.GetFormattedCounterArray(self._hc, win32pdh.PDH_FMT_DOUBLE)
                items = res[1] if isinstance(res, tuple) else res  # pywin32 builds differ: dict, or (count, dict)
                self._last = {k: float(v) for k, v in items.items()}
                self._last_t = now
                return list(self._last.items())
            except Exception as e:  # noqa: BLE001
                log.debug("GPU counter read failed: %s", e)
                return []

    def others_busy_percent(self, luid: str) -> float:
        return busy_by_others(self.sample(), luid, os.getpid())
