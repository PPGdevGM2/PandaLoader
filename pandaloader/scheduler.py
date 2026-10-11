"""Drive N worker slots over a queue file until nothing is pending.

Each slot runs in its own thread: claim the next pending scene (state 3,
saved), resolve the camera, write the job script, run the worker, write the
outcome back (state, secsRender, log line, saved). A crash therefore loses at
most the render in flight, as with the original plugin.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import dufinfo
from .config import Config, WorkerSlot
from .jobgen import JobSpec, write_job
from .logparse import Progress
from .queuefile import QueueFile, Scene, STATE_PENDING, STATE_RUNNING
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
        self._frame_fails = defaultdict(int)          # (scene index, frame) -> failed attempts
        self._anim_running: Dict[int, Dict[str, List[int]]] = defaultdict(dict)  # scene -> slot -> frames
        self._anim_denoise_fail: set = set()          # scenes with a failed frame denoise
        self._denoise_claimed: set = set()
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
                self._save()
            mode = "one scene at a time" if self.cfg.mode == "combined" else "one scene per worker"
            self.queue.append_log(f"PandaLoader: launched ({mode}) with {len(self.slots)} worker(s): "
                                  + ", ".join(f"{w.name}(gpu {w.gpu_text})" for w in self.slots))
            self._save()
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
        failures = 0
        try:
            while not self._stop.is_set():
                if self._pause.is_set():
                    time.sleep(0.5)
                    continue
                scene = None
                block = None
                try:
                    scene, block = self._claim(slot)
                    if scene is None:
                        dn = self._claim_denoise(slot)
                        if dn is None:
                            break
                        self._run_denoise_only(slot, dn)
                        continue
                    self._run_one(slot, scene, block)
                    failures = 0
                except Exception as e:  # one bad job must not take the worker down
                    failures += 1
                    log.exception("slot %s: job failed unexpectedly", slot.name)
                    self.status.log(f"job error: {e!r}", level="error", worker=slot.name)
                    if scene is not None:
                        if block:
                            self._anim_running[scene.index].pop(slot.name, None)
                        self._mark_crashed(slot, scene, f"internal error: {e!r}")
                    self.status.worker_idle(slot.name)
                    if failures >= 3:  # something systematic: stop this worker rather than loop
                        self.status.log("three consecutive errors; worker stops", level="error", worker=slot.name)
                        break
                    time.sleep(2.0)
        finally:
            self.status.worker_idle(slot.name)
            self._maybe_finished()

    def _mark_crashed(self, slot: WorkerSlot, scene: Scene, reason: str) -> None:
        with self.qlock:
            if scene.state == STATE_RUNNING:
                scene.mark_failed(reason)
                self.queue.append_log(f"{slot.name}: FAILED {scene.name}: {reason}")
            self._save("after job error")

    def _save(self, what: str = "") -> bool:
        """Save the queue; a persistent lock (Dropbox, antivirus) is logged, not fatal.
        The in-memory state is written by the next successful save."""
        try:
            self.queue.save()
            self._publish_queue()
            return True
        except (OSError, ValueError) as e:
            log.error("queue save failed %s: %s", what, e)
            self.status.log(f"queue save failed{' ' + what if what else ''}: {e}", level="error")
            return False

    def _claim(self, slot: WorkerSlot) -> Tuple[Optional[Scene], Optional[List[int]]]:
        """Next unit of work in queue order: a pending still, or frames of an animation.
        By default one worker takes every frame still missing and renders them in a single
        DAZ process; with cfg.anim_split the missing frames are divided among the free
        workers instead, each rendering its share in one process."""
        with self.qlock:
            for scene in self.queue.scenes:
                if scene.anim:
                    if scene.state not in (STATE_PENDING, STATE_RUNNING):
                        continue
                    busy = {f for frames in self._anim_running[scene.index].values() for f in frames}
                    remaining = [f for f in scene.frames_remaining() if f not in busy]
                    if not remaining:
                        continue
                    if self.cfg.anim_split:
                        # Divide what is left among the workers that are free right now, so each
                        # worker launches DAZ once per animation and renders its share in one process.
                        busy_slots = set(self._runs) | {s for blocks in self._anim_running.values() for s in blocks}
                        free = max(1, len(self.slots) - len(busy_slots))
                        size = max(1, math.ceil(len(remaining) / free))
                        block = remaining[:size]
                    else:
                        # One worker renders the whole animation, as a manual render would.
                        if self._anim_running[scene.index]:
                            continue
                        block = remaining
                    self._anim_running[scene.index][slot.name] = block
                    scene.mark_running()
                    self._attempts[scene.index] += 1
                    self.queue.append_log(f"{slot.name}: starting job {scene.index + 1}/{len(self.queue.scenes)}: "
                                          f"{scene.file} frames {block[0]}-{block[-1]} ({len(block)} of {len(scene.frames_remaining())} left)")
                    self._save()
                    return scene, block
                if scene.is_pending:
                    scene.mark_running()
                    self._attempts[scene.index] += 1
                    self.queue.append_log(f"{slot.name}: starting job {scene.index + 1}/{len(self.queue.scenes)}: {scene.file}")
                    self._save()
                    return scene, None
            return None, None

    # -- denoise-only jobs: done scenes whose flag was set after the render ----------
    def _claim_denoise(self, slot: WorkerSlot) -> Optional[Scene]:
        """Done scenes with post-render work still waiting: a denoise flag set after the
        render, or "Action when done" entries added later."""
        with self.qlock:
            scene = self.queue.next_post(exclude=self._denoise_claimed)
            if scene is None:
                return None
            self._denoise_claimed.add(scene.index)
            return scene

    def _run_denoise_only(self, slot: WorkerSlot, scene: Scene) -> None:
        st = self.status
        st.update_worker(slot.name, state="denoising", scene_index=scene.index, scene_name=scene.name,
                         target=scene.target, job_started=time.time(), render_started=None, iterations=0.0,
                         expected_secs=0.0, last_error="")
        if scene.needs_denoise:
            ok, msg = self._denoise_files(slot, scene, scene.output_files())
            with self.qlock:
                scene.mark_denoised(ok)
                self.queue.append_log(f"{slot.name}: {'denoised' if ok else 'DENOISE FAILED'} {scene.name} (existing render): {msg}")
                st.log(f"{scene.name}: {msg}", level="info" if ok else "warning", worker=slot.name)
        with self.qlock:
            self._run_actions(slot, scene)
            self._save("after post-render work")
        st.worker_idle(slot.name)

    def _run_actions(self, slot: WorkerSlot, scene: Scene) -> None:
        """Run the scene's pending "Action when done" entries, in the order selected.
        Called under the queue lock once the scene is done (and denoised)."""
        from .actions import run_action
        stock = {a["id"]: a for a in self.cfg.actions}
        for aid in scene.pending_actions():
            action = stock.get(aid)
            if action is None:
                ok, msg = False, "action no longer exists in Settings > Actions"
                label = aid
            else:
                label = action.get("label") or aid
                ok, msg = run_action(action, scene)
            scene.mark_action(aid, ok)
            self.queue.append_log(f"{slot.name}: {'action' if ok else 'ACTION FAILED'} '{label}' for {scene.name}: {msg}")
            self.status.log(f"{scene.name}: {label}: {msg}", level="info" if ok else "warning", worker=slot.name)

    def _run_one(self, slot: WorkerSlot, scene: Scene, block: Optional[List[int]] = None) -> None:
        st = self.status
        attempt = self._attempts[scene.index]
        n = len(block) if block else 0
        per_frame = st.expected_for(scene.last_secs)
        st.update_worker(slot.name, state="starting", scene_index=scene.index, scene_name=scene.name,
                         target=scene.target, job_started=time.time(), render_started=None, iterations=0.0,
                         expected_secs=per_frame * max(1, n), last_error="",
                         frames=n, frames_done=0, frame=block[0] if block else None, frame_first=block[0] if block else None)
        what = f"frames {block[0]}-{block[-1]}" if block else f"attempt {attempt}"
        st.log(f"job {scene.index + 1}: {scene.name} ({what})", worker=slot.name)
        try:
            spec, job_path = self._prepare(slot, scene, block)
        except FileNotFoundError as e:
            self._attempts[scene.index] = self.cfg.max_attempts  # retrying cannot bring the file back
            self._finish(slot, scene, None, str(e), block=block)
            return
        except Exception as e:
            self._finish(slot, scene, None, f"prepare failed: {e}", block=block)
            return
        st.update_worker(slot.name, camera=spec.camera_label, target=spec.frame_targets[0] if block else spec.target)

        def on_event(ev: str, p: Progress) -> None:
            self._on_event(slot, ev, p, block)

        run = WorkerRun(self.cfg, slot, spec, job_path, on_event=on_event)
        self._runs[slot.name] = run
        try:
            result = run.run()
        finally:
            self._runs.pop(slot.name, None)
        self._finish(slot, scene, result, result.reason, spec, block)

    def _prepare(self, slot: WorkerSlot, scene: Scene, block: Optional[List[int]] = None):
        camera = self._resolve_camera(scene)
        target = scene.target
        if self.target_dir is not None:
            target = (self.target_dir / Path(scene.target).name).as_posix()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = self.cfg.jobs_dir() / f"{slot.name}_{scene.name}_{stamp}"
        frames = list(block) if block else []
        frame_targets = []
        if frames:
            tdir = Path(target).parent
            frame_targets = [(tdir / Path(scene.frame_target(f)).name).as_posix() for f in frames]
        spec = JobSpec(
            scene_file=scene.file, target=target, camera_label=camera,
            result_file=str(base.with_suffix(".result.json")), post_loaders=scene.post_loaders,
            image_size=scene.image_size or self.cfg.image_size, settle_wait_s=self.cfg.settle_wait_s, frame=scene.frame,
            scene_index=scene.index, label=scene.name, frames=frames, frame_targets=frame_targets,
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

    def _on_event(self, slot: WorkerSlot, ev: str, p: Progress, block: Optional[List[int]] = None) -> None:
        st = self.status
        fields = {"iterations": p.iterations, "render_elapsed": p.render_elapsed}
        if block:
            fields["frames_done"] = min(p.frames_finished, len(block))
            if p.frames_started:
                fields["frame"] = block[min(p.frames_started, len(block)) - 1]
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
            # an animation block goes on with the next frame; only the last one is "finished"
            fields["state"] = "finished" if not block or p.frames_finished >= len(block) else "rendering"
        elif ev == "killing":
            fields["state"] = "killing"
        elif ev == "error":
            fields["last_error"] = p.errors[-1] if p.errors else ""
            st.log(f"log error: {fields['last_error']}", level="error", worker=slot.name)
        st.update_worker(slot.name, **fields)

    def _finish(self, slot: WorkerSlot, scene: Scene, result: Optional[JobResult], reason: str,
                spec: Optional[JobSpec] = None, block: Optional[List[int]] = None) -> None:
        if block:
            self._finish_block(slot, scene, result, reason, spec, block)
            return
        st = self.status
        ws = st.workers[slot.name]
        attempt = self._attempts[scene.index]
        denoise_note = None
        if result is not None and result.ok and scene.denoise and spec is not None:
            denoise_note = self._denoise(slot, scene, spec.target)
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
                if denoise_note is not None:
                    ok, msg = denoise_note
                    scene.mark_denoised(ok)
                    self.queue.append_log(f"{slot.name}: {'denoised' if ok else 'DENOISE FAILED'} {scene.name}: {msg}")
                    st.log(f"{scene.name}: {msg}", level="info" if ok else "warning", worker=slot.name)
                self._run_actions(slot, scene)
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
            self._save()
        st.worker_idle(slot.name)

    def _finish_block(self, slot: WorkerSlot, scene: Scene, result: Optional[JobResult], reason: str,
                      spec: Optional[JobSpec], block: List[int]) -> None:
        """Book the frames of an animation block. Finished frames are kept whatever
        happened to the rest; unfinished ones go back to the pool and count a failure."""
        st = self.status
        ws = st.workers[slot.name]
        frames_ok = list(result.frames_ok) if result is not None else []
        failed = [f for f in block if f not in frames_ok]
        denoise_note = None
        if frames_ok and scene.denoise and spec is not None:
            files = [t for f, t in zip(spec.frames, spec.frame_targets) if f in frames_ok]
            denoise_note = self._denoise_files(slot, scene, files)
        with self.qlock:
            self._anim_running[scene.index].pop(slot.name, None)
            others_running = bool(self._anim_running[scene.index])
            if frames_ok:
                secs = result.progress.frame_secs if result is not None else []
                avg = sum(secs) / len(secs) if secs else None
                scene.mark_frames_done(frames_ok, avg, spec.camera_label if spec else (scene.camera_hint or ""))
                self.queue.append_log(f"{slot.name}: frames {frames_ok[0]}-{frames_ok[-1]} of {scene.name} done"
                                      + (f", {int(avg)} s/frame" if avg else "") + f"; {len(scene.frames_done)}/{len(scene.frames)} total")
                st.log(f"{scene.name}: frames {frames_ok[0]}-{frames_ok[-1]} done ({len(scene.frames_done)}/{len(scene.frames)})", worker=slot.name)
                if denoise_note is not None:
                    ok, msg = denoise_note
                    if not ok:
                        self._anim_denoise_fail.add(scene.index)
                    self.queue.append_log(f"{slot.name}: {'denoised' if ok else 'DENOISE FAILED'} {scene.name} frames: {msg}")
                    st.log(f"{scene.name}: {msg}", level="info" if ok else "warning", worker=slot.name)
                if result is not None:
                    for w in result.warnings:
                        st.log(w, level="warning", worker=slot.name)
                        self.queue.append_log(f"{slot.name}: WARNING {w}")
            if scene.is_done:
                ws.jobs_done += 1
                if scene.denoise:
                    scene.mark_denoised(scene.index not in self._anim_denoise_fail)
                self.queue.append_log(f"{slot.name}: done {scene.name}: all {len(scene.frames)} frames -> {Path(scene.target).parent}")
                st.log(f"done {scene.name}: all {len(scene.frames)} frames", worker=slot.name)
                self._run_actions(slot, scene)
            elif failed:
                stopped = self._stop.is_set() or (result is not None and result.killed and "stopped" in result.reason)
                if stopped:
                    self.queue.append_log(f"{slot.name}: stopped during {scene.name} frames {failed[0]}-{failed[-1]}; back to pending")
                    st.log(f"stopped {scene.name}; frames {failed[0]}-{failed[-1]} back to pending", level="warning", worker=slot.name)
                    if not others_running:
                        scene.mark_pending()
                else:
                    for f in failed:
                        self._frame_fails[(scene.index, f)] += 1
                    worst = max(self._frame_fails[(scene.index, f)] for f in failed)
                    ws.jobs_failed += 1
                    st.update_worker(slot.name, last_error=reason)
                    if worst >= self.cfg.max_attempts:
                        scene.mark_failed(reason)
                        self.queue.append_log(f"{slot.name}: FAILED {scene.name} frames {failed[0]}-{failed[-1]} (attempt {worst}): {reason}")
                        st.log(f"failed {scene.name} frames {failed[0]}-{failed[-1]}: {reason}", level="error", worker=slot.name)
                    else:
                        if not others_running:
                            scene.mark_pending()
                        self.queue.append_log(f"{slot.name}: FAILED {scene.name} frames {failed[0]}-{failed[-1]} (attempt {worst}): {reason}; will retry")
                        st.log(f"failed {scene.name} frames {failed[0]}-{failed[-1]}: {reason}; retrying", level="error", worker=slot.name)
            elif not others_running and not scene.is_done:
                scene.mark_pending()  # nothing failed, frames remain: let the next claim take them
            if result is not None and not result.ok and result.log_tail:
                log.warning("%s log tail for %s:\n%s", slot.name, scene.name, "\n".join(result.log_tail[-15:]))
            self._save()
        st.worker_idle(slot.name)

    def _denoise_files(self, slot: WorkerSlot, scene: Scene, files: List[str]):
        """Denoise several outputs (animation frames). Returns (all ok, message)."""
        oks, msgs = [], []
        for f in files:
            ok, msg = self._denoise(slot, scene, f)
            oks.append(ok)
            if not ok:
                msgs.append(f"{Path(f).name}: {msg}")
        n_ok = sum(1 for o in oks if o)
        if all(oks):
            return True, f"{n_ok} file(s) denoised"
        return False, f"{n_ok}/{len(files)} denoised; " + "; ".join(msgs[:3])

    def _denoise(self, slot: WorkerSlot, scene: Scene, target: str):
        """Run the configured denoiser over the finished render. Returns (ok, message)."""
        from .denoise import denoise_in_place, resolve_denoiser
        exe = resolve_denoiser(self.cfg.denoiser_exe)
        if exe is None:
            return False, "denoise skipped: no valid denoiser path in Settings"
        if not Path(target).is_file():
            return False, f"denoise skipped: render not found at {target}"
        self.status.update_worker(slot.name, state="denoising")
        self.status.log(f"denoising {scene.name}", worker=slot.name)
        ok, msg, _secs = denoise_in_place(exe, target, timeout_s=self.cfg.denoise_timeout_s,
                                          keep_original=self.cfg.denoise_keep_original,
                                          scratch_dir=str(Path(self.cfg.work_dir) / "denoise"))
        return ok, msg

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
        # Called from a slot thread's finally block: that thread is still alive, so it must
        # not count. Otherwise the last slot never sees the run as finished.
        me = threading.current_thread()
        others_alive = any(t.is_alive() and t is not me for t in self._threads.values())
        if others_alive or self._finished_once:
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
            self._save()
        self.status.log(f"queue finished: {c['done']} done, {c['failed']} failed")
        if self.on_queue_finished:
            try:
                self.on_queue_finished()
            except Exception:
                log.exception("on_queue_finished failed")
