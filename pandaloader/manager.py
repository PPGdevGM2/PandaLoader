"""Long-lived application object behind the web UI and the tray icon.

Owns the config, the open queue, the status model and (while rendering) the
scheduler. Every mutation goes through one lock so that the HTTP handlers,
the scheduler threads and the tray never race on the queue file.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__, dufinfo, gpumon
from .config import MODES, MODE_COMBINED, Config, WorkerSlot, _parse_gpu
from .queuefile import QueueFile, Scene, STATE_DONE, STATE_RUNNING
from .scheduler import Scheduler
from .state import Status

log = logging.getLogger("pandaloader.manager")

SCENE_EXTS = {".duf"}
QUEUE_SUFFIX = ".rq.json"


class ManagerError(Exception):
    """A user-facing error: wrong state or bad input."""


class Manager:
    def __init__(self, cfg: Config, config_path: Optional[Path] = None):
        self.cfg = cfg
        self.config_path = Path(config_path) if config_path else Path(cfg.work_dir) / "config.json"
        self.lock = threading.RLock()
        self.queue: Optional[QueueFile] = None
        self.status = Status(cfg.startup_overhead_s, cfg.default_render_secs)
        self.scheduler: Optional[Scheduler] = None
        self._state_file = Path(cfg.work_dir) / "manager-state.json"
        self.recent: List[str] = []
        self.browse_dirs: dict = {}  # last folder confirmed in each file picker, by picker key
        self._mtime_cache: dict = {}
        self.on_port_change: Optional[Callable[[int], None]] = None  # set by the host (tray/serve) to rebind
        self.on_quit: Optional[Callable[[], None]] = None  # set by the tray: a clean exit, like its Quit item
        self.shutdown_at: Optional[float] = None  # epoch seconds of a pending PC shutdown
        self._denoiser_install: Optional[dict] = None  # {"phase", "fraction", "error", "done"} while/after installing
        self.first_run = False  # set by the host when no config.json existed at start
        self._load_state()
        for slot in cfg.run_slots():
            self.status.add_worker(slot.name, slot.gpu)
        self.status.shutdown_after_queue = cfg.shutdown_after_queue
        self.status.log(f"PandaLoader {__version__} started")

    # -- persistence of the manager's own little state ------------------------
    def _load_state(self) -> None:
        self._last_queue_pending: Optional[str] = None  # last queue not reachable yet (Dropbox/drive not up)
        try:
            d = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.info("no manager state restored: %s", e)
            return
        self.recent = [p for p in d.get("recent", []) if Path(p).exists()]
        dirs = d.get("browseDirs")
        if isinstance(dirs, dict):
            self.browse_dirs = {str(k): str(v) for k, v in dirs.items() if v}
        last = d.get("lastQueue")
        if last and not self._restore_queue(last):
            self._last_queue_pending = last
            self.status.log(f"last queue not reachable yet, waiting for it: {last}", level="warning")

    def _restore_queue(self, path: str) -> bool:
        """Reopen the queue used last time. False if the file is not there (yet) or unreadable."""
        try:
            if not Path(path).is_file():
                return False
            self.queue = QueueFile.load(path)
            self.status.set_queue(Path(path).as_posix(), self.queue.counts(), self.queue.average_secs())
            self.status.log(f"reopened {Path(path).name}: {self.queue.counts()}")
            log.info("reopened last queue %s", path)
            self._warn_moved_files()
            return True
        except (OSError, ValueError) as e:
            log.warning("could not reopen last queue %s: %s", path, e)
            self.status.log(f"could not reopen {path}: {e}", level="error")
            return False

    def _retry_last_queue(self) -> None:
        """Called on every snapshot: a queue on a drive that was not mounted at start (Dropbox,
        network share) is picked up as soon as it appears, until the user opens something else."""
        if self._last_queue_pending and self.queue is None and Path(self._last_queue_pending).is_file():
            path, self._last_queue_pending = self._last_queue_pending, None
            self._restore_queue(path)

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({
                "lastQueue": str(self.queue.path) if self.queue and self.queue.path else None,
                "recent": self.recent[:12],
                "browseDirs": self.browse_dirs,
            }, indent=2), encoding="utf-8")
        except OSError:
            log.exception("could not save manager state")

    def save_config(self) -> None:
        self.cfg.save(self.config_path)

    # -- queue file ------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.scheduler is not None and self.scheduler.alive

    def _require_idle(self, what: str) -> None:
        if self.running:
            raise ManagerError(f"cannot {what} while rendering; stop the run first")

    def _require_queue(self) -> QueueFile:
        if self.queue is None:
            raise ManagerError("no queue open")
        return self.queue

    def _remember(self, path: str) -> None:
        path = Path(path).as_posix()
        self.recent = [path] + [p for p in self.recent if p != path]
        self._save_state()

    def open_queue(self, path: str) -> None:
        with self.lock:
            self._require_idle("open another queue")
            p = Path(path)
            if not p.exists():
                raise ManagerError(f"queue file not found: {path}")
            self.queue = QueueFile.load(p)
            self.status = self._fresh_status()
            self._publish()
            self._remember(str(p))
            self.status.log(f"opened {p.name}: {self.queue.counts()}")
            self._warn_moved_files()

    def _warn_moved_files(self) -> None:
        """Scenes still to render whose .duf is no longer at the recorded path. Done scenes
        with a moved file are normal (the user files them away) and are not mentioned."""
        gone = [s.name for s in self.queue.scenes if not s.is_done and s.file and not Path(s.file).is_file()]
        if gone:
            self.status.log(f"{len(gone)} scene(s) to render whose .duf is not at the recorded path: "
                            + ", ".join(gone[:5]) + ("…" if len(gone) > 5 else ""), level="warning")

    def new_queue(self, path: str) -> None:
        with self.lock:
            self._require_idle("create a queue")
            p = Path(path)
            if p.suffix != ".json":
                p = p.with_name(p.name + QUEUE_SUFFIX)
            if p.exists():
                raise ManagerError(f"file exists: {p}")
            self.queue = QueueFile.empty(p)
            self.queue.append_log("PandaLoader: queue created")
            self.queue.save()
            self.status = self._fresh_status()
            self._publish()
            self._remember(str(p))
            self.status.log(f"created {p.name}")

    def save_queue(self, path: Optional[str] = None) -> str:
        with self.lock:
            q = self._require_queue()
            if path:
                self._require_idle("save the queue under a new name")
            target = q.save(path)
            self._remember(str(target))
            return str(target)

    def _fresh_status(self) -> Status:
        st = Status(self.cfg.startup_overhead_s, self.cfg.default_render_secs)
        st.listeners = list(self.status.listeners)
        for ev in list(self.status.events)[-50:]:
            st.events.append(ev)
        for slot in self.cfg.run_slots():
            st.add_worker(slot.name, slot.gpu)
        st.shutdown_after_queue = self.cfg.shutdown_after_queue
        return st

    def _publish(self) -> None:
        if self.queue is not None:
            path = self.queue.path.as_posix() if self.queue.path else ""
            self.status.set_queue(path, self.queue.counts(), self.queue.average_secs())

    # -- run control -------------------------------------------------------------
    def start(self) -> None:
        with self.lock:
            q = self._require_queue()
            if self.running:
                raise ManagerError("already rendering")
            if q.path is None:
                raise ManagerError("save the queue first")
            if not Path(self.cfg.daz_exe).exists():
                raise ManagerError(f"DAZ executable not found: {self.cfg.daz_exe}")
            c = q.counts()
            if c["pending"] == 0 and c["running"] == 0 and c["toDenoise"] == 0:
                raise ManagerError("nothing pending and nothing to denoise; reset scenes first")
            q = self._require_queue()
            only_denoise = c["pending"] == 0 and c["running"] == 0 and not any(s.pending_actions() for s in q.scenes)
            if only_denoise and not _denoiser_valid(self.cfg.denoiser_exe):
                raise ManagerError("only denoise jobs are waiting, but no valid denoiser path is set in Settings")
            if not self.cfg.run_slots():
                raise ManagerError("no worker is enabled; enable one in Settings")
            self.status = self._fresh_status()
            self.scheduler = Scheduler(self.cfg, q, self.status, on_queue_finished=self._on_finished)
            self.scheduler.start()

    def pause(self) -> None:
        with self.lock:
            if not self.running:
                raise ManagerError("not rendering")
            self.scheduler.pause()

    def resume(self) -> None:
        with self.lock:
            if not self.running:
                raise ManagerError("not rendering")
            self.scheduler.resume()

    def stop(self) -> None:
        with self.lock:
            if not self.running:
                raise ManagerError("not rendering")
            sched = self.scheduler
        threading.Thread(target=sched.stop, kwargs={"wait": True}, name="stopper", daemon=True).start()

    def emergency_stop(self) -> None:
        """Fast path for Windows shutdown: scenes in flight back to pending, queue
        saved, worker processes killed. Must finish in a couple of seconds."""
        sched = self.scheduler
        if sched is not None and sched.alive:
            sched._stop.set()
            with sched.qlock:
                n = self.queue.reset_running() if self.queue else 0
                if self.queue is not None and self.queue.path is not None:
                    self.queue.append_log(f"PandaLoader: Windows shutdown; {n} scene(s) back to pending")
                    self.queue.save()
            for run in list(sched._runs.values()):
                proc = run.proc
                if proc is not None and proc.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False, timeout=3)
        self.status.log("stopped for Windows shutdown", level="warning")

    SHUTDOWN_DELAY_S = 60

    def _on_finished(self) -> None:
        if self.cfg.shutdown_after_queue:
            self.status.log(f"queue finished: shutting down the PC in {self.SHUTDOWN_DELAY_S} s (abort on the page or in the tray)", level="warning")
            r = subprocess.run(["shutdown", "/s", "/t", str(self.SHUTDOWN_DELAY_S), "/c", "PandaLoader: render queue finished"],
                               capture_output=True, text=True, check=False)
            if r.returncode == 0:
                self.shutdown_at = time.time() + self.SHUTDOWN_DELAY_S
            else:
                self.status.log(f"shutdown command failed: {(r.stderr or r.stdout).strip()}", level="error")

    def abort_shutdown(self) -> None:
        """Cancel a pending shutdown (shutdown /a). Safe to call when none is pending."""
        with self.lock:
            r = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, check=False)
            was_pending = self.shutdown_at is not None
            self.shutdown_at = None
            if r.returncode == 0 or was_pending:
                self.status.log("PC shutdown aborted", level="warning")
            else:
                raise ManagerError(f"no shutdown to abort ({(r.stderr or r.stdout).strip() or 'shutdown /a failed'})")

    @property
    def shutdown_pending(self) -> bool:
        return self.shutdown_at is not None and time.time() < self.shutdown_at + 5

    # -- scene editing -------------------------------------------------------------
    def _scene(self, index: int) -> Scene:
        q = self._require_queue()
        if not 0 <= index < len(q.data["scenes"]):
            raise ManagerError(f"no scene {index}")
        return q.scene(index)

    def _commit(self, message: Optional[str] = None) -> None:
        q = self._require_queue()
        if message:
            q.append_log(f"PandaLoader: {message}")
        if q.path is not None:
            q.save()
        self._publish()

    def _qlock(self):
        return self.scheduler.qlock if self.scheduler is not None else self.lock

    def add_scenes(self, files: List[str], target_dir: Optional[str] = None,
                   post_loaders: Optional[List[str]] = None, at_top: bool = False, denoise: bool = False,
                   anim: Optional[dict] = None, image_size: Optional[list] = None,
                   actions: Optional[list] = None) -> int:
        """anim: {"frameFirst": a, "frameLast": b} renders the (single) scene as an animation.
        image_size: [w, h] overrides the preset's render size for every added scene.
        actions: ids from the action stock to run when each scene is done."""
        size = _parse_size(image_size)
        action_ids = self._check_action_ids(actions)
        anim_range = None
        if anim and len(files) == 1:
            a, b = int(anim.get("frameFirst", 0)), int(anim.get("frameLast", 0))
            if b < a:
                raise ManagerError("last frame must not be before the first frame")
            anim_range = (a, b)
        with self.lock, self._qlock():
            q = self._require_queue()
            if post_loaders is None:
                post_loaders = []  # pre-render assets are per scene, never inherited from earlier entries
            tdir = Path(target_dir) if target_dir else self._default_target_dir()
            added = 0
            new_entries = []
            for f in files:
                p = Path(f)
                if p.suffix.lower() not in SCENE_EXTS or not p.exists():
                    continue
                if any(Path(s.file) == p for s in q.scenes):
                    continue
                target = (tdir / (p.stem + ".png")).as_posix() if tdir else ""
                if anim_range:
                    sc = q.add_scene(p.as_posix(), target, post_loaders, denoise=denoise, anim=True,
                                     frame_first=anim_range[0], frame_last=anim_range[1])
                else:
                    sc = q.add_scene(p.as_posix(), target, post_loaders, denoise=denoise)
                if size:
                    sc.set_image_size(size)
                if action_ids:
                    sc.set_actions(action_ids)
                new_entries.append(sc.data)
                added += 1
            if at_top and new_entries:
                for e in new_entries:
                    q.data["scenes"].remove(e)
                q.data["scenes"][0:0] = new_entries
            if added:
                self._commit(f"added {added} scene(s)")
            return added

    def _default_post_loaders(self) -> List[str]:
        q = self._require_queue()
        for s in reversed(q.scenes):
            if s.post_loaders:
                return list(s.post_loaders)
        return []

    def _default_target_dir(self) -> Optional[Path]:
        q = self._require_queue()
        for s in reversed(q.scenes):
            if s.target:
                return Path(s.target).parent
        return None

    def remove_scene(self, index: int) -> None:
        with self.lock, self._qlock():
            sc = self._scene(index)
            if sc.state == STATE_RUNNING:
                raise ManagerError("scene is rendering")
            del self._require_queue().data["scenes"][index]
            self._commit(f"removed {sc.name}")

    def move_scene(self, index: int, delta: int) -> int:
        with self.lock, self._qlock():
            q = self._require_queue()
            scenes = q.data["scenes"]
            self._scene(index)
            new = max(0, min(len(scenes) - 1, index + delta))
            if new != index:
                entry = scenes.pop(index)
                scenes.insert(new, entry)
                self._commit()
            return new

    def reorder(self, indices: List[int], to: int) -> List[int]:
        """Move the given scenes (keeping their relative order) so that the first lands
        at list position ``to`` (a position in the list before removal). Returns the
        new indices of the moved scenes."""
        with self.lock, self._qlock():
            q = self._require_queue()
            scenes = q.data["scenes"]
            moving = sorted({int(i) for i in indices if 0 <= int(i) < len(scenes)})
            if not moving:
                return []
            to = max(0, min(int(to), len(scenes)))
            entries = [scenes[i] for i in moving]
            insert_at = to - sum(1 for i in moving if i < to)
            for i in reversed(moving):
                del scenes[i]
            scenes[insert_at:insert_at] = entries
            self._commit()
            return list(range(insert_at, insert_at + len(entries)))

    def reset_scene(self, index: int) -> None:
        with self.lock, self._qlock():
            sc = self._scene(index)
            if sc.state == STATE_RUNNING:
                raise ManagerError("scene is rendering")
            sc.mark_pending(keep_frames=False)
            sc.data.pop("lastError", None)
            self._commit(f"reset {sc.name} to pending")

    def mark_done(self, index: int) -> None:
        with self.lock, self._qlock():
            sc = self._scene(index)
            if sc.state == STATE_RUNNING:
                raise ManagerError("scene is rendering")
            sc.state = STATE_DONE
            self._commit(f"marked {sc.name} as done (skipped)")

    def reset_all(self, failed_only: bool = False) -> int:
        with self.lock, self._qlock():
            n = self._require_queue().reset(failed_only=failed_only)
            self._commit(f"reset {n} scene(s)" + (" (failed only)" if failed_only else ""))
            return n

    def batch(self, indices: List[int], action: str, on: bool = True) -> dict:
        """Apply one action to several scenes: denoise, reset, done, remove.
        Running scenes are skipped (except for the denoise flag). Returns counts."""
        with self.lock, self._qlock():
            q = self._require_queue()
            idx = sorted({int(i) for i in indices if 0 <= int(i) < len(q.data["scenes"])}, reverse=True)
            done = skipped = 0
            for i in idx:
                sc = q.scene(i)
                if action == "denoise":
                    sc.denoise = bool(on)
                    done += 1
                elif sc.state == STATE_RUNNING:
                    skipped += 1
                elif action == "reset":
                    sc.mark_pending()
                    sc.data.pop("lastError", None)
                    done += 1
                elif action == "done":
                    sc.state = STATE_DONE
                    done += 1
                elif action == "remove":
                    del q.data["scenes"][i]  # descending order keeps the other indices valid
                    done += 1
                else:
                    raise ManagerError(f"unknown batch action {action!r}")
            if done:
                label = {"denoise": f"denoise {'on' if on else 'off'} for", "reset": "reset", "done": "marked done", "remove": "removed"}[action]
                self._commit(f"{label} {done} scene(s)" if action != "denoise" else None)
            return {"done": done, "skipped": skipped}

    def set_denoise(self, index: int, on: bool) -> None:
        """Quick toggle of the post-render denoise flag; allowed while rendering (read at finish)."""
        with self.lock, self._qlock():
            sc = self._scene(index)
            sc.denoise = bool(on)
            self._commit()

    def edit_scene(self, index: int, target: Optional[str] = None, post_loaders: Optional[List[str]] = None,
                   camera: Optional[str] = None, denoise: Optional[bool] = None, anim: Optional[bool] = None,
                   frame_first: Optional[int] = None, frame_last: Optional[int] = None,
                   image_size: Optional[list] = "keep", actions: Optional[list] = None) -> None:
        """camera: '' or None keeps 'camera saved with scene'; a label forces that camera.
        image_size: [w, h] sets the per-scene render size, [] or None clears it, "keep" leaves it.
        actions: list of action ids (None leaves them)."""
        with self.lock, self._qlock():
            sc = self._scene(index)
            if sc.state == STATE_RUNNING:
                raise ManagerError("scene is rendering")
            if denoise is not None:
                sc.denoise = bool(denoise)
            if actions is not None:
                sc.set_actions(self._check_action_ids(actions))
            if image_size != "keep":
                sc.set_image_size(_parse_size(image_size))
            if anim is not None or frame_first is not None or frame_last is not None:
                on = sc.anim if anim is None else bool(anim)
                a = sc.frame_first if frame_first is None else int(frame_first)
                b = sc.frame_last if frame_last is None else int(frame_last)
                if on and b < a:
                    raise ManagerError("last frame must not be before the first frame")
                if on != sc.anim or a != sc.frame_first or b != sc.frame_last:
                    if sc.is_done and on:
                        sc.mark_pending(keep_frames=True)  # new/changed range: render what is missing
                    sc.set_anim(on, a, b)
            if target is not None:
                sc.target = Path(target).as_posix() if target else ""
            if post_loaders is not None:
                sc.data["postLoaders"] = [{"asset": Path(p).as_posix(), "node": ""} for p in post_loaders if p]
            if camera is not None:
                if camera:
                    sc.data["cam"] = 0
                    sc.data["singleCamName"] = camera
                else:
                    sc.data["cam"] = 1
                    sc.data["singleCamName"] = 0
            self._commit(f"edited {sc.name}")

    # -- denoiser install ------------------------------------------------------
    def install_denoiser(self) -> None:
        """Download the latest prebuilt Denoiser OI release into <work dir>/denoiser in the
        background and point the denoiser setting at the unpacked exe. Progress is in snapshot()."""
        from .denoise import REPO_URL, install_denoiser
        with self.lock:
            st = self._denoiser_install
            if st is not None and not st.get("done"):
                raise ManagerError("a denoiser install is already running")
            self._denoiser_install = {"phase": "starting", "fraction": 0.0, "error": None, "done": False, "exe": None}
            self.status.log(f"denoiser: installing the latest release from {REPO_URL} into {Path(self.cfg.work_dir) / 'denoiser'}")

        def progress(phase: str, frac: float) -> None:
            with self.lock:
                if self._denoiser_install is not None:
                    self._denoiser_install.update(phase=phase, fraction=frac)

        def run() -> None:
            try:
                exe = install_denoiser(self.cfg.work_dir, progress)
            except Exception as e:  # noqa: BLE001
                log.exception("denoiser install failed")
                with self.lock:
                    self._denoiser_install.update(phase="failed", error=f"{type(e).__name__}: {e}", done=True)
                    self.status.log(f"denoiser: install failed: {e}", level="error")
                return
            with self.lock:
                self.cfg.denoiser_exe = exe
                self._denoiser_install.update(phase="installed", fraction=1.0, done=True, exe=exe)
                self.status.log(f"denoiser: installed and set to {exe}")
                try:
                    self.save_config()
                except OSError:
                    log.exception("could not save config after denoiser install")
                self._publish()

        threading.Thread(target=run, name="denoiser-install", daemon=True).start()

    # -- settings ------------------------------------------------------------------
    def update_settings(self, workers: Optional[List[dict]] = None, shutdown_after_queue: Optional[bool] = None,
                        job_timeout_s: Optional[float] = None, stall_timeout_s: Optional[float] = None,
                        max_attempts: Optional[int] = None, image_size: Optional[list] = None,
                        daz_exe: Optional[str] = None, web_port: Optional[int] = None,
                        mode: Optional[str] = None, notifications: Optional[dict] = None,
                        denoiser_exe: Optional[str] = None, denoise_keep_original: Optional[bool] = None,
                        anim_split: Optional[bool] = None, actions: Optional[list] = None) -> None:
        with self.lock:
            if actions is not None:
                from .actions import validate_actions
                try:
                    new = validate_actions(actions)
                except ValueError as e:
                    raise ManagerError(str(e))
                if new != self.cfg.actions:
                    self.cfg.actions = new
                    self.status.log(f"action stock: {len(new)} action(s): " + ", ".join(a["label"] for a in new) if new else "action stock cleared")
            if anim_split is not None and bool(anim_split) != self.cfg.anim_split:
                self.cfg.anim_split = bool(anim_split)
                self.status.log("animations: " + ("frames split across free workers" if self.cfg.anim_split else "one worker per animation"))
            if denoise_keep_original is not None and bool(denoise_keep_original) != self.cfg.denoise_keep_original:
                self.cfg.denoise_keep_original = bool(denoise_keep_original)
                self.status.log("denoise: " + ("keep original, write _denoised copy" if self.cfg.denoise_keep_original else "replace the render in place"))
            if denoiser_exe is not None and denoiser_exe != self.cfg.denoiser_exe:
                from .denoise import validate_denoiser
                self.cfg.denoiser_exe = denoiser_exe.strip()
                ok, info = validate_denoiser(self.cfg.denoiser_exe)
                self.status.log("denoiser: " + (f"set to {info}" if ok else f"{self.cfg.denoiser_exe or '(none)'}: {info}; denoise flags are skipped"),
                                level="info" if ok or not self.cfg.denoiser_exe else "warning")
            if notifications is not None:
                for k in self.cfg.notifications:
                    if k in notifications:
                        self.cfg.notifications[k] = bool(notifications[k])
            if mode is not None and mode != self.cfg.mode:
                if self.running:
                    raise ManagerError("cannot change the scheduling mode while rendering")
                if mode not in MODES:
                    raise ManagerError(f"unknown mode {mode!r}")
                self.cfg.mode = mode
                self.status.log("scheduling mode: " + ("one scene at a time" if mode == MODE_COMBINED else "one scene per worker"))
                self.status = self._fresh_status()
                self._publish()
            if web_port is not None and int(web_port) != self.cfg.web_port:
                port = int(web_port)
                if not 1024 <= port <= 65535:
                    raise ManagerError("port must be between 1024 and 65535")
                if self.on_port_change is not None:
                    self.on_port_change(port)  # raises ManagerError when the port is taken
                self.cfg.web_port = port
                self.status.log(f"web port set to {port}")
            if workers is not None:
                if self.running:
                    raise ManagerError("cannot change workers while rendering")
                slots = []
                for w in workers:
                    name = str(w.get("name", "")).strip()
                    if not name or any(ch in name for ch in ' \\/:*?"<>|[]'):
                        raise ManagerError(f"bad worker name: {name!r}")
                    gpu = w.get("gpu")
                    slots.append(WorkerSlot(name, _parse_gpu(gpu), bool(w.get("enabled", True))))
                if not slots:
                    raise ManagerError("at least one worker is needed")
                if len({w.name for w in slots}) != len(slots):
                    raise ManagerError("worker names must be unique")
                if [w.to_dict() for w in slots] != [w.to_dict() for w in self.cfg.workers]:
                    self.status.log("workers set to " + ", ".join(
                        f"{w.name}(gpu {w.gpu_text}{'' if w.enabled else ', disabled'})" for w in slots))
                self.cfg.workers = slots
                self.status = self._fresh_status()
                self._publish()
            if shutdown_after_queue is not None and bool(shutdown_after_queue) != self.cfg.shutdown_after_queue:
                self.cfg.shutdown_after_queue = bool(shutdown_after_queue)
                self.status.shutdown_after_queue = self.cfg.shutdown_after_queue
                self.status.log("shutdown after queue " + ("enabled" if self.cfg.shutdown_after_queue else "disabled"))
            if image_size is not None and (tuple(image_size) if image_size else None) != self.cfg.image_size:
                self.status.log("image size override " + ("x".join(str(x) for x in image_size) if image_size else "cleared"))
            if job_timeout_s:
                self.cfg.job_timeout_s = float(job_timeout_s)
            if stall_timeout_s:
                self.cfg.stall_timeout_s = float(stall_timeout_s)
            if max_attempts:
                self.cfg.max_attempts = int(max_attempts)
            if image_size is not None:
                self.cfg.image_size = tuple(int(x) for x in image_size) if image_size else None
            if daz_exe:
                self.cfg.daz_exe = daz_exe
            self.save_config()

    # -- browsing / lookups --------------------------------------------------------
    def remember_browse(self, key: str, directory: str) -> None:
        """A picker was confirmed in this folder: its next opening starts there."""
        key = str(key or "").strip()
        if not key or not directory:
            return
        d = Path(directory)
        if d.is_file():
            d = d.parent
        with self.lock:
            self.browse_dirs[key] = d.as_posix()
            self._save_state()

    def browse(self, directory: Optional[str], kind: str = "scene", key: Optional[str] = None,
               fallback: Optional[str] = None) -> dict:
        """List a folder for the file picker. Without a folder, a picker with a key starts
        in its own last confirmed folder (when it still exists), else in `fallback`, else
        in the default. Any folder that cannot be used (missing, a file, unreadable,
        unreachable drive, garbage text) silently falls back to the user's Documents folder."""
        requested = directory
        try:
            d = Path(directory) if directory else self._default_browse_dir(kind)
            if d.is_file():
                d = d.parent
            listing = self._list_dir(d, kind)
        except (OSError, ValueError):
            listing = None
        if listing is None:
            d = documents_dir()
            try:
                listing = self._list_dir(d, kind)
            except (OSError, ValueError):
                d = Path.home()
                listing = self._list_dir(d, kind)
        if not directory:
            for cand in (self.browse_dirs.get(key) if key else None, fallback):
                try:
                    if cand and Path(cand).is_dir():
                        directory = cand
                        break
                except (OSError, ValueError):
                    pass
        dirs, files = listing
        parent = d.parent if d.parent != d else None
        return {"dir": d.as_posix(), "parent": parent.as_posix() if parent else None, "dirs": dirs, "files": files,
                "drives": _drives(), "fallback": bool(requested) and Path(requested) != d and d == documents_dir()}

    @staticmethod
    def _list_dir(d: Path, kind: str) -> tuple:
        """(dirs, files) of a folder; raises OSError when it cannot be read."""
        if not d.is_dir():
            raise NotADirectoryError(str(d))
        dirs, files = [], []
        for entry in sorted(d.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith(".") or entry.name.startswith("$"):
                continue
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
                    continue
                if (kind == "scene" and entry.suffix.lower() in SCENE_EXTS) or \
                   (kind == "queue" and entry.name.lower().endswith(QUEUE_SUFFIX)) or \
                   (kind == "exe" and entry.suffix.lower() == ".exe") or kind == "any":
                    files.append({"name": entry.name, "mtime": entry.stat().st_mtime})
            except OSError:
                continue  # one unreadable entry must not sink the listing
        return dirs, files

    def _default_browse_dir(self, kind: str = "scene") -> Path:
        if self.queue is not None:
            candidates = []
            if self.queue.path:
                candidates.append(self.queue.path.parent)
            for s in reversed(self.queue.scenes):
                candidates.append(Path(s.file).parent)
                break
            if kind != "queue":
                candidates.reverse()  # scene picker: scene folder first, queue folder second
            for c in candidates:
                if c.is_dir():
                    return c
        return documents_dir()

    def _check_action_ids(self, ids: Optional[list]) -> list:
        """Only ids that exist in the stock, in the given order."""
        if not ids:
            return []
        stock = {a["id"] for a in self.cfg.actions}
        unknown = [str(a) for a in ids if str(a) not in stock]
        if unknown:
            raise ManagerError("unknown action(s): " + ", ".join(unknown))
        return [str(a) for a in ids]

    def scene_animinfo(self, file: str) -> dict:
        """Timeline facts of a scene file (for the Add dialog), see dufinfo.anim_info."""
        p = Path(file)
        if p.suffix.lower() not in SCENE_EXTS or not p.is_file():
            raise ManagerError(f"not a scene file: {file}")
        return dufinfo.anim_info(p)

    def scene_cameras(self, index: int) -> dict:
        sc = self._scene(index)
        doc = dufinfo.load_duf(sc.file)
        return {"current": dufinfo.current_camera_label(sc.file, doc), "cameras": dufinfo.camera_labels(sc.file, doc)}

    def display_file(self, sc) -> Optional[Path]:
        return _display_file(sc)

    def thumbnail(self, index: int, width: int = 400) -> Optional[Path]:
        """Cached scaled copy of a scene's rendered target, or None if not rendered yet."""
        sc = self._scene(index)
        target = self.display_file(sc)
        if target is None:
            return None
        from PIL import Image  # optional dependency only needed here
        cache = Path(self.cfg.work_dir) / "thumbs"
        cache.mkdir(parents=True, exist_ok=True)
        st = target.stat()
        out = cache / f"{target.stem}_{int(st.st_mtime)}_{width}.jpg"
        if not out.exists():
            with Image.open(target) as im:
                im = im.convert("RGB")
                im.thumbnail((width, width))
                im.save(out, "JPEG", quality=85)
        return out

    def _file_mtime(self, path: str) -> Optional[float]:
        now = time.time()
        hit = self._mtime_cache.get(path)
        if hit and now - hit[0] < 30:
            return hit[1]
        try:
            m = os.stat(path).st_mtime
        except OSError:
            m = None
        self._mtime_cache[path] = (now, m)
        return m

    # -- snapshot for the UI -------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            self._retry_last_queue()
            snap = self.status.snapshot()
            snap["running"] = self.running
            snap["firstRun"] = self.first_run
            snap["version"] = __version__
            snap["shutdownAt"] = self.shutdown_at if self.shutdown_pending else None
            q = self.queue
            scenes = []
            if q is not None:
                running_index = {w["sceneIndex"]: w["name"] for w in snap["workers"] if w["sceneIndex"] is not None}
                for s in q.scenes:
                    d = s.to_dict()
                    d["mtime"] = self._file_mtime(s.file)
                    d["fileExists"] = d["mtime"] is not None  # .duf moved away after rendering is normal
                    d["postLoaders"] = s.post_loaders
                    d["savedCamera"] = s.uses_saved_camera
                    d["worker"] = running_index.get(s.index)
                    shown = _display_file(s)
                    d["targetExists"] = shown is not None
                    d["shownFile"] = shown.as_posix() if shown else None
                    d["denoisedFile"] = _denoised_file(s.target) if not s.anim else None
                    scenes.append(d)
                snap["queue"]["log"] = q.data.get("log", [])[-30:]
            snap["scenes"] = scenes
            snap["recent"] = list(self.recent)
            snap["config"] = {
                "workers": [w.to_dict() for w in self.cfg.workers], "dazExe": self.cfg.daz_exe,
                "jobTimeoutS": self.cfg.job_timeout_s, "stallTimeoutS": self.cfg.stall_timeout_s,
                "maxAttempts": self.cfg.max_attempts, "imageSize": list(self.cfg.image_size) if self.cfg.image_size else None,
                "shutdownAfterQueue": self.cfg.shutdown_after_queue, "workDir": self.cfg.work_dir,
                "webPort": self.cfg.web_port, "mode": self.cfg.mode, "notifications": dict(self.cfg.notifications),
                "animSplit": self.cfg.anim_split, "actions": [dict(a) for a in self.cfg.actions],
                "denoiserExe": self.cfg.denoiser_exe, "denoiserValid": _denoiser_valid(self.cfg.denoiser_exe),
                "denoiseKeepOriginal": self.cfg.denoise_keep_original,
                "denoiserInstall": dict(self._denoiser_install) if self._denoiser_install else None,
                "runSlots": [w.to_dict() for w in self.cfg.run_slots()],
            }
            snap["gpus"] = _gpus()
            return snap


def _parse_size(value) -> Optional[tuple]:
    """[w, h] / (w, h) -> (w, h) of positive ints; anything else -> None."""
    try:
        if value and len(value) == 2 and int(value[0]) > 0 and int(value[1]) > 0:
            return int(value[0]), int(value[1])
    except (TypeError, ValueError):
        pass
    if value:
        raise ManagerError("render size needs a positive width and height")
    return None


def _display_file(sc) -> Optional[Path]:
    """The image to show for a scene: its target, or the latest finished frame of an animation."""
    if sc.anim:
        for f in reversed(sc.frames_done):
            p = Path(sc.frame_target(f))
            if p.is_file():
                return p
        return None
    p = Path(sc.target) if sc.target else None
    return p if p and p.is_file() else None


def _denoised_file(target: str) -> Optional[str]:
    """Path of the kept-original denoised copy if it exists, else None."""
    if not target:
        return None
    from .denoise import denoised_path
    p = denoised_path(target)
    return p.as_posix() if p.is_file() else None


def _denoiser_valid(path: str) -> bool:
    from .denoise import validate_denoiser
    return validate_denoiser(path)[0]


def _gpus() -> list:
    """Latest GPU telemetry sample (see gpumon)."""
    return gpumon.gpus()


def documents_dir() -> Path:
    """The user's Documents folder (Windows known folder), falling back to the home dir."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5 -> "My Documents", honours a relocated Documents folder
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except (OSError, AttributeError):
            pass
    d = Path.home() / "Documents"
    return d if d.is_dir() else Path.home()


def _drives() -> list:
    if os.name != "nt":
        return ["/"]
    return [f"{c}:/" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{c}:/").exists()]
