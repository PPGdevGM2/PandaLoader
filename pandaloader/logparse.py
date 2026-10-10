"""Parse the live log.txt of a DAZ Studio worker instance.

The log carries everything the manager needs for progress and failure
detection, so the job script does not have to report anything itself:

    [INFO] :: Begin asset load (open): /D:/.../Scene12055.duf
    [INFO] :: Rendering image...
    Iray [INFO] - IRAY:RENDER :: 1.0 IRAY rend info : Received update to 00625.54 iterations after 21.621 s.
    Iray [INFO] - IRAY:RENDER :: 1.0 IRAY rend progr: Convergence threshold reached.
    Iray [INFO] - IRAY:RENDER :: 1.0 IRAY rend info : Device statistics at rendering done:
    Iray [INFO] - IRAY:RENDER :: 1.0 IRAY rend info :    CUDA device 0 (NVIDIA GeForce RTX 4090): 1784.0 spp, ...
    [INFO] :: Saved image: C:\\...\\out.png
    [INFO] :: Finished rendering.
    [INFO] :: Script executed successfully: ...
    --------------- Daz Studio 6.25.2026.14722 exited ------------------

Fatal lines seen in practice:

    rend error: Cannot render: found no usable devices. ...
    rend warn : There is no CUDA-capable GPU available to the iray photoreal renderer.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

# Phases, in the order a healthy job passes through them.
PHASE_STARTING = "starting"
PHASE_LOADING = "loading"
PHASE_RENDERING = "rendering"
PHASE_SAVING = "saving"
PHASE_FINISHED = "finished"
PHASE_EXITED = "exited"

_RE_STARTING = re.compile(r"\+{3,} Daz Studio (\S+) starting")
_RE_EXITED = re.compile(r"-{3,} Daz Studio \S+ exited")
_RE_CUDA_FOUND = re.compile(r"Found (\d+) CUDA device")
_RE_SCRIPT_LOAD = re.compile(r"Loading script: (.+)$")
_RE_SCRIPT_OK = re.compile(r"Script executed successfully")
_RE_ASSET_LOAD = re.compile(r"Begin asset load \(open\): (.+)$")
_RE_RENDER_START = re.compile(r"Rendering image\.\.\.")
_RE_ITER = re.compile(r"Received update to ([0-9.]+) iterations? after ([0-9.]+) s")
_RE_CONVERGED = re.compile(r"Convergence threshold reached")
_RE_DEVSTATS = re.compile(r"Device statistics at rendering done")
_RE_DEVLINE = re.compile(r"rend info :\s+(CUDA device \d+ \([^)]*\)|CPU)\s*:\s+([0-9.]+) spp")
_RE_SAVED = re.compile(r"Saved image: (.+)$")
_RE_FINISHED = re.compile(r"Finished rendering")
_RE_TOTAL_TIME = re.compile(r"Total Rendering Time: ([0-9.]+) seconds")
_RE_FATAL = re.compile(
    r"Cannot render|no CUDA-capable GPU|found no usable devices|rend error|Script Error|"
    r"Error in script|uncaught exception|\[ERROR\] .*script",
    re.IGNORECASE,
)


_RE_ERR_TAIL = re.compile(r"rend (?:error|warn)\s*:\s*(.+)$")
_RE_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def session_start_time(line: str) -> Optional[float]:
    """If the line is the 'Daz Studio ... starting' banner, return its local timestamp (epoch seconds)."""
    if not _RE_STARTING.search(line):
        return None
    m = _RE_STAMP.match(line)
    if not m:
        return 0.0
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0.0


def _error_text(line: str) -> str:
    """Reduce a log line to its human part: drop timestamp, source file and Iray prefixes."""
    m = _RE_ERR_TAIL.search(line)
    if m:
        return m.group(1).strip()[:300]
    if "::" in line:
        return line.split("::", 1)[1].strip()[:300]
    return line.strip()[:300]


@dataclass
class Progress:
    """Mutable snapshot of one worker run, updated line by line."""
    phase: str = PHASE_STARTING
    daz_version: Optional[str] = None
    cuda_devices: Optional[int] = None
    script: Optional[str] = None
    script_ok: bool = False
    loading_file: Optional[str] = None
    iterations: float = 0.0
    render_elapsed: float = 0.0       # seconds, as reported by Iray
    converged: bool = False
    devices: List[tuple] = field(default_factory=list)  # (name, spp)
    saved_image: Optional[str] = None
    finished: bool = False
    total_render_time: Optional[float] = None
    exited: bool = False
    errors: List[str] = field(default_factory=list)
    # wall-clock timestamps (time.time()) of the key transitions
    t_render_start: Optional[float] = None
    t_finished: Optional[float] = None
    t_last_line: Optional[float] = None
    lines: int = 0

    @property
    def cpu_rendered(self) -> bool:
        return any(name == "CPU" and spp > 0 for name, spp in self.devices)

    @property
    def render_secs(self) -> Optional[float]:
        """Best available render duration: DAZ total, else Iray's last update time, else wall clock."""
        if self.total_render_time:
            return self.total_render_time
        if self.render_elapsed:
            return self.render_elapsed
        if self.t_render_start and self.t_finished:
            return self.t_finished - self.t_render_start
        return None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase, "iterations": self.iterations, "renderElapsed": self.render_elapsed,
            "converged": self.converged, "savedImage": self.saved_image, "finished": self.finished,
            "exited": self.exited, "errors": list(self.errors), "cudaDevices": self.cuda_devices,
            "devices": [list(d) for d in self.devices], "cpuRendered": self.cpu_rendered,
        }


