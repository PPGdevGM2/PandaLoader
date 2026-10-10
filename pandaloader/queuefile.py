"""Reader/writer for the Render Queue 3 "*.rq.json" format.

The file layout is kept exactly as ManFriday's plugin wrote it so that existing
queues stay usable. Unknown keys are preserved untouched.

Scene states: 2 = done and 3 = in progress are the plugin's values; 0 (pending)
and 4 (failed) are PandaLoader's additions.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

STATE_PENDING = 0
STATE_DONE = 2
STATE_RUNNING = 3
STATE_FAILED = 4

STATE_NAMES = {STATE_PENDING: "pending", STATE_DONE: "done", STATE_RUNNING: "running", STATE_FAILED: "failed"}


class Scene:
    """Thin view over one entry of the "scenes" array."""

    def __init__(self, data: dict, index: int):
        self.data = data
        self.index = index

    # -- plain fields --------------------------------------------------------
    @property
    def file(self) -> str:
        return self.data.get("file", "")

    @property
    def target(self) -> str:
        return self.data.get("target", "")

    @target.setter
    def target(self, value: str) -> None:
        self.data["target"] = value

    @property
    def state(self) -> int:
        return int(self.data.get("state", STATE_PENDING))

    @state.setter
    def state(self, value: int) -> None:
        self.data["state"] = int(value)

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, f"state{self.state}")

    @property
    def is_done(self) -> bool:
        return self.state == STATE_DONE

    @property
    def is_pending(self) -> bool:
        return self.state not in (STATE_DONE, STATE_RUNNING, STATE_FAILED)

    @property
    def post_loaders(self) -> list:
        """Assets merged after the scene loads (render-settings presets)."""
        return [p.get("asset", "") for p in self.data.get("postLoaders", []) if p.get("asset")]

    @property
    def frame(self) -> int:
        sf = int(self.data.get("singleFrame", -1))
        return sf if sf >= 0 else 0

    @property
    def name(self) -> str:
        return Path(self.file).stem

    @property
    def last_error(self) -> str:
        return self.data.get("lastError", "")

    # -- post-render denoise (PandaLoader addition to the format) --------------
    @property
    def denoise(self) -> bool:
        return bool(self.data.get("denoise", False))

    @denoise.setter
    def denoise(self, value: bool) -> None:
        if value:
            self.data["denoise"] = True
        else:  # keep the plugin's file layout untouched unless the flag is on
            self.data.pop("denoise", None)
            self.data.pop("denoised", None)

    @property
    def denoised(self) -> Optional[bool]:
        """True/False after a render with the flag set, None if never attempted."""
        return self.data.get("denoised")

    # -- camera --------------------------------------------------------------
    @property
    def uses_saved_camera(self) -> bool:
        return int(self.data.get("cam", 1)) == 1

    @property
    def camera_hint(self) -> Optional[str]:
        """Camera label recorded by a previous run, or an explicit single camera name."""
        single = self.data.get("singleCamName")
        if isinstance(single, str) and single:
            return single
        for rec in reversed(self.data.get("secsRender", [])):
            if rec.get("type") == "scene" and rec.get("cam"):
                return rec["cam"]
        return None

    # -- history -------------------------------------------------------------
    @property
    def last_secs(self) -> Optional[float]:
        for rec in reversed(self.data.get("secsRender", [])):
            if rec.get("type") == "scene" and rec.get("secs"):
                return float(rec["secs"])
        return None

    # -- state transitions ---------------------------------------------------
    def mark_running(self) -> None:
        self.state = STATE_RUNNING

    def mark_pending(self) -> None:
        self.state = STATE_PENDING

    def mark_done(self, secs: float, camera_label: str, frame: int = 0) -> None:
        self.state = STATE_DONE
        self.data.pop("lastError", None)
        self.data.setdefault("secsRender", []).append({
            "type": "scene", "asset": "", "cam": camera_label, "frame": int(frame), "secs": int(round(secs)),
        })

    def mark_failed(self, message: str) -> None:
        self.state = STATE_FAILED
        self.data["lastError"] = message[:500]

    def to_dict(self) -> dict:
        return {
            "index": self.index, "name": self.name, "file": self.file, "target": self.target,
            "state": self.state, "stateName": self.state_name, "camera": self.camera_hint,
            "lastSecs": self.last_secs, "lastError": self.last_error,
            "denoise": self.denoise, "denoised": self.denoised,
        }


class QueueFile:
    def __init__(self, data: dict, path: Optional[Path] = None):
        self.data = data
        self.path = Path(path) if path else None
        data.setdefault("renderqVersion", 1)
        data.setdefault("scenes", [])
        data.setdefault("log", [])

    # -- io ------------------------------------------------------------------
    @classmethod
    def load(cls, path) -> "QueueFile":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f), path)

    @classmethod
    def empty(cls, path=None) -> "QueueFile":
        return cls({"renderqVersion": 1, "scenes": [], "log": []}, path)

    def save(self, path=None, retries: int = 10) -> Path:
        """Atomic save: write a sibling temp file, then replace. Retries on sync locks."""
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("queue has no path")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        last: Optional[Exception] = None
        for _ in range(retries):
            try:
                os.replace(tmp, target)
                self.path = target
                return target
            except PermissionError as e:  # Dropbox or antivirus holding the file
                last = e
                time.sleep(0.5)
        assert last is not None
        raise last

    # -- scenes --------------------------------------------------------------
    @property
    def scenes(self) -> list:
        return [Scene(s, i) for i, s in enumerate(self.data["scenes"])]

    def scene(self, index: int) -> Scene:
        return Scene(self.data["scenes"][index], index)

    def add_scene(self, file: str, target: str, post_loaders: Iterable[str] = (), frame_first: int = 0,
                  frame_last: int = 30, denoise: bool = False) -> Scene:
        entry = {
            "file": file, "state": STATE_PENDING, "target": target, "jobs": [], "cam": 1,
            "singleCamName": 0, "singleFrame": -1, "frameFirst": frame_first, "frameLast": frame_last,
            "postLoaders": [{"asset": a, "node": ""} for a in post_loaders], "secsRender": [],
        }
        if denoise:
            entry["denoise"] = True
        self.data["scenes"].append(entry)
        return Scene(entry, len(self.data["scenes"]) - 1)

    def next_pending(self, exclude: Iterable[int] = ()) -> Optional[Scene]:
        skip = set(exclude)
        for s in self.scenes:
            if s.is_pending and s.index not in skip:
                return s
        return None

    def reset_running(self) -> int:
        """Scenes left in state 3 by a crash go back to pending. Returns how many."""
        n = 0
        for s in self.scenes:
            if s.state == STATE_RUNNING:
                s.mark_pending()
                n += 1
        return n

    def reset(self, failed_only: bool = False) -> int:
        n = 0
        for s in self.scenes:
            if s.state == STATE_FAILED or (not failed_only and s.state != STATE_PENDING):
                s.mark_pending()
                s.data.pop("lastError", None)
                n += 1
        return n

    def counts(self) -> dict:
        c = {"total": 0, "pending": 0, "running": 0, "done": 0, "failed": 0}
        for s in self.scenes:
            c["total"] += 1
            if s.is_done:
                c["done"] += 1
            elif s.state == STATE_RUNNING:
                c["running"] += 1
            elif s.state == STATE_FAILED:
                c["failed"] += 1
            else:
                c["pending"] += 1
        return c

    def average_secs(self) -> Optional[float]:
        vals = [s.last_secs for s in self.scenes if s.is_done and s.last_secs]
        return sum(vals) / len(vals) if vals else None

    # -- log -----------------------------------------------------------------
    def append_log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.data["log"].append(f"[{stamp}] {message}")
