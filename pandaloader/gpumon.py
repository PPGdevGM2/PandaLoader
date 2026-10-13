"""GPU telemetry for the worker cards.

Two sources, joined per CUDA index:

* ``nvidia-smi`` for name, VRAM used/total, temperature and the chip-wide
  utilisation (``utilization.gpu``).
* Windows performance counters ``GPU Engine(*)\\Utilization Percentage`` for
  the per-engine split Task Manager shows: the "Cuda" engine is what an Iray
  render loads, "3D" is ordinary graphics work. Counter instances carry the
  adapter LUID; ``D3DKMTEnumAdapters2`` maps a LUID to its PCI bus number and
  nvidia-smi reports the bus per CUDA index, which joins the two.

Everything is sampled by one background thread; readers get the last sample.
On a machine without nvidia-smi or without the counters the missing fields
are None and the UI shows "n/a".
"""
from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("pandaloader.gpumon")

_SMI_QUERY = "index,pci.bus_id,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _num(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:  # "[N/A]"
        return None


_SMI_FALLBACKS = [
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
]


def find_nvidia_smi() -> Optional[str]:
    """Path of nvidia-smi: on PATH, else the driver's usual install locations; None if absent."""
    import shutil
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for p in _SMI_FALLBACKS:
        if os.path.isfile(p):
            return p
    return None


NVIDIA_SMI_MISSING_TEXT = (
    "nvidia-smi was not found on this PC.\n\n"
    "It is installed with the NVIDIA driver (C:\\Windows\\System32\\nvidia-smi.exe). "
    "Without it PandaLoader cannot show GPU gauges or GPU names; rendering itself is not affected.\n\n"
    "Install or repair the NVIDIA driver, then restart PandaLoader."
)


