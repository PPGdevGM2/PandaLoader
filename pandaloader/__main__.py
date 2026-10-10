"""Command line entry point.

    python -m pandaloader run QUEUE.rq.json [options]
    python -m pandaloader status QUEUE.rq.json
    python -m pandaloader reset QUEUE.rq.json [--failed]
    python -m pandaloader camera SCENE.duf
    python -m pandaloader preset PRESET.duf
    python -m pandaloader config [--write FILE]
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import dufinfo
from .config import Config, WorkerSlot
from .queuefile import QueueFile
from .scheduler import Scheduler
from .state import Status, _hms


def _load_config(args) -> Config:
    cfg = Config.load(args.config) if getattr(args, "config", None) else Config()
    if getattr(args, "workers", None):
        cfg.workers = [WorkerSlot.parse(w) for w in args.workers]
    if getattr(args, "timeout", None):
        cfg.job_timeout_s = float(args.timeout)
    if getattr(args, "stall", None):
        cfg.stall_timeout_s = float(args.stall)
    if getattr(args, "size", None):
        w, _, h = args.size.lower().partition("x")
        cfg.image_size = (int(w), int(h))
    if getattr(args, "max_attempts", None):
        cfg.max_attempts = int(args.max_attempts)
    if getattr(args, "work_dir", None):
        cfg.work_dir = args.work_dir
    if getattr(args, "shutdown", False):
        cfg.shutdown_after_queue = True
    if getattr(args, "daz_exe", None):
        cfg.daz_exe = args.daz_exe
    return cfg


def _setup_logging(cfg: Config, verbose: bool) -> None:
    Path(cfg.work_dir).mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(Path(cfg.work_dir) / "pandaloader.log", encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def _print_event(ev: dict) -> None:
    stamp = time.strftime("%H:%M:%S", time.localtime(ev["t"]))
    who = f"[{ev['worker']}] " if ev.get("worker") else ""
    print(f"{stamp} {ev['level'].upper():7} {who}{ev['msg']}", flush=True)


def _progress_line(status: Status) -> str:
    snap = status.snapshot()
    q = snap["queue"]
    parts = [f"{q['done']}/{q['total']} done, {q['pending']} pending, {q['failed']} failed, ETA {_hms(q['eta'])}"]
    for w in snap["workers"]:
        if w["state"] == "idle":
            parts.append(f"{w['name']}: idle")
        else:
            parts.append(f"{w['name']}: {w['sceneName']} {w['state']} {int(w['iterations'])} it "
                         f"{_hms(w['renderElapsed'])} / ~{_hms(w['expectedSecs'])}")
    return " | ".join(parts)


def cmd_run(args) -> int:
    cfg = _load_config(args)
    _setup_logging(cfg, args.verbose)
    if not Path(cfg.daz_exe).exists():
        print(f"DAZ executable not found: {cfg.daz_exe}", file=sys.stderr)
        return 2
    queue = QueueFile.load(args.queue)
    status = Status(cfg.startup_overhead_s, cfg.default_render_secs)
    status.listeners.append(_print_event)
    mlog = logging.getLogger("pandaloader.events")
    status.listeners.append(lambda ev: mlog.log(
        logging.ERROR if ev["level"] == "error" else logging.WARNING if ev["level"] == "warning" else logging.INFO,
        "%s%s", f"[{ev['worker']}] " if ev.get("worker") else "", ev["msg"]))

    finished = threading.Event()

    def on_finished() -> None:
        finished.set()

    sched = Scheduler(cfg, queue, status, target_dir=args.target_dir, on_queue_finished=on_finished)
    print(f"queue: {args.queue}\n{queue.counts()}\nworkers: {[w.to_dict() for w in cfg.workers]}"
          + (f"\ntargets redirected to {args.target_dir}" if args.target_dir else "")
          + (f"\nimage size override {cfg.image_size}" if cfg.image_size else ""), flush=True)
    if queue.counts()["pending"] == 0 and queue.counts()["running"] == 0:
        print("nothing pending; use 'reset' to re-run scenes")
        return 0

    stopping = {"flag": False}

    def on_sigint(signum, frame):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        print("\nstopping: killing workers, scenes go back to pending...", flush=True)
        threading.Thread(target=sched.stop, kwargs={"wait": False}, daemon=True).start()

    signal.signal(signal.SIGINT, on_sigint)
    sched.start()
    last_print = 0.0
    while sched.alive:
        time.sleep(0.5)
        if time.time() - last_print >= args.interval:
            last_print = time.time()
            print(_progress_line(status), flush=True)
    sched.join(5)
    print(_progress_line(status))
    print(f"final: {queue.counts()}")
    if finished.is_set() and cfg.shutdown_after_queue and not stopping["flag"]:
        print("queue finished: shutting down in 60 s (run 'shutdown /a' to abort)", flush=True)
        subprocess.run(["shutdown", "/s", "/t", "60", "/c", "PandaLoader: render queue finished"], check=False)
    return 0 if queue.counts()["failed"] == 0 else 1


def cmd_serve(args) -> int:
    from .manager import Manager
    from .web import WebServer
    cfg_path = Path(args.config) if args.config else Path(Config().work_dir) / "config.json"
    cfg = Config.load(cfg_path) if cfg_path.exists() else Config()
    if args.port:
        cfg.web_port = int(args.port)
    if args.host:
        cfg.web_host = args.host
    _setup_logging(cfg, args.verbose)
    manager = Manager(cfg, cfg_path)
    if not cfg_path.exists():
        manager.save_config()
    if args.queue:
        manager.open_queue(args.queue)
    server = WebServer(manager, cfg.web_host, cfg.web_port)
    server.start()
    print(f"PandaLoader web UI: {server.url}  (Ctrl+C to quit)", flush=True)
    if not args.no_browser:
        server.open_browser()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    if manager.running:
        print("stopping workers...", flush=True)
        manager.scheduler.stop(wait=True, timeout=60)
    server.stop()
    return 0


def cmd_status(args) -> int:
    q = QueueFile.load(args.queue)
    print(json.dumps(q.counts()))
    for s in q.scenes:
        secs = f"{int(s.last_secs)} s" if s.last_secs else "-"
        err = f"  ! {s.last_error}" if s.last_error else ""
        print(f"{s.index + 1:3} {s.state_name:8} {secs:>7}  {s.camera_hint or '?':14} {s.name}{err}")
    avg = q.average_secs()
    if avg:
        print(f"average render: {int(avg)} s")
    return 0


def cmd_reset(args) -> int:
    q = QueueFile.load(args.queue)
    n = q.reset(failed_only=args.failed)
    q.append_log(f"PandaLoader: reset {n} scene(s) to pending" + (" (failed only)" if args.failed else ""))
    q.save()
    print(f"reset {n} scene(s); now {q.counts()}")
    return 0


def cmd_camera(args) -> int:
    doc = dufinfo.load_duf(args.scene)
    print("current camera:", dufinfo.current_camera_label(args.scene, doc))
    if args.all:
        print("cameras:", ", ".join(dufinfo.camera_labels(args.scene, doc)))
    return 0


def cmd_preset(args) -> int:
    print(json.dumps(dufinfo.preset_render_info(args.preset), indent=2))
    return 0


def cmd_config(args) -> int:
    cfg = Config()
    if args.write:
        cfg.save(args.write)
        print(f"wrote {args.write}")
    else:
        print(json.dumps(cfg.to_dict(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pandaloader", description="External render queue manager for DAZ Studio 6")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process a queue until nothing is pending")
    r.add_argument("queue")
    r.add_argument("--config", help="JSON config file (see 'config --write')")
    r.add_argument("--workers", nargs="+", metavar="NAME[:GPU]", help="e.g. rqgpu0:0 rqgpu1:1, or rqall for both cards")
    r.add_argument("--timeout", type=float, help="per-job timeout in seconds")
    r.add_argument("--stall", type=float, help="kill after this many seconds without log activity")
    r.add_argument("--size", help="override image size, e.g. 960x540 (test renders)")
    r.add_argument("--target-dir", help="write outputs into this folder instead of the queue targets (test renders)")
    r.add_argument("--max-attempts", type=int)
    r.add_argument("--work-dir")
    r.add_argument("--daz-exe")
    r.add_argument("--shutdown", action="store_true", help="shut the PC down after the last job")
    r.add_argument("--interval", type=float, default=15.0, help="seconds between progress lines")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    sv = sub.add_parser("serve", help="start the web UI (and keep running)")
    sv.add_argument("queue", nargs="?", help="queue to open at start (otherwise the last one)")
    sv.add_argument("--config", help="config JSON (default: <work dir>/config.json)")
    sv.add_argument("--port", type=int)
    sv.add_argument("--host")
    sv.add_argument("--no-browser", action="store_true")
    sv.add_argument("-v", "--verbose", action="store_true")
    sv.set_defaults(func=cmd_serve)

    s = sub.add_parser("status", help="show the scenes of a queue")
    s.add_argument("queue")
    s.set_defaults(func=cmd_status)

    rs = sub.add_parser("reset", help="put scenes back to pending")
    rs.add_argument("queue")
    rs.add_argument("--failed", action="store_true", help="only failed scenes")
    rs.set_defaults(func=cmd_reset)

    c = sub.add_parser("camera", help="show the saved camera of a scene file")
    c.add_argument("scene")
    c.add_argument("--all", action="store_true")
    c.set_defaults(func=cmd_camera)

    pr = sub.add_parser("preset", help="show image size and Iray limits of a render preset")
    pr.add_argument("preset")
    pr.set_defaults(func=cmd_preset)

    cf = sub.add_parser("config", help="print or write the default config")
    cf.add_argument("--write")
    cf.set_defaults(func=cmd_config)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
