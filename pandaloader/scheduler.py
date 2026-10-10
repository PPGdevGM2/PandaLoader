"""Drive N worker slots over a queue file until nothing is pending.

Each slot runs in its own thread: claim the next pending scene (state 3,
saved), resolve the camera, write the job script, run the worker, write the
outcome back (state, secsRender, log line, saved). A crash therefore loses at
most the render in flight, as with the original plugin.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, Optional

from . import dufinfo
from .config import Config, WorkerSlot
from .jobgen import JobSpec, write_job
from .logparse import Progress
from .queuefile import QueueFile, Scene
from .state import Status
from .worker import JobResult, WorkerRun

log = logging.getLogger("pandaloader.scheduler")


class Scheduler:
    def __init__(self, cfg: Config, queue: QueueFile, status: Status,
                 target_dir: Optional[str] = None,
                 on_queue_finished: Optional[Callable[[], None]] = None):
        self.cfg = cfg
        self.queue = queue
        self.status = status
        self.target_dir = Path(target_dir) if target_dir else None
        self.on_queue_finished = on_queue_finished
        self.qlock = threading.RLock()
        self._threads: Dict[str, threading.Thread] = {}
        self._runs: Dict[str, WorkerRun] = {}
        self._attempts = defaultdict(int)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._finished_once = False
        self.slots = cfg.run_slots()  # enabled workers, or one combined slot
        for slot in self.slots:
            status.add_worker(slot.name, slot.gpu)
        status.shutdown_after_queue = cfg.shutdown_after_queue
        self._publish_queue()

    # -- control ---------------------------------------------------------------
    def start(self) -> None:
        with self.qlock:
            n = self.queue.reset_running()
            if n:
                self.queue.append_log(f"PandaLoader: reset {n} scene(s) left in state 3")
                self.queue.save()
            mode = "one scene at a time" if self.cfg.mode == "combined" else "one scene per worker"
            self.queue.append_log(f"PandaLoader: launched ({mode}) with {len(self.slots)} worker(s): "
                                  + ", ".join(f"{w.name}(gpu {w.gpu_text})" for w in self.slots))
            self.queue.save()
        self.status.running = True
        self.status.started_at = time.time()
        self.status.log(f"queue {self.queue.path}: {self.queue.counts()}")
        for slot in self.slots:
            t = threading.Thread(target=self._slot_loop, args=(slot,), name=f"slot-{slot.name}", daemon=True)
            self._threads[slot.name] = t
            t.start()

    def pause(self) -> None:
        self._pause.set()
        self.status.paused = True
        self.status.log("paused: running jobs finish, no new jobs start")

    def resume(self) -> None:
        self._pause.clear()
        self.status.paused = False
        self.status.log("resumed")

    def stop(self, wait: bool = True, timeout: float = 60.0) -> None:
        """Stop claiming, kill running jobs, put their scenes back to pending."""
        self._stop.set()
        for run in list(self._runs.values()):
            run.request_stop()
        if wait:
            self.join(timeout)
        self.status.running = False

    def join(self, timeout: Optional[float] = None) -> None:
        deadline = None if timeout is None else time.time() + timeout
        for t in list(self._threads.values()):
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            t.join(remaining)

    @property
    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads.values())

    # -- slot loop -------------------------------------------------------------
    def _slot_loop(self, slot: WorkerSlot) -> None:
        try:
            while not self._stop.is_set():
                if self._pause.is_set():
                    time.sleep(0.5)
                    continue
                scene = self._claim(slot)
                if scene is None:
                    break
                self._run_one(slot, scene)
        except Exception as e:  # never let a slot die silently
            log.exception("slot %s crashed", slot.name)
            self.status.log(f"slot crashed: {e!r}", level="error", worker=slot.name)
        finally:
            self.status.worker_idle(slot.name)
            self._maybe_finished()

    def _claim(self, slot: WorkerSlot) -> Optional[Scene]:
        with self.qlock:
            scene = self.queue.next_pending()
            if scene is None:
                return None
            scene.mark_running()
            self._attempts[scene.index] += 1
            self.queue.append_log(f"{slot.name}: starting job {scene.index + 1}/{len(self.queue.scenes)}: {scene.file}")
            self.queue.save()
            self._publish_queue()
            return scene

    def _run_one(self, slot: WorkerSlot, scene: Scene) -> None:
        st = self.status
        attempt = self._attempts[scene.index]
        st.update_worker(slot.name, state="starting", scene_index=scene.index, scene_name=scene.name,
                         target=scene.target, job_started=time.time(), render_started=None, iterations=0.0,
                         expected_secs=st.expected_for(scene.last_secs), last_error="")
        st.log(f"job {scene.index + 1}: {scene.name} (attempt {attempt})", worker=slot.name)
        try:
            spec, job_path = self._prepare(slot, scene)
        except Exception as e:
            self._finish(slot, scene, None, f"prepare failed: {e}")
            return
        st.update_worker(slot.name, camera=spec.camera_label, target=spec.target)

        def on_event(ev: str, p: Progress) -> None:
            self._on_event(slot, ev, p)

        run = WorkerRun(self.cfg, slot, spec, job_path, on_event=on_event)
        self._runs[slot.name] = run
        try:
            result = run.run()
        finally:
            self._runs.pop(slot.name, None)
        self._finish(slot, scene, result, result.reason, spec)

    def _prepare(self, slot: WorkerSlot, scene: Scene):
        camera = self._resolve_camera(scene)
        target = scene.target
        if self.target_dir is not None:
            target = (self.target_dir / Path(scene.target).name).as_posix()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = self.cfg.jobs_dir() / f"{slot.name}_{scene.name}_{stamp}"
        spec = JobSpec(
            scene_file=scene.file, target=target, camera_label=camera,
            result_file=str(base.with_suffix(".result.json")), post_loaders=scene.post_loaders,
            image_size=self.cfg.image_size, settle_wait_s=self.cfg.settle_wait_s, frame=scene.frame,
            scene_index=scene.index, label=scene.name,
        )
        job_path = write_job(spec, base.with_suffix(".dsa"))
        self._prune_jobs()
        return spec, job_path

    def _resolve_camera(self, scene: Scene) -> str:
        if not Path(scene.file).exists():
            raise FileNotFoundError(f"scene file missing: {scene.file}")
        label = None
        if scene.uses_saved_camera:
            try:
                label = dufinfo.current_camera_label(scene.file)
            except Exception as e:
                log.warning("camera lookup failed for %s: %s", scene.file, e)
        label = label or scene.camera_hint
        if not label:
            raise ValueError("no camera: scene has no saved camera and no recorded camera label")
        return label

    def _on_event(self, slot: WorkerSlot, ev: str, p: Progress) -> None:
        st = self.status
        fields = {"iterations": p.iterations, "render_elapsed": p.render_elapsed}
        if ev == "launched":
            fields["pid"] = self._runs[slot.name].proc.pid if slot.name in self._runs and self._runs[slot.name].proc else None
        if ev == "render_start":
            fields["render_started"] = time.time()
            fields["state"] = "rendering"
        elif ev == "iteration" and st.workers[slot.name].render_started is None:
            fields["render_started"] = time.time() - p.render_elapsed
            fields["state"] = "rendering"
        elif ev == "loading":
            fields["state"] = "loading"
        elif ev in ("devstats", "saved"):
            fields["state"] = "saving"
        elif ev == "finished":
            fields["state"] = "finished"
        elif ev == "killing":
            fields["state"] = "killing"
        elif ev == "error":
            fields["last_error"] = p.errors[-1] if p.errors else ""
            st.log(f"log error: {fields['last_error']}", level="error", worker=slot.name)
        st.update_worker(slot.name, **fields)

    def _finish(self, slot: WorkerSlot, scene: Scene, result: Optional[JobResult], reason: str,
                spec: Optional[JobSpec] = None) -> None:
        st = self.status
        ws = st.workers[slot.name]
        attempt = self._attempts[scene.index]
        with self.qlock:
            if result is not None and result.ok:
                secs = result.render_secs or (time.time() - (ws.render_started or ws.job_started or time.time()))
                scene.mark_done(secs, spec.camera_label if spec else (scene.camera_hint or ""), scene.frame)
                self.queue.append_log(f"{slot.name}: done {scene.name} in {int(secs)} s, {int(result.iterations)} iterations -> {spec.target if spec else scene.target}")
                ws.jobs_done += 1
                st.log(f"done {scene.name}: {int(secs)} s render, {int(result.iterations)} it", worker=slot.name)
                for w in result.warnings:
                    st.log(w, level="warning", worker=slot.name)
                    self.queue.append_log(f"{slot.name}: WARNING {w}")
            elif self._stop.is_set() or (result is not None and result.killed and "stopped" in result.reason):
                scene.mark_pending()
                self.queue.append_log(f"{slot.name}: stopped during {scene.name}; back to pending")
                st.log(f"stopped {scene.name}; back to pending", level="warning", worker=slot.name)
            elif attempt < self.cfg.max_attempts:
                scene.mark_pending()
                self.queue.append_log(f"{slot.name}: FAILED {scene.name} (attempt {attempt}): {reason}; will retry")
                ws.jobs_failed += 1
                st.update_worker(slot.name, last_error=reason)
                st.log(f"failed {scene.name}: {reason}; retrying", level="error", worker=slot.name)
            else:
                scene.mark_failed(reason)
                self.queue.append_log(f"{slot.name}: FAILED {scene.name} (attempt {attempt}): {reason}")
                ws.jobs_failed += 1
                st.update_worker(slot.name, last_error=reason)
                st.log(f"failed {scene.name}: {reason}", level="error", worker=slot.name)
            if result is not None and not result.ok and result.log_tail:
                log.warning("%s log tail for %s:\n%s", slot.name, scene.name, "\n".join(result.log_tail[-15:]))
            self.queue.save()
            self._publish_queue()
        st.worker_idle(slot.name)

    # -- housekeeping ----------------------------------------------------------
    def _publish_queue(self) -> None:
        self.status.set_queue(str(self.queue.path or ""), self.queue.counts(), self.queue.average_secs())

    def _prune_jobs(self, keep: int = 60) -> None:
        try:
            files = sorted(self.cfg.jobs_dir().glob("*"), key=lambda p: p.stat().st_mtime)
            for p in files[:-keep]:
                p.unlink()
        except OSError:
            pass

    def _maybe_finished(self) -> None:
        if self.alive or self._finished_once:
            return
        self._finished_once = True
        self.status.running = False
        if self._stop.is_set():
            self.status.log("stopped")
            return
        self.status.finished = True
        c = self.queue.counts()
        with self.qlock:
            self.queue.append_log(f"PandaLoader: queue finished: {c['done']} done, {c['failed']} failed")
            self.queue.save()
        self.status.log(f"queue finished: {c['done']} done, {c['failed']} failed")
        if self.on_queue_finished:
            try:
                self.on_queue_finished()
            except Exception:
                log.exception("on_queue_finished failed")
