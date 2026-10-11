"""Animation frames to video with ffmpeg ("Make video" action).

Every output is encoded straight from the rendered PNG frames, one ffmpeg run
per format, so no format is a re-encode of another:

    ts    MPEG-2 at 60 Mbit/s (the master copy)
    webm  VP9, constant quality CRF 18
    gif   palette generated from the clip itself

Frames are scaled to a fixed height (width follows, kept even) unless the
height is left empty. Ping-pong appends the clip reversed, for a seamless loop.

ts and webm get yuv420p explicitly: VP9 fed with RGB frames would otherwise
pick an RGB profile that many players and browsers cannot play.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

FORMATS = ("ts", "webm", "gif")
_MUXER = {"ts": "mpegts", "webm": "webm", "gif": "gif"}
_CODEC = {
    "ts": ["-c:v", "mpeg2video", "-b:v", "60000k", "-pix_fmt", "yuv420p"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "18", "-b:v", "0", "-pix_fmt", "yuv420p"],
    "gif": [],
}
CREATE_NO_WINDOW = 0x08000000


def resolve_ffmpeg(configured: str = "") -> Optional[str]:
    """The ffmpeg.exe set in Settings, if it exists. Nothing else is searched: no PATH,
    no usual install spots. None when nothing (valid) is set."""
    c = (configured or "").strip().strip('"')
    if not c:
        return None
    p = Path(c)
    return str(p) if p.is_file() and p.suffix.lower() == ".exe" else None


def frame_pattern(first_frame_file: str, frame: int, pad: int) -> str:
    """ffmpeg image2 pattern for a frame file name: Scene_05.png -> Scene_%02d.png.
    The frame number is the last run of digits matching `frame` padded to `pad`."""
    p = Path(first_frame_file)
    num = f"{int(frame):0{pad}d}"
    name = p.name.replace("%", "%%")
    i = name.rfind(num)
    if i < 0:
        raise ValueError(f"frame number {num} not found in {p.name}")
    return str(p.with_name(name[:i] + f"%0{pad}d" + name[i + len(num):]))


def build_command(ffmpeg: str, pattern: str, first: int, fps: float, fmt: str, out: str,
                  height: Optional[int], pingpong: bool) -> List[str]:
    graph = "[0:v]" + (f"scale=-2:{int(height)}" if height else "null")
    if pingpong:
        graph += ",split[f][b];[b]reverse[r];[f][r]concat=n=2:v=1:a=0"
    graph += ",split[a][b];[b]palettegen[p];[a][p]paletteuse[v]" if fmt == "gif" else ",format=yuv420p[v]"
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "image2", "-framerate", f"{fps:g}", "-start_number", str(int(first)), "-i", pattern,
            "-filter_complex", graph, "-map", "[v]", *_CODEC[fmt], "-f", _MUXER[fmt], out]


def encode(ffmpeg: str, frames: List[str], first: int, pad: int, fps: float, formats: Iterable[str],
           out_stem: Path, height: Optional[int] = None, pingpong: bool = False,
           stop: Optional[threading.Event] = None, progress: Optional[Callable[[str], None]] = None,
           timeout_s: float = 3600.0) -> Tuple[bool, str]:
    """Encode the frames into out_stem.<fmt> for each format. Each file is written as
    .part and renamed when complete, so a half-written video never looks finished.
    Returns (ok, message). Never raises."""
    missing = [f for f in frames if not Path(f).is_file()]
    if missing:
        return False, f"{len(missing)} frame file(s) missing, e.g. {Path(missing[0]).name}"
    try:
        pattern = frame_pattern(frames[0], first, pad)
    except ValueError as e:
        return False, str(e)
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    made, fmts = [], [f for f in FORMATS if f in set(formats)]
    for n, fmt in enumerate(fmts, 1):
        if stop is not None and stop.is_set():
            return False, "stopped" + (f" after {', '.join(made)}" if made else "")
        out = out_stem.with_name(out_stem.name + "." + fmt)
        part = out.with_name(out.name + ".part")
        if progress:
            progress(f"encoding {fmt} ({n}/{len(fmts)})")
        cmd = build_command(ffmpeg, pattern, first, fps, fmt, str(part), height, pingpong)
        ok, err = _run(cmd, stop, timeout_s)
        if not ok:
            try:
                part.unlink()
            except OSError:
                pass
            if stop is not None and stop.is_set():  # "stopped" first: the caller leaves the action pending
                return False, f"stopped during {fmt}" + (f" ({', '.join(made)} done)" if made else "")
            return False, f"{fmt}: {err}" + (f" ({', '.join(made)} done)" if made else "")
        os.replace(part, out)
        made.append(out.name)
    return True, f"{', '.join(made)} in {out_stem.parent}"


def _run(cmd: List[str], stop: Optional[threading.Event], timeout_s: float) -> Tuple[bool, str]:
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as e:
        return False, f"ffmpeg could not start: {e}"
    err_chunks: list = []
    reader = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()
    started = time.time()
    while proc.poll() is None:
        if (stop is not None and stop.is_set()) or time.time() - started > timeout_s:
            proc.kill()
            proc.wait()
            return False, "stopped" if stop is not None and stop.is_set() else f"timed out after {int(timeout_s)} s"
        time.sleep(0.3)
    reader.join(5)
    err = b"".join(c for c in err_chunks if c).decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        last = err.splitlines()[-1] if err else f"exit code {proc.returncode}"
        return False, f"ffmpeg failed: {last}"
    return True, ""
