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
from typing import List, Optional

from . import dufinfo
from .config import Config, WorkerSlot
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
        self._mtime_cache: dict = {}
        self._load_state()
        for slot in cfg.workers:
            self.status.add_worker(slot.name, slot.gpu)
        self.status.shutdown_after_queue = cfg.shutdown_after_queue
        self.status.log("PandaLoader started")

    # -- persistence of the manager's own little state ------------------------
    def _load_state(self) -> None:
        try:
            d = json.loads(self._state_file.read_text(encoding="utf-8"))
            self.recent = [p for p in d.get("recent", []) if Path(p).exists()]
            last = d.get("lastQueue")
            if last and Path(last).exists():
                self.queue = QueueFile.load(last)
                self.status.set_queue(last, self.queue.counts(), self.queue.average_secs())
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({
                "lastQueue": str(self.queue.path) if self.queue and self.queue.path else None,
                "recent": self.recent[:12],
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
        for slot in self.cfg.workers:
            st.add_worker(slot.name, slot.gpu)
        st.shutdown_after_queue = self.cfg.shutdown_after_queue
        return st

    def _publish(self) -> None:
        if self.queue is not None:
            self.status.set_queue(str(self.queue.path or ""), self.queue.counts(), self.queue.average_secs())

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
            if q.counts()["pending"] == 0 and q.counts()["running"] == 0:
                raise ManagerError("nothing pending; reset scenes first")
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

    def shutdown_workers_now(self) -> None:
        """Emergency: kill every DAZ worker instance we know of."""
        for slot in self.cfg.workers:
            subprocess.run(["taskkill", "/F", "/FI", f"WINDOWTITLE eq *[{slot.name}]*"], capture_output=True, check=False)

    def _on_finished(self) -> None:
        if self.cfg.shutdown_after_queue:
            self.status.log("queue finished: shutting down the PC in 60 s (shutdown /a aborts)", level="warning")
            subprocess.run(["shutdown", "/s", "/t", "60", "/c", "PandaLoader: render queue finished"], check=False)

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
                   post_loaders: Optional[List[str]] = None, at_top: bool = False) -> int:
        with self.lock, self._qlock():
            q = self._require_queue()
            if post_loaders is None:
                post_loaders = self._default_post_loaders()
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
                sc = q.add_scene(p.as_posix(), target, post_loaders)
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

    def reset_scene(self, index: int) -> None:
        with self.lock, self._qlock():
            sc = self._scene(index)
            if sc.state == STATE_RUNNING:
                raise ManagerError("scene is rendering")
            sc.mark_pending()
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

    def edit_scene(self, index: int, target: Optional[str] = None, post_loaders: Optional[List[str]] = None,
                   camera: Optional[str] = None) -> None:
        """camera: '' or None keeps 'camera saved with scene'; a label forces that camera."""
        with self.lock, self._qlock():
            sc = self._scene(index)
            if sc.state == STATE_RUNNING:
                raise ManagerError("scene is rendering")
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

    # -- settings ------------------------------------------------------------------
    def update_settings(self, workers: Optional[List[dict]] = None, shutdown_after_queue: Optional[bool] = None,
                        job_timeout_s: Optional[float] = None, stall_timeout_s: Optional[float] = None,
                        max_attempts: Optional[int] = None, image_size: Optional[list] = None,
                        daz_exe: Optional[str] = None) -> None:
        with self.lock:
            if workers is not None:
                if self.running:
                    raise ManagerError("cannot change workers while rendering")
                slots = []
                for w in workers:
                    name = str(w.get("name", "")).strip()
                    if not name or any(ch in name for ch in ' \\/:*?"<>|[]'):
                        raise ManagerError(f"bad worker name: {name!r}")
                    gpu = w.get("gpu")
                    slots.append(WorkerSlot(name, None if gpu in (None, "", "all") else int(gpu)))
                if not slots:
                    raise ManagerError("at least one worker is needed")
                self.cfg.workers = slots
                self.status = self._fresh_status()
                self._publish()
            if shutdown_after_queue is not None:
                self.cfg.shutdown_after_queue = bool(shutdown_after_queue)
                self.status.shutdown_after_queue = self.cfg.shutdown_after_queue
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
    def browse(self, directory: Optional[str], kind: str = "scene") -> dict:
        d = Path(directory) if directory else (self._default_browse_dir())
        if not d.exists() or not d.is_dir():
            raise ManagerError(f"not a folder: {d}")
        dirs, files = [], []
        try:
            for entry in sorted(d.iterdir(), key=lambda p: p.name.lower()):
                if entry.name.startswith(".") or entry.name.startswith("$"):
                    continue
                if entry.is_dir():
                    dirs.append(entry.name)
                elif kind == "scene" and entry.suffix.lower() in SCENE_EXTS:
                    files.append({"name": entry.name, "mtime": entry.stat().st_mtime})
                elif kind == "queue" and entry.name.lower().endswith(QUEUE_SUFFIX):
                    files.append({"name": entry.name, "mtime": entry.stat().st_mtime})
                elif kind == "any":
                    files.append({"name": entry.name, "mtime": entry.stat().st_mtime})
        except PermissionError:
            raise ManagerError(f"no access to {d}")
        parent = d.parent if d.parent != d else None
        return {"dir": d.as_posix(), "parent": parent.as_posix() if parent else None, "dirs": dirs, "files": files,
                "drives": _drives()}

    def _default_browse_dir(self) -> Path:
        if self.queue is not None:
            for s in reversed(self.queue.scenes):
                if Path(s.file).parent.exists():
                    return Path(s.file).parent
            if self.queue.path and self.queue.path.parent.exists():
                return self.queue.path.parent
        return Path.home()

    def scene_cameras(self, index: int) -> dict:
        sc = self._scene(index)
        doc = dufinfo.load_duf(sc.file)
        return {"current": dufinfo.current_camera_label(sc.file, doc), "cameras": dufinfo.camera_labels(sc.file, doc)}

    def thumbnail(self, index: int, width: int = 400) -> Optional[Path]:
        """Cached scaled copy of a scene's rendered target, or None if not rendered yet."""
        sc = self._scene(index)
        target = Path(sc.target)
        if not target.exists():
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
            snap = self.status.snapshot()
            snap["running"] = self.running
            q = self.queue
            scenes = []
            if q is not None:
                running_index = {w["sceneIndex"]: w["name"] for w in snap["workers"] if w["sceneIndex"] is not None}
                for s in q.scenes:
                    d = s.to_dict()
                    d["mtime"] = self._file_mtime(s.file)
                    d["postLoaders"] = s.post_loaders
                    d["savedCamera"] = s.uses_saved_camera
                    d["worker"] = running_index.get(s.index)
                    d["targetExists"] = Path(s.target).exists() if s.target else False
                    scenes.append(d)
                snap["queue"]["log"] = q.data.get("log", [])[-30:]
            snap["scenes"] = scenes
            snap["recent"] = list(self.recent)
            snap["config"] = {
                "workers": [w.to_dict() for w in self.cfg.workers], "dazExe": self.cfg.daz_exe,
                "jobTimeoutS": self.cfg.job_timeout_s, "stallTimeoutS": self.cfg.stall_timeout_s,
                "maxAttempts": self.cfg.max_attempts, "imageSize": list(self.cfg.image_size) if self.cfg.image_size else None,
                "shutdownAfterQueue": self.cfg.shutdown_after_queue, "workDir": self.cfg.work_dir,
                "webPort": self.cfg.web_port,
            }
            snap["gpus"] = _gpus()
            return snap


_GPU_CACHE = {"t": 0.0, "v": []}


def _gpus() -> list:
    """Names of the CUDA devices, via nvidia-smi, cached for a minute."""
    if time.time() - _GPU_CACHE["t"] < 60:
        return _GPU_CACHE["v"]
    out = []
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5, check=False)
        for line in r.stdout.splitlines():
            idx, _, name = line.partition(",")
            if idx.strip().isdigit():
                out.append({"index": int(idx), "name": name.strip()})
    except (OSError, subprocess.SubprocessError):
        pass
    _GPU_CACHE.update(t=time.time(), v=out)
    return out


def _drives() -> list:
    if os.name != "nt":
        return ["/"]
    return [f"{c}:/" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{c}:/").exists()]
