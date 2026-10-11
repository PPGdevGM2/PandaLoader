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
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, dufinfo
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
    handlers = []
    for folder in (Path(cfg.work_dir), Path(os.environ.get("TEMP") or ".")):
        try:  # the work dir first; if it cannot be written, the temp folder rather than silence
            folder.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(folder / "pandaloader.log", encoding="utf-8"))
            break
        except OSError:
            continue
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


class AlreadyRunning(Exception):
    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


def _running_instance(port: int):
    """URL of a PandaLoader already serving on this port, or None."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            data = _json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict) and "workers" in data and "queue" in data:
            return f"http://localhost:{port}/"
    except Exception:  # noqa: BLE001  (connection refused, not JSON, not ours)
        pass
    return None


def _message_box(text: str, title: str = "PandaLoader", icon: int = 0x10, background: bool = False) -> None:
    """Modal Windows message box (0x10 error, 0x30 warning). background=True shows it
    from a thread so the caller is not blocked until the user clicks OK."""
    if os.name == "nt":
        try:
            import ctypes

            def show() -> None:
                ctypes.windll.user32.MessageBoxW(None, text, title, icon | 0x1000)  # MB_SYSTEMMODAL: on top

            if background:
                threading.Thread(target=show, name="msgbox", daemon=True).start()
            else:
                show()
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"{title}: {text}", file=sys.stderr)


def _check_nvidia_smi(manager) -> None:
    """Mandatory warning when nvidia-smi is missing; not covered by the notification switches."""
    from .gpumon import NVIDIA_SMI_MISSING_TEXT, find_nvidia_smi
    if find_nvidia_smi():
        return
    manager.status.log("nvidia-smi not found: GPU gauges unavailable", level="warning")
    logging.getLogger("pandaloader").warning("nvidia-smi not found")
    _message_box(NVIDIA_SMI_MISSING_TEXT, "PandaLoader: nvidia-smi missing", icon=0x30, background=True)


def _start_app(args):
    """Shared setup for serve and tray: config, manager, web server.

    Raises AlreadyRunning when another PandaLoader already owns the port.
    """
    from .manager import Manager
    from .web import WebServer
    cfg_path = Path(args.config) if args.config else Path(Config().work_dir) / "config.json"
    cfg_exists = cfg_path.is_file()
    if cfg_exists:
        try:
            cfg = Config.load(cfg_path)
        except (OSError, ValueError) as e:
            # Never run on defaults while a config exists: that would look like "everything reset".
            raise RuntimeError(f"Config could not be read:\n{cfg_path}\n\n{e}\n\n"
                               f"Fix or delete the file, then start PandaLoader again.")
    else:
        cfg = Config()
    if getattr(args, "port", None):
        cfg.web_port = int(args.port)
    if getattr(args, "host", None):
        cfg.web_host = args.host
    _setup_logging(cfg, args.verbose)
    log = logging.getLogger("pandaloader")
    log.info("PandaLoader %s starting; config %s (%s): workers %s, denoiser %r, work dir %s",
             __version__, cfg_path, "loaded" if cfg_exists else "not found, defaults",
             [(w.name, w.gpu_text, w.enabled) for w in cfg.workers], cfg.denoiser_exe, cfg.work_dir)
    existing = _running_instance(cfg.web_port)
    if existing:
        raise AlreadyRunning(existing)
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:  # fail early, before touching the queue
            probe.bind((cfg.web_host, cfg.web_port))
    except OSError as e:
        # A PandaLoader that is still unpacking/starting owns the port but does not answer yet
        # (double-clicked twice on a slow first launch): give it a few seconds before blaming others.
        for _ in range(16):
            time.sleep(0.5)
            existing = _running_instance(cfg.web_port)
            if existing:
                raise AlreadyRunning(existing)
        raise RuntimeError(f"Port {cfg.web_port} is in use by another program ({e.strerror or e}).\n"
                           f"Change the port with: PandaLoader.exe --port <number>")
    first_run = not cfg_exists
    manager = Manager(cfg, cfg_path)
    manager.first_run = first_run
    if first_run:
        manager.save_config()
        manager.status.log("first start: default config written; see Settings > Setup for the checklist")
    if getattr(args, "queue", None):
        manager.open_queue(args.queue)
    server = WebServer(manager, cfg.web_host, cfg.web_port)
    server.start()
    return manager, server


def cmd_serve(args) -> int:
    from .manager import ManagerError
    from .web import WebServer
    try:
        manager, server = _start_app(args)
    except AlreadyRunning as e:
        print(f"PandaLoader is already running at {e.url}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    holder = {"server": server}

    def rebind(port: int) -> None:
        try:
            new = WebServer(manager, manager.cfg.web_host, port)
        except OSError as e:
            raise ManagerError(f"port {port} not available: {e.strerror or e}")
        old = holder["server"]
        new.start()
        holder["server"] = new
        old.stop()
        print(f"web UI moved to {new.url}", flush=True)

    manager.on_port_change = rebind
    _check_nvidia_smi(manager)
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
    holder["server"].stop()
    return 0


def _escape_container(args) -> bool:
    """True when this process must not go on: it was started inside another app's
    container (its AppData writes would land in that app's private folder) and has
    either been restarted outside it or told the user how to start it properly."""
    from . import sandbox
    work = Path(args.config).parent if args.config else Path(Config().work_dir)
    exe_dir = Path(sys.executable).parent
    redirected = sandbox.redirected_folder(work)
    if not redirected:
        try:
            (exe_dir / ".pandaloader-relaunch").unlink()
        except OSError:
            pass
        return False
    if sandbox.relaunch_outside(exe_dir):
        return True
    owner = sandbox.container_name(redirected)
    _message_box(f"PandaLoader was started from inside another app{f' ({owner})' if owner else ''}.\n\n"
                 f"Its settings and queue state would be saved in that app's private folder:\n{redirected}\n"
                 f"instead of:\n{work}\n\n"
                 f"and would look reset the next time PandaLoader is started normally.\n\n"
                 f"Start PandaLoader.exe from Explorer or the Start Menu instead.")
    return True


def cmd_tray(args) -> int:
    import webbrowser
    if _escape_container(args):
        return 0
    try:
        from .tray import TrayApp
        manager, server = _start_app(args)
    except AlreadyRunning as e:
        webbrowser.open(e.url)  # second launch: just show the page of the running instance
        return 0
    except Exception as e:  # noqa: BLE001
        logging.getLogger("pandaloader").exception("startup failed")
        _message_box(f"PandaLoader could not start:\n\n{e}")
        return 1
    app = TrayApp(manager, server, manager.cfg.web_host)
    _check_nvidia_smi(manager)
    if not args.no_browser:  # every fresh start shows the page (a second launch shows it too, see above)
        server.open_browser()
    try:
        app.run()  # blocks on the main thread until Quit
    except Exception as e:  # noqa: BLE001
        logging.getLogger("pandaloader").exception("tray crashed")
        _message_box(f"PandaLoader stopped with an error:\n\n{e}")
        return 1
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

    tr = sub.add_parser("tray", help="run with a tray icon and the web UI (use pythonw for no console)")
    tr.add_argument("queue", nargs="?")
    tr.add_argument("--config")
    tr.add_argument("--port", type=int)
    tr.add_argument("--host")
    tr.add_argument("--open", action="store_true", help="(default now) open the browser at start")
    tr.add_argument("--no-browser", action="store_true", help="do not open the browser at start")
    tr.add_argument("-v", "--verbose", action="store_true")
    tr.set_defaults(func=cmd_tray)

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
