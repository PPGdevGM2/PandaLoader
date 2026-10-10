"""Configuration model for PandaLoader.

Everything that differs between machines or between a real run and a test run
lives here, so the worker and scheduler never hard-code paths or launch flags.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

DEFAULT_DAZ_EXE = "C:/Program Files/DAZ 3D/DAZStudio6/DAZStudio.exe"

# Placeholders: {instance} = worker instance name, {job} = path of the job .dsa.
# -copyAppSettings is mandatory: it inherits the GPU-only hardware selection.
DEFAULT_DAZ_ARGS = [
    "-instanceName", "{instance}",
    "-copyAppSettings", ".",
    "-headless", "-noPrompt", "-noDefaultScene",
    "{job}",
]


def appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


@dataclass
class WorkerSlot:
    """One worker: a DAZ instance name and the CUDA device it is pinned to (None = all)."""
    name: str
    gpu: Optional[int] = None

    @classmethod
    def parse(cls, text: str) -> "WorkerSlot":
        """Parse 'rqgpu0:0' or 'rqall' (no pin)."""
        name, _, gpu = text.partition(":")
        name = name.strip()
        if not name:
            raise ValueError(f"empty worker name in {text!r}")
        return cls(name, int(gpu) if gpu.strip() else None)

    def to_dict(self) -> dict:
        return {"name": self.name, "gpu": self.gpu}


@dataclass
class Config:
    daz_exe: str = DEFAULT_DAZ_EXE
    daz_args: list = field(default_factory=lambda: list(DEFAULT_DAZ_ARGS))
    workers: list = field(default_factory=lambda: [WorkerSlot("rqgpu0", 0), WorkerSlot("rqgpu1", 1)])
    # Where a named instance writes its log.txt. {instance} is substituted.
    instance_dir_pattern: str = field(default_factory=lambda: str(appdata() / "DAZ 3D" / "Studio6 [{instance}]"))
    # Job scripts, result files and the manager's own log go here.
    work_dir: str = field(default_factory=lambda: str(localappdata() / "PandaLoader"))
    job_timeout_s: float = 3600.0      # whole process, start to exit
    stall_timeout_s: float = 900.0     # no new log line for this long -> kill
    poll_interval_s: float = 1.0
    settle_wait_s: int = 3             # seconds the job script idles after loading
    max_attempts: int = 2              # a failed scene is retried until this many attempts
    default_render_secs: float = 510.0 # ETA fallback when a scene has no history
    startup_overhead_s: float = 70.0   # process start + scene load, for ETA
    shutdown_after_queue: bool = False
    image_size: Optional[tuple] = None # (w, h) override; None = take it from the preset
    web_host: str = "0.0.0.0"
    web_port: int = 8765

    # -- derived paths -----------------------------------------------------
    def instance_dir(self, instance: str) -> Path:
        return Path(self.instance_dir_pattern.format(instance=instance))

    def instance_log(self, instance: str) -> Path:
        return self.instance_dir(instance) / "log.txt"

    def jobs_dir(self) -> Path:
        return Path(self.work_dir) / "jobs"

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["workers"] = [w.to_dict() for w in self.workers]
        d["image_size"] = list(self.image_size) if self.image_size else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        d = dict(d)
        workers = [WorkerSlot(**w) if isinstance(w, dict) else WorkerSlot.parse(w) for w in d.get("workers", [])]
        d["workers"] = workers or cls().workers
        if d.get("image_size"):
            d["image_size"] = tuple(int(x) for x in d["image_size"])
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def load(cls, path) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save(self, path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
