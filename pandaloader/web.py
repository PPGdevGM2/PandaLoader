"""Local web UI: a static page plus a small JSON API over http.server.

Bound to the LAN by default (config.web_host), no authentication: it is meant
for a home network. Nothing here renders HTML; the page lives in web/static.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .manager import Manager, ManagerError

log = logging.getLogger("pandaloader.web")
STATIC = Path(__file__).parent / "web" / "static"


class ApiHandler(BaseHTTPRequestHandler):
    manager: Manager  # set on the server class
    server_version = "PandaLoader/0.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    # -- helpers ---------------------------------------------------------------
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _file(self, path: Path, content_type: Optional[str] = None, cache: bool = False) -> None:
        if not path.exists() or not path.is_file():
            self._error("not found", 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        # Everything no-store: a local app whose page must pick up updates immediately.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _index(self) -> None:
        """index.html with {{v}} replaced by the newest static file's mtime: a cache buster
        so browsers reload style and script after every update."""
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        try:
            v = int(max(p.stat().st_mtime for p in STATIC.iterdir() if p.is_file()))
        except (OSError, ValueError):
            v = 0
        body = html.replace("{{v}}", str(v)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise ManagerError("bad JSON body")

    # -- routing ---------------------------------------------------------------
    def do_GET(self) -> None:
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if not parts:
                return self._index()
            if parts[0] == "static" and len(parts) == 2:
                return self._file(STATIC / parts[1], cache=True)
            if parts[0] in ("favicon.ico",):
                return self._file(STATIC / "favicon.ico", cache=True)
            if parts[0] != "api":
                return self._error("not found", 404)
            m = self.manager
            if parts[1:] == ["state"]:
                return self._json(m.snapshot())
            if parts[1:] == ["browse"]:
                return self._json(m.browse(q.get("dir"), q.get("kind", "scene")))
            if len(parts) == 4 and parts[1] == "scene" and parts[3] == "thumb":
                p = m.thumbnail(int(parts[2]), int(q.get("w", 400)))
                if p is None:
                    return self._error("not rendered", 404)
                return self._file(p, "image/jpeg")
            if len(parts) == 4 and parts[1] == "scene" and parts[3] == "image":
                sc = m._scene(int(parts[2]))
                return self._file(Path(sc.target), "image/png")
            if len(parts) == 4 and parts[1] == "scene" and parts[3] == "cameras":
                return self._json(m.scene_cameras(int(parts[2])))
            return self._error("not found", 404)
        except ManagerError as e:
            self._error(str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s failed", self.path)
            self._error(f"{type(e).__name__}: {e}", 500)

    def do_POST(self) -> None:
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if parts[:1] != ["api"]:
                return self._error("not found", 404)
            body = self._body()
            m = self.manager
            route = "/".join(parts[1:])
            if route == "queue/open":
                m.open_queue(body["path"])
            elif route == "queue/new":
                m.new_queue(body["path"])
            elif route == "queue/save":
                return self._json({"path": m.save_queue(body.get("path"))})
            elif route == "run/start":
                m.start()
            elif route == "run/pause":
                m.pause()
            elif route == "run/resume":
                m.resume()
            elif route == "run/stop":
                m.stop()
            elif route == "scenes/add":
                n = m.add_scenes(body.get("files", []), body.get("targetDir"), body.get("postLoaders"), body.get("atTop", False))
                return self._json({"added": n})
            elif route == "scenes/reset":
                return self._json({"reset": m.reset_all(body.get("failedOnly", False))})
            elif len(parts) == 4 and parts[1] == "scene":
                i, action = int(parts[2]), parts[3]
                if action == "remove":
                    m.remove_scene(i)
                elif action == "move":
                    return self._json({"index": m.move_scene(i, int(body.get("delta", 0)))})
                elif action == "reset":
                    m.reset_scene(i)
                elif action == "done":
                    m.mark_done(i)
                elif action == "edit":
                    m.edit_scene(i, body.get("target"), body.get("postLoaders"), body.get("camera"))
                else:
                    return self._error("not found", 404)
            elif route == "settings":
                m.update_settings(
                    workers=body.get("workers"), shutdown_after_queue=body.get("shutdownAfterQueue"),
                    job_timeout_s=body.get("jobTimeoutS"), stall_timeout_s=body.get("stallTimeoutS"),
                    max_attempts=body.get("maxAttempts"), image_size=body.get("imageSize"), daz_exe=body.get("dazExe"),
                    web_port=body.get("webPort"), mode=body.get("mode"), notifications=body.get("notifications"),
                )
            else:
                return self._error("not found", 404)
            self._json({"ok": True})
        except ManagerError as e:
            self._error(str(e))
        except (KeyError, ValueError) as e:
            self._error(f"bad request: {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s failed", self.path)
            self._error(f"{type(e).__name__}: {e}", 500)


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second socket bind a port that is still in
    # use, silently stealing or losing connections. Binding must fail instead.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


class WebServer:
    def __init__(self, manager: Manager, host: str, port: int):
        self.manager = manager
        self.host, self.port = host, port
        handler = type("BoundHandler", (ApiHandler,), {"manager": manager})
        self.httpd = _Server((host, port), handler)
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)
        self._thread.start()
        log.info("web UI on %s", self.url)

    def stop(self) -> None:
        """Stop serving and close the listening socket so the port is really free."""
        self.httpd.shutdown()
        self.httpd.server_close()

    def open_browser(self) -> None:
        webbrowser.open(self.url)
