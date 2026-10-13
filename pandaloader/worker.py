"""Run one render job in a fresh DAZ Studio process and watch it to completion.

One process per job (RENDERQUEUE_MANAGER.md, rule 3 and 5): the process is
launched with its own instance name and, when pinned, CUDA_VISIBLE_DEVICES.
Progress comes from tailing the instance log. The outcome is judged from the
exit, the result JSON the job script writes, and the output file on disk.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config, WorkerSlot
from .jobgen import JobSpec
from .logparse import LogParser, LogTail, Progress, session_start_time

EventCallback = Callable[[str, Progress], None]


@dataclass
class JobResult:
    ok: bool
    reason: str
    exit_code: Optional[int]
    killed: bool
    duration_s: float
    render_secs: Optional[float]
    iterations: float
    script_result: Optional[dict]
    progress: Progress
    warnings: List[str] = field(default_factory=list)
    log_tail: List[str] = field(default_factory=list)
    frames_ok: List[int] = field(default_factory=list)   # animation: frames with a fresh output file

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "reason": self.reason, "exitCode": self.exit_code, "killed": self.killed,
            "durationS": self.duration_s, "renderSecs": self.render_secs, "iterations": self.iterations,
            "scriptResult": self.script_result, "warnings": list(self.warnings), "progress": self.progress.to_dict(),
            "framesOk": list(self.frames_ok),
        }


def kill_process_tree(proc: subprocess.Popen, timeout: float = 30.0) -> None:
    """Kill the process and anything it spawned. taskkill /T handles the tree on Windows."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


