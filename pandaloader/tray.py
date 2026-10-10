"""Windows tray icon: quick access to the web UI, hover stats, run controls.

Runs pystray on the main thread (Windows wants the message loop there); the
web server and the scheduler live in their own threads. The icon is redrawn
from the logo with a progress bar and a state dot, the tooltip is refreshed
from Status.tooltip() every two seconds.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from functools import partial
from pathlib import Path
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw

from .manager import Manager, ManagerError
from .web import STATIC, WebServer

log = logging.getLogger("pandaloader.tray")

PORT_CHOICES = [6969, 8080, 8765, 8000, 5000, 3000]
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
COLORS = {
    "idle": (120, 120, 128), "rendering": (255, 191, 255), "paused": (240, 182, 74),
    "done": (127, 212, 138), "failed": (240, 106, 106),
}


def load_logo() -> Image.Image:
    p = STATIC / "logo.png"
    if p.exists():
        return Image.open(p).convert("RGBA")
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((16, 16, 240, 240), 48, fill=(0, 0, 0, 255))
    return im


def draw_icon(logo: Image.Image, state: str, frac: float, size: int = 64) -> Image.Image:
    """Logo with a progress strip at the bottom and a state dot top-right."""
    im = logo.resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    color = COLORS.get(state, COLORS["idle"])
    bar_h = max(6, size // 9)
    d.rectangle((0, size - bar_h, size, size), fill=(30, 30, 34, 255))
    if frac > 0:
        d.rectangle((0, size - bar_h, int(size * min(1.0, frac)), size), fill=color + (255,))
    r = max(5, size // 7)
    d.ellipse((size - 2 * r - 2, 2, size - 2, 2 * r + 2), fill=color + (255,), outline=(20, 20, 20, 255), width=2)
    return im


class TrayApp:
    def __init__(self, manager: Manager, server: WebServer, host: str):
        self.manager = manager
        self.server = server
        self.host = host
        self.logo = load_logo()
        self._icon_key = None
        self._stop = threading.Event()
        self.icon = pystray.Icon("PandaLoader", draw_icon(self.logo, "idle", 0.0), "PandaLoader", menu=self._menu())
        self._install_session_handlers()
        manager.on_port_change = self.rebind
        manager.status.listeners.append(self._on_event)
        purge_stale_tray_icons()  # ghosts of earlier, killed instances

    # -- Windows shutdown / logoff ----------------------------------------------------
    def _install_session_handlers(self) -> None:
        """Answer WM_QUERYENDSESSION with yes and exit cleanly on WM_ENDSESSION.

        pystray returns 0 for messages it does not know, which for
        WM_QUERYENDSESSION means "block the shutdown" and makes Windows show
        the "waiting for PandaLoader" screen.
        """
        handlers = getattr(self.icon, "_message_handlers", None)
        if handlers is None:
            return
        handlers[WM_QUERYENDSESSION] = lambda wparam, lparam: 1
        handlers[WM_ENDSESSION] = self._on_endsession

    def _on_endsession(self, wparam, lparam) -> int:
        if wparam:  # the session really ends (0 = a previous query was cancelled)
            log.info("Windows ends the session; shutting down")
            self.shutdown_fast()
        return 0

    def shutdown_fast(self) -> None:
        """Save state and exit within the few seconds Windows grants at shutdown."""
        self._stop.set()
        threading.Timer(4.0, lambda: os._exit(0)).start()  # hard stop if anything below hangs
        try:
            self.manager.emergency_stop()
        except Exception:  # noqa: BLE001
            log.exception("emergency stop failed")
        try:
            self.server.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.icon.visible = False
            self.icon.stop()
        except Exception:  # noqa: BLE001
            pass

    # -- menu ----------------------------------------------------------------
    def _menu(self) -> pystray.Menu:
        m, item = self.manager, pystray.MenuItem
        return pystray.Menu(
            item("Open PandaLoader", self.open_ui, default=True),
            item(lambda _: self._headline(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Render", self._safe(m.start), visible=lambda _: not m.running and m.queue is not None),
            item("Pause after current", self._safe(m.pause), visible=lambda _: m.running and not m.status.paused),
            item("Resume", self._safe(m.resume), visible=lambda _: m.running and m.status.paused),
            item("Stop (kill renders)", self._safe(m.stop), visible=lambda _: m.running),
            item("Shut down PC after queue", self.toggle_shutdown, checked=lambda _: m.cfg.shutdown_after_queue),
            item("ABORT PC SHUTDOWN", self._safe(m.abort_shutdown), visible=lambda _: m.shutdown_pending),
            pystray.Menu.SEPARATOR,
            item(lambda _: f"Port {m.cfg.web_port}", pystray.Menu(self._port_items)),
            pystray.Menu.SEPARATOR,
            item("Quit PandaLoader", self.quit),
        )

    def _port_items(self):
        m, item = self.manager, pystray.MenuItem
        ports = sorted(set(PORT_CHOICES) | {m.cfg.web_port})
        items = [item(str(p), partial(self._set_port, p), checked=lambda _, p=p: m.cfg.web_port == p, radio=True) for p in ports]
        items.append(item("Custom…", self._custom_port))
        return items

    def _headline(self) -> str:
        st = self.manager.status
        c = st.counts
        if not self.manager.queue:
            return "no queue open"
        name = Path(self.manager.queue.path).name if self.manager.queue.path else "unsaved queue"
        return f"{name}: {c.get('done', 0)}/{c.get('total', 0)} done" + (" (paused)" if st.paused else "")

    def _safe(self, fn: Callable) -> Callable:
        def run(icon=None, item=None):
            try:
                fn()
            except ManagerError as e:
                self.notify(str(e), "PandaLoader")
            except Exception as e:  # noqa: BLE001
                log.exception("tray action failed")
                self.notify(f"{type(e).__name__}: {e}", "PandaLoader")
        return run

    # -- actions -------------------------------------------------------------------
    def open_ui(self, icon=None, item=None) -> None:
        self.server.open_browser()

    def toggle_shutdown(self, icon=None, item=None) -> None:
        self._safe(lambda: self.manager.update_settings(shutdown_after_queue=not self.manager.cfg.shutdown_after_queue))()

    def _set_port(self, port: int, icon=None, item=None) -> None:
        self._safe(lambda: self.manager.update_settings(web_port=port))()

    def _custom_port(self, icon=None, item=None) -> None:
        port = ask_port(self.manager.cfg.web_port)
        if port:
            self._set_port(port)

    def rebind(self, port: int) -> None:
        """Move the web server to a new port. Raises ManagerError if the port is taken."""
        if port == self.server.port:
            return
        try:
            new = WebServer(self.manager, self.host, port)
        except OSError as e:
            raise ManagerError(f"port {port} not available: {e.strerror or e}")
        old = self.server
        new.start()
        self.server = new
        old.stop()  # synchronous: shutdown waits for the accept loop, then the socket closes
        self.notify(f"web UI now on {new.url}", "PandaLoader", kind="port")
        self.manager.status.log(f"web UI moved to port {port}")

    def quit(self, icon=None, item=None) -> None:
        self._stop.set()
        try:
            if self.manager.running:
                self.manager.scheduler.stop(wait=True, timeout=60)
        finally:
            try:
                self.server.stop()
            except Exception:  # noqa: BLE001
                pass
            self.icon.stop()
            purge_stale_tray_icons()

    # -- feedback --------------------------------------------------------------------
    def notify(self, message: str, title: str = "PandaLoader", kind: str = "errors") -> None:
        """Show a balloon unless the user switched this kind off in Settings."""
        if not self.manager.cfg.notifications.get(kind, True):
            return
        try:
            self.icon.notify(message[:250], title)
        except Exception:  # noqa: BLE001
            log.debug("notify failed", exc_info=True)

    def _on_event(self, ev: dict) -> None:
        msg = ev.get("msg", "")
        if ev.get("level") == "error" and msg.startswith("failed "):
            self.notify(f"[{ev.get('worker')}] {msg}", "Render failed", kind="failed")
        elif msg.startswith("queue finished"):
            self.notify(msg, "Queue finished", kind="finished")

    def _state_and_progress(self) -> tuple:
        m, st = self.manager, self.manager.status
        if m.running:
            active = [w for w in st.workers.values() if w.state != "idle"]
            fr = []
            for w in active:
                if w.render_started and w.expected_secs:
                    fr.append(min(1.0, (time.time() - w.render_started) / w.expected_secs))
                else:
                    fr.append(0.05)
            return ("paused" if st.paused else "rendering"), (sum(fr) / len(fr) if fr else 0.0)
        c = st.counts
        if c.get("failed"):
            return "failed", 0.0
        if st.finished or (c.get("total") and c.get("pending") == 0 and c.get("running") == 0):
            return "done", 0.0
        return "idle", 0.0

    def _menu_signature(self) -> tuple:
        """Everything the menu displays or hides. Rebuilding the menu while it is open
        kills the hover highlight, so it is rebuilt only when this changes."""
        m, st = self.manager, self.manager.status
        return (m.running, st.paused, m.queue is not None, self._headline(), m.cfg.shutdown_after_queue,
                m.cfg.web_port, m.shutdown_pending)

    def _ticker(self) -> None:
        menu_sig = None
        while not self._stop.is_set():
            try:
                state, frac = self._state_and_progress()
                key = (state, int(frac * 32))
                if key != self._icon_key:
                    self._icon_key = key
                    self.icon.icon = draw_icon(self.logo, state, frac)
                tooltip = self.manager.status.tooltip()
                if tooltip != self.icon.title:
                    self.icon.title = tooltip
                sig = self._menu_signature()
                if sig != menu_sig:
                    menu_sig = sig
                    self.icon.update_menu()
            except Exception:  # noqa: BLE001
                log.debug("tray tick failed", exc_info=True)
            self._stop.wait(2.0)

    # -- run -------------------------------------------------------------------------
    def run(self) -> None:
        threading.Thread(target=self._ticker, name="tray-tick", daemon=True).start()
        self.icon.run()


def purge_stale_tray_icons() -> None:
    """Make Explorer drop notification icons whose process is gone.

    Explorer only removes a dead icon when the mouse passes over it. Sweeping
    WM_MOUSEMOVE across the notification toolbars (visible and overflow)
    triggers that check for every slot. Harmless for live icons.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowExW.restype = wintypes.HWND
        WM_MOUSEMOVE = 0x0200

        def sweep(hwnd) -> None:
            if not hwnd:
                return
            rect = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return
            step = 8
            for y in range(step // 2, max(rect.bottom, 1), step):
                for x in range(step // 2, max(rect.right, 1), step):
                    user32.SendMessageTimeoutW(hwnd, WM_MOUSEMOVE, 0, (y << 16) | (x & 0xFFFF), 0x0002, 50, None)

        tray = user32.FindWindowW("Shell_TrayWnd", None)
        notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None) if tray else None
        pager = user32.FindWindowExW(notify, None, "SysPager", None) if notify else None
        sweep(user32.FindWindowExW(pager or notify, None, "ToolbarWindow32", None) if (pager or notify) else None)
        overflow = user32.FindWindowW("NotifyIconOverflowWindow", None)
        sweep(user32.FindWindowExW(overflow, None, "ToolbarWindow32", None) if overflow else None)
    except Exception:  # noqa: BLE001
        log.debug("tray purge failed", exc_info=True)


def ask_port(current: int) -> Optional[int]:
    """Tiny tkinter prompt for a port number. Returns None when cancelled."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return simpledialog.askinteger("PandaLoader", "Web UI port:", initialvalue=current, minvalue=1024, maxvalue=65535, parent=root)
    finally:
        root.destroy()