class LogParser:
    """Feed log lines, get a Progress object and event names back."""

    def __init__(self, progress: Optional[Progress] = None, clock: Callable[[], float] = time.time):
        self.progress = progress or Progress()
        self._clock = clock
        self._in_devstats = False

    def feed(self, line: str) -> Optional[str]:
        """Update progress from one log line. Returns an event name or None."""
        p = self.progress
        now = self._clock()
        p.t_last_line = now
        p.lines += 1
        line = line.rstrip("\r\n")

        if self._in_devstats:
            m = _RE_DEVLINE.search(line)
            if m:
                p.devices.append((m.group(1), float(m.group(2))))
                return "device"
            self._in_devstats = False  # block ended

        m = _RE_ITER.search(line)
        if m:
            p.iterations = float(m.group(1))
            p.render_elapsed = float(m.group(2))
            if p.phase in (PHASE_STARTING, PHASE_LOADING):
                p.phase = PHASE_RENDERING
                p.t_render_start = p.t_render_start or now
            return "iteration"
        if _RE_RENDER_START.search(line):
            p.phase = PHASE_RENDERING
            p.t_render_start = now
            return "render_start"
        if _RE_CONVERGED.search(line):
            p.converged = True
            return "converged"
        if _RE_DEVSTATS.search(line):
            self._in_devstats = True
            p.phase = PHASE_SAVING
            return "devstats"
        m = _RE_SAVED.search(line)
        if m:
            p.saved_image = m.group(1).strip()
            p.phase = PHASE_SAVING
            return "saved"
        if _RE_FINISHED.search(line):
            p.finished = True
            p.t_finished = now
            p.phase = PHASE_FINISHED
            return "finished"
        m = _RE_TOTAL_TIME.search(line)
        if m:
            p.total_render_time = float(m.group(1))
            return "total_time"
        m = _RE_ASSET_LOAD.search(line)
        if m:
            p.loading_file = m.group(1).strip()
            if p.phase == PHASE_STARTING:
                p.phase = PHASE_LOADING
            return "loading"
        m = _RE_SCRIPT_LOAD.search(line)
        if m:
            p.script = m.group(1).strip()
            return "script_start"
        if _RE_SCRIPT_OK.search(line):
            p.script_ok = True
            return "script_ok"
        m = _RE_CUDA_FOUND.search(line)
        if m:
            p.cuda_devices = int(m.group(1))
            return "cuda"
        m = _RE_STARTING.search(line)
        if m:
            p.daz_version = m.group(1)
            return "starting"
        if _RE_EXITED.search(line):
            p.exited = True
            p.phase = PHASE_EXITED
            return "exited"
        if _RE_FATAL.search(line):
            p.errors.append(_error_text(line))
            return "error"
        return None

    def feed_many(self, lines) -> List[str]:
        events = []
        for ln in lines:
            ev = self.feed(ln)
            if ev:
                events.append(ev)
        return events


class LogTail:
    """Incrementally read new complete lines appended to a file.

    DAZ appends to log.txt across runs, so the tail starts at the current end
    of file. A shrinking file resets the offset to the start: DAZ trims the
    head of a large log when it launches, so callers must additionally gate on
    the session banner (see ``session_start_time``) to skip old sessions.
    """

    def __init__(self, path, start_at_end: bool = True, encoding: str = "utf-8"):
        self.path = Path(path)
        self.encoding = encoding
        self._buf = b""
        self.offset = 0
        if start_at_end and self.path.exists():
            self.offset = self.path.stat().st_size

    def read_new_lines(self) -> List[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:  # truncated or rotated
            self.offset = 0
            self._buf = b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read(size - self.offset)
        self.offset = size
        data = self._buf + chunk
        parts = data.split(b"\n")
        self._buf = parts.pop()  # incomplete trailing line, if any
        return [p.decode(self.encoding, errors="replace") for p in parts]

    def flush_partial(self) -> List[str]:
        """Return the buffered partial line (process exited without newline)."""
        if not self._buf:
            return []
        line, self._buf = self._buf, b""
        return [line.decode(self.encoding, errors="replace")]