class WorkerRun:
    """Launch and supervise a single job. ``run()`` blocks until the process is gone."""

    def __init__(self, cfg: Config, slot: WorkerSlot, spec: JobSpec, job_path, on_event: Optional[EventCallback] = None):
        self.cfg = cfg
        self.slot = slot
        self.spec = spec
        self.job_path = Path(job_path)
        self.on_event = on_event
        self.proc: Optional[subprocess.Popen] = None
        self._stop_requested = False
        self._recent: List[str] = []

    # -- launch description ----------------------------------------------------
    def command(self) -> List[str]:
        subst = {"instance": self.slot.name, "job": str(self.job_path)}
        return [self.cfg.daz_exe] + [a.format(**subst) for a in self.cfg.daz_args]

    def environment(self) -> dict:
        env = dict(os.environ)
        gpus = self.slot.gpu_list
        if gpus is None:
            env.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            env["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in gpus)
        env["PANDALOADER_INSTANCE"] = self.slot.name
        return env

    def request_stop(self) -> None:
        self._stop_requested = True

    # -- main loop -------------------------------------------------------------
    def run(self) -> JobResult:
        cfg = self.cfg
        result_file = Path(self.spec.result_file)
        target = Path(self.spec.target)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if result_file.exists():
            result_file.unlink()

        log_path = cfg.instance_log(self.slot.name)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        tail = LogTail(log_path, start_at_end=True)
        parser = LogParser()
        progress = parser.progress

        start = time.time()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            self.command(), env=self.environment(), cwd=str(Path(cfg.work_dir)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._emit("launched", progress)

        killed = False
        reason = ""
        last_activity = start
        # Only lines from this process's own session count. DAZ trims the head of
        # a big log at startup, which makes the tail restart from byte 0 and would
        # otherwise replay old sessions (and their errors).
        session_ts_min = start - 15.0
        armed = {"ok": False}

        def own_session(lines: List[str]) -> List[str]:
            if armed["ok"]:
                return lines
            out: List[str] = []
            for ln in lines:
                if not armed["ok"]:
                    ts = session_start_time(ln)
                    if ts is not None and ts >= session_ts_min:
                        armed["ok"] = True
                        out.append(ln)
                else:
                    out.append(ln)
            return out

        while True:
            exited = self.proc.poll() is not None
            lines = tail.read_new_lines()
            if exited:
                lines += tail.flush_partial()
            lines = own_session(lines)
            if lines:
                last_activity = time.time()
                self._remember(lines)
                for ev in parser.feed_many(lines):
                    self._emit(ev, progress)
            self._emit("poll", progress)
            if exited:
                break
            now = time.time()
            if self._stop_requested:
                reason, killed = "stopped by user", True
            elif now - start > cfg.job_timeout_s * max(1, len(self.spec.frames)):  # per frame for animations
                reason, killed = f"timeout after {int(now - start)} s", True
            elif now - last_activity > cfg.stall_timeout_s:
                reason, killed = f"no log activity for {int(now - last_activity)} s (phase {progress.phase})", True
            if killed:
                self._emit("killing", progress)
                kill_process_tree(self.proc)
                break
            time.sleep(cfg.poll_interval_s)

        # Late lines written during shutdown.
        time.sleep(0.2)
        late = own_session(tail.read_new_lines() + tail.flush_partial())
        if late:
            self._remember(late)
            for ev in parser.feed_many(late):
                self._emit(ev, progress)

        duration = time.time() - start
        exit_code = self.proc.returncode
        script_result = self._read_result(result_file)
        frames_ok: List[int] = []
        if self.spec.frames:
            # Each frame stands on its own: a crash after frame 3 still keeps frames 0-3.
            fr = (script_result or {}).get("frames", {}) or {}
            for f, path in zip(self.spec.frames, self.spec.frame_targets):
                rec = fr.get(str(f)) or {}
                if rec.get("ok") and self._output_fresh(Path(path), start):
                    frames_ok.append(int(f))
            output_ok = len(frames_ok) == len(self.spec.frames)
        else:
            output_ok = self._output_fresh(target, start)
        warnings: List[str] = []
        if progress.cpu_rendered:
            warnings.append("CPU took part in the render; check the Hardware pane and -copyAppSettings")
        used = (script_result or {}).get("frameUsed")
        if not self.spec.frames and used is not None and int(used) != int(self.spec.frame):
            warnings.append(f"timeline was at frame {used}, not {self.spec.frame}: the render may show the wrong pose")

        if not killed:
            if progress.errors:
                reason = progress.errors[0]
            if script_result is None:
                reason = reason or f"no result file (crash, script abort or suppressed dialog); exit code {exit_code}"
            elif script_result.get("error"):
                reason = f"script: {script_result['error']}"
            elif not script_result.get("ok"):
                reason = reason or f"render returned {script_result.get('renderReturned')!r} at stage {script_result.get('stage')}"
            if not output_ok:
                if self.spec.frames:
                    missing = [f for f in self.spec.frames if f not in frames_ok]
                    reason = reason or f"frames not rendered: {missing[:8]}{'...' if len(missing) > 8 else ''}"
                else:
                    reason = reason or "output file missing or stale"
        ok = (not killed) and script_result is not None and bool(script_result.get("ok")) and output_ok and not progress.errors
        if ok:
            reason = "ok"

        return JobResult(
            ok=ok, reason=reason, exit_code=exit_code, killed=killed, duration_s=duration,
            render_secs=progress.render_secs, iterations=progress.iterations, script_result=script_result,
            progress=progress, warnings=warnings, log_tail=list(self._recent[-40:]), frames_ok=frames_ok,
        )

    # -- helpers ---------------------------------------------------------------
    def _emit(self, event: str, progress: Progress) -> None:
        if self.on_event:
            try:
                self.on_event(event, progress)
            except Exception:  # a UI hiccup must never kill the job
                pass

    def _remember(self, lines: List[str]) -> None:
        self._recent.extend(lines)
        if len(self._recent) > 200:
            del self._recent[:-200]

    @staticmethod
    def _read_result(path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        for _ in range(5):  # the writer may still be closing the file
            try:
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    return json.loads(text)
            except (OSError, json.JSONDecodeError):
                pass
            time.sleep(0.2)
        return None

    @staticmethod
    def _output_fresh(target: Path, start: float) -> bool:
        try:
            st = target.stat()
        except OSError:
            return False
        return st.st_size > 0 and st.st_mtime >= start - 5