def query_nvidia_smi() -> List[dict]:
    """One nvidia-smi sample per GPU, or [] when unavailable."""
    exe = find_nvidia_smi()
    if not exe:
        return []
    try:
        r = subprocess.run([exe, f"--query-gpu={_SMI_QUERY}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5, check=False, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in r.stdout.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 7 or not p[0].isdigit():
            continue
        try:
            bus = int(p[1].split(":")[1], 16)  # "00000000:01:00.0" -> 1
        except (IndexError, ValueError):
            bus = None
        out.append({"index": int(p[0]), "bus": bus, "name": p[2], "util": _num(p[3]),
                    "memUsed": _num(p[4]), "memTotal": _num(p[5]), "temp": _num(p[6]),
                    "cuda": None, "d3d": None})
    return out


# -- Windows: LUID -> PCI bus ------------------------------------------------------
def adapter_buses() -> Dict[str, int]:
    """{'0x00000000_0x00013963': 3, ...} for every display adapter with a PCI address."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class ADAPTERINFO(ctypes.Structure):
        _fields_ = [("hAdapter", wintypes.UINT), ("AdapterLuid", LUID), ("NumOfSources", wintypes.ULONG),
                    ("bPrecisePresentRegionsPreferred", wintypes.BOOL)]

    class ENUMADAPTERS2(ctypes.Structure):
        _fields_ = [("NumAdapters", wintypes.ULONG), ("pAdapters", ctypes.POINTER(ADAPTERINFO))]

    class ADAPTERADDRESS(ctypes.Structure):
        _fields_ = [("BusNumber", wintypes.UINT), ("DeviceNumber", wintypes.UINT), ("FunctionNumber", wintypes.UINT)]

    class QUERYADAPTERINFO(ctypes.Structure):
        _fields_ = [("hAdapter", wintypes.UINT), ("Type", wintypes.UINT), ("pPrivateDriverData", ctypes.c_void_p),
                    ("PrivateDriverDataSize", wintypes.UINT)]

    class CLOSEADAPTER(ctypes.Structure):
        _fields_ = [("hAdapter", wintypes.UINT)]

    KMTQAITYPE_ADAPTERADDRESS = 6
    out: Dict[str, int] = {}
    try:
        gdi32 = ctypes.windll.gdi32
        e = ENUMADAPTERS2(0, None)
        if gdi32.D3DKMTEnumAdapters2(ctypes.byref(e)) & 0xFFFFFFFF:
            return out
        arr = (ADAPTERINFO * e.NumAdapters)()
        e.pAdapters = arr
        if gdi32.D3DKMTEnumAdapters2(ctypes.byref(e)) & 0xFFFFFFFF:
            return out
        for i in range(e.NumAdapters):
            a = arr[i]
            addr = ADAPTERADDRESS()
            q = QUERYADAPTERINFO(a.hAdapter, KMTQAITYPE_ADAPTERADDRESS,
                                 ctypes.cast(ctypes.byref(addr), ctypes.c_void_p), ctypes.sizeof(addr))
            st = gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(q)) & 0xFFFFFFFF
            luid = f"0x{a.AdapterLuid.HighPart & 0xFFFFFFFF:08x}_0x{a.AdapterLuid.LowPart:08x}"  # lower case, see sample()
            if st == 0 and addr.BusNumber != 0xFFFFFFFF:
                out[luid] = addr.BusNumber
            gdi32.D3DKMTCloseAdapter(ctypes.byref(CLOSEADAPTER(a.hAdapter)))
    except Exception:  # noqa: BLE001
        log.debug("adapter enumeration failed", exc_info=True)
    return out


# -- Windows: GPU Engine counters ----------------------------------------------------
class EngineCounters:
    """Keeps a PDH query open; each sample() returns {luid: {engtype: percent}}."""

    _RX = re.compile(r"luid_(0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_phys_\d+_eng_\d+_engtype_(\w+)")
    PDH_FMT_DOUBLE = 0x00000200
    PDH_MORE_DATA = 0x800007D2

    def __init__(self):
        self.ok = False
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        self._ct, self._wt = ctypes, wintypes

        class FMT_VALUE(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class ITEM(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", FMT_VALUE)]

        self._ITEM = ITEM
        try:
            self._pdh = ctypes.windll.pdh
            self._query = ctypes.c_void_p()
            self._counter = ctypes.c_void_p()
            if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return
            path = "\\GPU Engine(*)\\Utilization Percentage"  # English names work on any locale
            if self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(self._counter)) != 0:
                return
            self._pdh.PdhCollectQueryData(self._query)  # first collect primes the rate counters
            self.ok = True
        except Exception:  # noqa: BLE001
            log.debug("PDH setup failed", exc_info=True)

    def sample(self) -> Dict[str, Dict[str, float]]:
        if not self.ok:
            return {}
        ct, wt = self._ct, self._wt
        try:
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return {}
            size, count = wt.DWORD(0), wt.DWORD(0)
            st = self._pdh.PdhGetFormattedCounterArrayW(self._counter, self.PDH_FMT_DOUBLE, ct.byref(size),
                                                        ct.byref(count), None) & 0xFFFFFFFF
            if st != self.PDH_MORE_DATA:
                return {}
            buf = ct.create_string_buffer(size.value)
            st = self._pdh.PdhGetFormattedCounterArrayW(self._counter, self.PDH_FMT_DOUBLE, ct.byref(size),
                                                        ct.byref(count), buf) & 0xFFFFFFFF
            if st != 0:
                return {}
            items = ct.cast(buf, ct.POINTER(self._ITEM))
            agg: Dict[str, Dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
            for i in range(count.value):
                it = items[i]
                if it.FmtValue.CStatus != 0:
                    continue
                m = self._RX.search(it.szName or "")
                if m and it.FmtValue.doubleValue > 0:
                    # Counter names spell the LUID in upper case (0x0001112D), adapter_buses() in
                    # lower case: compare in lower case, or LUIDs with hex letters never match.
                    agg[m.group(1).lower()][m.group(2)] += it.FmtValue.doubleValue
            return {luid: {k: min(100.0, v) for k, v in eng.items()} for luid, eng in agg.items()}
        except Exception:  # noqa: BLE001
            log.debug("PDH sample failed", exc_info=True)
            return {}

    def close(self) -> None:
        if self.ok:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except Exception:  # noqa: BLE001
                pass
            self.ok = False


# -- the monitor -----------------------------------------------------------------------
class GpuMonitor:
    """Background sampler; ``snapshot()`` returns the latest list of per-GPU dicts."""

    def __init__(self, interval_s: float = 2.0):
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._gpus: List[dict] = []
        self._engines: Optional[EngineCounters] = None
        self._luid_to_bus: Dict[str, int] = {}
        self._thread = threading.Thread(target=self._loop, name="gpu-monitor", daemon=True)

    def start(self) -> "GpuMonitor":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [dict(g) for g in self._gpus]

    def _loop(self) -> None:
        self._engines = EngineCounters()
        self._luid_to_bus, self._luid_checked = adapter_buses(), time.time()
        while not self._stop.is_set():
            gpus = query_nvidia_smi()
            if self._engines.ok and gpus:
                by_bus: Dict[int, Dict[str, float]] = {}
                sample = self._engines.sample()
                # LUIDs change on a driver reset: look them up again, at most once a minute
                # (a virtual adapter without a PCI bus is never in the table)
                if any(luid not in self._luid_to_bus for luid in sample) and time.time() - self._luid_checked > 60:
                    self._luid_to_bus, self._luid_checked = adapter_buses(), time.time()
                for luid, eng in sample.items():
                    bus = self._luid_to_bus.get(luid)
                    if bus is not None:
                        by_bus[bus] = eng
                for g in gpus:
                    eng = by_bus.get(g["bus"]) if g["bus"] is not None else None
                    if eng is not None or by_bus:  # counters present: missing engine = 0 %
                        eng = eng or {}
                        g["cuda"] = round(eng.get("Cuda", 0.0), 1)
                        g["d3d"] = round(eng.get("3D", 0.0), 1)
            with self._lock:
                self._gpus = gpus
            self._stop.wait(self.interval_s)
        self._engines.close()


_MONITOR: Optional[GpuMonitor] = None
_MONITOR_LOCK = threading.Lock()


def gpus() -> List[dict]:
    """Latest sample; starts the monitor on first use."""
    global _MONITOR
    with _MONITOR_LOCK:
        if _MONITOR is None:
            _MONITOR = GpuMonitor().start()
    return _MONITOR.snapshot()
