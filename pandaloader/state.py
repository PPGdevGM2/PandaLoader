"""Shared, thread-safe status model.

The scheduler and workers write into it; the CLI, web UI and tray icon read
``snapshot()``. Nothing here knows about DAZ, so it stays trivially testable.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional


@dataclass
class WorkerStatus:
    name: str
    gpu: Optional[int]
    state: str = "idle"                 # idle, starting, loading, rendering, saving, finished, killing
    scene_index: Optional[int] = None
    scene_name: str = ""
    target: str = ""
    camera: str = ""
    job_started: Optional[float] = None
    render_started: Optional[float] = None
    iterations: float = 0.0
    render_elapsed: float = 0.0
    expected_secs: float = 0.0          # render-only estimate for this scene
    pid: Optional[int] = None
    last_error: str = ""
    jobs_done: int = 0
    jobs_failed: int = 0

    def eta_s(self, startup_overhead: float, now: Optional[float] = None) -> Optional[float]:
        """Estimated seconds until this job finishes, or None when idle."""
        if self.job_started is None or self.state == "idle":
            return None
        now = now or time.time()
        if self.render_started is not None:
            remaining = self.expected_secs - (now - self.render_started)
        else:
            remaining = self.expected_secs + startup_overhead - (now - self.job_started)
        return max(0.0, remaining)

    def to_dict(self, startup_overhead: float, now: Optional[float] = None) -> dict:
        now = now or time.time()
        return {
            "name": self.name, "gpu": self.gpu, "state": self.state, "sceneIndex": self.scene_index,
            "sceneName": self.scene_name, "target": self.target, "camera": self.camera,
            "jobStarted": self.job_started, "renderStarted": self.render_started,
            "jobElapsed": (now - self.job_started) if self.job_started else 0.0,
            "renderElapsed": (now - self.render_started) if self.render_started else 0.0,
            "iterations": self.iterations, "expectedSecs": self.expected_secs,
            "eta": self.eta_s(startup_overhead, now), "pid": self.pid, "lastError": self.last_error,
            "jobsDone": self.jobs_done, "jobsFailed": self.jobs_failed,
        }


class Status:
    """Process-wide status: workers, queue counts, event log, control flags."""

    def __init__(self, startup_overhead_s: float = 70.0, default_render_secs: float = 510.0, max_events: int = 500):
        self._lock = threading.RLock()
        self.workers: Dict[str, WorkerStatus] = {}
        self.queue_path: str = ""
        self.counts: dict = {"total": 0, "pending": 0, "running": 0, "done": 0, "failed": 0}
        self.average_secs: Optional[float] = None
        self.events: Deque[dict] = deque(maxlen=max_events)
        self.paused = False
        self.running = False
        self.finished = False
        self.shutdown_after_queue = False
        self.started_at: Optional[float] = None
        self.startup_overhead_s = startup_overhead_s
        self.default_render_secs = default_render_secs
        self.listeners: List[Callable[[dict], None]] = []  # called with each new event dict

    # -- mutation --------------------------------------------------------------
    def add_worker(self, name: str, gpu: Optional[int]) -> WorkerStatus:
        with self._lock:
            ws = WorkerStatus(name=name, gpu=gpu)
            self.workers[name] = ws
            return ws

    def update_worker(self, name: str, **fields) -> None:
        with self._lock:
            ws = self.workers[name]
            for k, v in fields.items():
                setattr(ws, k, v)

    def worker_idle(self, name: str) -> None:
        with self._lock:
            ws = self.workers[name]
            ws.state = "idle"
            ws.scene_index = None
            ws.scene_name = ws.target = ws.camera = ""
            ws.job_started = ws.render_started = None
            ws.iterations = ws.render_elapsed = 0.0
            ws.pid = None

    def set_queue(self, path: str, counts: dict, average_secs: Optional[float]) -> None:
        with self._lock:
            self.queue_path = path
            self.counts = dict(counts)
            self.average_secs = average_secs

    def log(self, message: str, level: str = "info", worker: Optional[str] = None) -> None:
        ev = {"t": time.time(), "level": level, "worker": worker, "msg": message}
        with self._lock:
            self.events.append(ev)
            listeners = list(self.listeners)
        for fn in listeners:
            try:
                fn(ev)
            except Exception:
                pass

    # -- read ------------------------------------------------------------------
    def expected_for(self, last_secs: Optional[float]) -> float:
        with self._lock:
            return float(last_secs or self.average_secs or self.default_render_secs)

    def queue_eta_s(self, now: Optional[float] = None) -> Optional[float]:
        """Rough time until the queue is empty, assuming all workers keep going."""
        with self._lock:
            now = now or time.time()
            active = [w for w in self.workers.values() if w.state != "idle"]
            slots = max(1, len(self.workers))
            per_job = self.expected_for(None) + self.startup_overhead_s
            pending = self.counts.get("pending", 0)
            current = [w.eta_s(self.startup_overhead_s, now) or 0.0 for w in active]
            if not active and pending == 0:
                return 0.0 if self.finished or self.counts.get("total", 0) else None
            tail = pending * per_job / slots
            head = max(current) if current else 0.0
            return head + tail

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "now": now,
                "startedAt": self.started_at,
                "running": self.running,
                "paused": self.paused,
                "finished": self.finished,
                "shutdownAfterQueue": self.shutdown_after_queue,
                "queue": {"path": self.queue_path, **self.counts, "averageSecs": self.average_secs,
                          "eta": self.queue_eta_s(now)},
                "workers": [w.to_dict(self.startup_overhead_s, now) for w in self.workers.values()],
                "events": list(self.events)[-100:],
            }

    def tooltip(self, max_len: int = 127) -> str:
        """Compact multi-line summary for a tray tooltip (Windows caps it at 127 chars)."""
        with self._lock:
            c = self.counts
            eta = self.queue_eta_s()
            eta_txt = f"ETA {_hms(eta)}" if eta else ("paused" if self.paused else ("done" if self.finished else ""))
            head = f"PandaLoader {c.get('done', 0)}/{c.get('total', 0)} {eta_txt}".strip()
            rows = [head]
            for w in self.workers.values():
                if w.state == "idle":
                    rows.append(f"{w.name}: idle")
                else:
                    rows.append(f"{w.name}: {w.scene_name[:14]} {int(w.iterations)}it {w.state}")
            text = "\n".join(rows)
            return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _hms(secs: Optional[float]) -> str:
    if secs is None:
        return "--:--"
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
