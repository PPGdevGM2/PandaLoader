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


MODE_PARALLEL = "parallel"   # one scene per enabled worker
MODE_COMBINED = "combined"   # one scene at a time, all enabled workers' GPUs in one DAZ instance
MODES = (MODE_PARALLEL, MODE_COMBINED)


@dataclass
class WorkerSlot:
    """One worker: a DAZ instance name and the CUDA device(s) it is pinned to.

    ``gpu`` is an int for one card, a comma string such as "0,1" for several,
    or None for every card. ``enabled`` is the user's toggle on the worker card.
    """
    name: str
    gpu: Optional[object] = None
    enabled: bool = True

    @classmethod
    def parse(cls, text: str) -> "WorkerSlot":
        """Parse 'rqgpu0:0', 'rqboth:0,1' or 'rqall' (no pin)."""
        name, _, gpu = text.partition(":")
        name = name.strip()
        if not name:
            raise ValueError(f"empty worker name in {text!r}")
        return cls(name, _parse_gpu(gpu))

    @property
    def gpu_text(self) -> str:
        return "all" if self.gpu is None else str(self.gpu)

    @property
    def gpu_list(self) -> Optional[list]:
        """CUDA indices as ints, or None for all."""
        if self.gpu is None:
            return None
        return [int(x) for x in str(self.gpu).split(",") if str(x).strip() != ""]

    def to_dict(self) -> dict:
        return {"name": self.name, "gpu": self.gpu, "enabled": self.enabled}


def _parse_gpu(value) -> Optional[object]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "all", "none"):
        return None
    parts = [int(p) for p in text.split(",") if p.strip() != ""]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else ",".join(str(p) for p in parts)


def combined_slot(workers: list, name: str = "rqall") -> WorkerSlot:
    """One slot that uses every GPU of the enabled workers (None if any of them uses all)."""
    gpus: list = []
    for w in workers:
        if not w.enabled:
            continue
        if w.gpu_list is None:
            return WorkerSlot(name, None)
        gpus.extend(w.gpu_list)
    uniq = sorted(set(gpus))
    return WorkerSlot(name, uniq[0] if len(uniq) == 1 else ",".join(str(g) for g in uniq) if uniq else None)


@dataclass
class Config:
    daz_exe: str = DEFAULT_DAZ_EXE
    daz_args: list = field(default_factory=lambda: list(DEFAULT_DAZ_ARGS))
    workers: list = field(default_factory=lambda: [WorkerSlot("rqgpu0", 0), WorkerSlot("rqgpu1", 1)])
    mode: str = MODE_PARALLEL          # parallel: one scene per worker; combined: one scene at a time
    combined_name: str = "rqall"       # instance name used in combined mode
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
    web_port: int = 6969

    # -- derived paths -----------------------------------------------------
    def instance_dir(self, instance: str) -> Path:
        return Path(self.instance_dir_pattern.format(instance=instance))

    def instance_log(self, instance: str) -> Path:
        return self.instance_dir(instance) / "log.txt"

    def jobs_dir(self) -> Path:
        return Path(self.work_dir) / "jobs"

    def enabled_workers(self) -> list:
        return [w for w in self.workers if w.enabled]

    def run_slots(self) -> list:
        """The slots a run actually uses, according to the scheduling mode."""
        enabled = self.enabled_workers()
        if not enabled:
            return []
        if self.mode == MODE_COMBINED:
            return [combined_slot(enabled, self.combined_name)]
        return enabled

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["workers"] = [w.to_dict() for w in self.workers]
        d["image_size"] = list(self.image_size) if self.image_size else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        d = dict(d)
        workers = []
        for w in d.get("workers", []):
            if isinstance(w, dict):
                workers.append(WorkerSlot(str(w.get("name", "")), _parse_gpu(w.get("gpu")), bool(w.get("enabled", True))))
            else:
                workers.append(WorkerSlot.parse(w))
        d["workers"] = [w for w in workers if w.name] or cls().workers
        if d.get("mode") not in MODES:
            d["mode"] = MODE_PARALLEL
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
