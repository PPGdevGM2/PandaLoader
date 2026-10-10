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
from .logparse import LogParser, LogTail, Progress

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

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "reason": self.reason, "exitCode": self.exit_code, "killed": self.killed,
            "durationS": self.duration_s, "renderSecs": self.render_secs, "iterations": self.iterations,
            "scriptResult": self.script_result, "warnings": list(self.warnings), "progress": self.progress.to_dict(),
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
        if self.slot.gpu is None:
            env.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            env["CUDA_VISIBLE_DEVICES"] = str(self.slot.gpu)
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
        while True:
            exited = self.proc.poll() is not None
            lines = tail.read_new_lines()
            if exited:
                lines += tail.flush_partial()
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
            elif now - start > cfg.job_timeout_s:
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
        late = tail.read_new_lines() + tail.flush_partial()
        if late:
            self._remember(late)
            for ev in parser.feed_many(late):
                self._emit(ev, progress)

        duration = time.time() - start
        exit_code = self.proc.returncode
        script_result = self._read_result(result_file)
        output_ok = self._output_fresh(target, start)
        warnings: List[str] = []
        if progress.cpu_rendered:
            warnings.append("CPU took part in the render; check the Hardware pane and -copyAppSettings")

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
                reason = reason or "output file missing or stale"
        ok = (not killed) and script_result is not None and bool(script_result.get("ok")) and output_ok and not progress.errors
        if ok:
            reason = "ok"

        return JobResult(
            ok=ok, reason=reason, exit_code=exit_code, killed=killed, duration_s=duration,
            render_secs=progress.render_secs, iterations=progress.iterations, script_result=script_result,
            progress=progress, warnings=warnings, log_tail=list(self._recent[-40:]),
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
