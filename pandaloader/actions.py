"""Actions that run when a scene is done ("Action when done").

The stock of actions lives in the config (Settings > Actions); a scene refers to
them by id. Each action type has a small parameter set. Results are recorded on
the scene (actionsDone), so an action runs once and a failure is visible.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Tuple

# type -> (label, parameter names)
ACTION_TYPES = {
    "move_scene": ("Move the scene file to a folder", ("folder",)),
    # fps: "" = the scene's own; height: "" = original size; formats: "ts,webm,gif";
    # pingpong: "1" appends the clip reversed; folder: "" = next to the frames
    "video": ("Make video from the animation frames (ffmpeg)", ("fps", "height", "formats", "pingpong", "folder")),
}


class ActionContext:
    """What an action may need from the running app besides the scene."""

    def __init__(self, ffmpeg_exe: str = "", denoise_keep_original: bool = False, stop=None, progress=None):
        self.ffmpeg_exe = ffmpeg_exe
        self.denoise_keep_original = denoise_keep_original
        self.stop = stop            # threading.Event: the run is being stopped
        self.progress = progress    # callable(str): short status for the worker card

SIDECARS = (".duf.png", ".tip.png")  # DAZ thumbnails next to a .duf travel with it


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def validate_actions(raw) -> list:
    """Normalise the stock as sent from Settings: list of {id, type, label, params}.
    Raises ValueError with a user-facing message."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("actions must be a list")
    out, ids = [], set()
    for a in raw:
        if not isinstance(a, dict):
            raise ValueError("bad action entry")
        typ = str(a.get("type", ""))
        if typ not in ACTION_TYPES:
            raise ValueError(f"unknown action type {typ!r}")
        params = {k: str((a.get("params") or {}).get(k, "")).strip() for k in ACTION_TYPES[typ][1]}
        if typ == "move_scene" and not params["folder"]:
            raise ValueError("'Move the scene file' needs a folder (absolute, or relative like .\\DONE = next to the scene's folder)")
        if typ == "video":
            params = _validate_video(params)
        aid = str(a.get("id") or new_id())
        if aid in ids:
            raise ValueError("duplicate action id")
        ids.add(aid)
        label = str(a.get("label", "")).strip() or describe({"type": typ, "params": params})
        out.append({"id": aid, "type": typ, "label": label, "params": params})
    return out


def _validate_video(p: dict) -> dict:
    from .video import FORMATS
    fps = p.get("fps", "").replace(",", ".")
    if fps:
        try:
            if not 0 < float(fps) <= 240:
                raise ValueError
        except ValueError:
            raise ValueError("video frame rate must be a number between 0 and 240, or empty for the scene's own")
        fps = f"{float(fps):g}"
    height = p.get("height", "")
    if height:
        if not height.isdigit() or not 16 <= int(height) <= 8640:
            raise ValueError("video height must be a whole number of pixels (e.g. 1080), or empty for the original size")
        height = str(int(height))
    formats = [f for f in FORMATS if f in {x.strip().lower() for x in p.get("formats", "").split(",")}]
    if not formats:
        raise ValueError("a video action needs at least one format (ts, webm, gif)")
    pingpong = "1" if p.get("pingpong", "") in ("1", "true", "True", "on", "yes") else ""
    return {"fps": fps, "height": height, "formats": ",".join(formats), "pingpong": pingpong,
            "folder": p.get("folder", "")}


def describe(action: dict) -> str:
    typ = action.get("type")
    p = action.get("params") or {}
    if typ == "move_scene":
        return f"Move scene to {p.get('folder', '?')}"
    if typ == "video":
        bits = [p.get("formats", "").replace(",", "+"), f"{p['fps']} fps" if p.get("fps") else "scene fps",
                f"{p['height']}p" if p.get("height") else "original size"]
        return "Video " + ", ".join(bits) + (", ping-pong" if p.get("pingpong") else "")
    return typ or "?"


def run_action(action: dict, scene, ctx: "ActionContext" = None) -> Tuple[bool, str]:
    """Run one action for a scene. Returns (ok, message). Never raises."""
    ctx = ctx or ActionContext()
    try:
        if action.get("type") == "move_scene":
            return _move_scene(scene, action["params"]["folder"])
        if action.get("type") == "video":
            return _make_video(scene, action["params"], ctx)
        return False, f"unknown action type {action.get('type')!r}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def scene_fps(scene) -> Tuple[float, str]:
    """The scene's frame rate: stored when it was added, else read from the .duf,
    else 30 (DAZ's default). Returns (fps, where it came from)."""
    stored = scene.data.get("fps")
    if stored:
        return float(stored), "scene"
    try:
        from .dufinfo import anim_info
        fps = anim_info(scene.file).get("fps")
        if fps:
            return float(fps), "scene file"
    except Exception:  # noqa: BLE001  (file moved away, unreadable)
        pass
    return 30.0, "default, scene fps unknown"


def _make_video(scene, p: dict, ctx: "ActionContext") -> Tuple[bool, str]:
    from . import video
    from .denoise import denoised_path
    if not scene.anim:
        return True, "skipped: not an animation"
    ffmpeg = video.resolve_ffmpeg(ctx.ffmpeg_exe)
    if ffmpeg is None:
        where = f"ffmpeg not found at '{ctx.ffmpeg_exe}'" if ctx.ffmpeg_exe else "no ffmpeg path set"
        return False, f"{where}: set the ffmpeg path in Settings > Actions"
    frames = scene.output_files()
    if ctx.denoise_keep_original and scene.denoise:
        denoised = [str(denoised_path(f)) for f in frames]
        if all(Path(f).is_file() for f in denoised):
            frames = denoised  # the denoised copies are the finished frames
    if p.get("fps"):
        fps, src = float(p["fps"]), "action"
    else:
        fps, src = scene_fps(scene)
    frames_dir = Path(frames[0]).parent
    out_dir = resolve_folder(p["folder"], frames_dir) if p.get("folder") else frames_dir
    stem = Path(scene.target).stem
    ok, msg = video.encode(ffmpeg, frames, scene.frame_first, scene.frame_pad, fps, p["formats"].split(","),
                           out_dir / stem, int(p["height"]) if p.get("height") else None, bool(p.get("pingpong")),
                           stop=ctx.stop, progress=ctx.progress)
    return ok, f"{msg} ({len(frames)} frames at {fps:g} fps from {src})" if ok else msg


def resolve_folder(folder: str, scene_dir: Path) -> Path:
    """An absolute folder as is. A relative one (".\\DONE", "DONE", "..\\archive") is taken
    from the parent of the scene's folder, so ".\\DONE" ends up next to the folder the
    scene came from: ...\\GPv0.65\\TODO\\Scene.duf -> ...\\GPv0.65\\DONE\\Scene.duf."""
    f = folder.strip()
    p = Path(f)
    if p.is_absolute() and p.drive:
        return p
    if f.startswith((".\\", "./")):
        f = f[2:]
    return (scene_dir.parent / f.lstrip("\\/")).resolve()


def _move_scene(scene, folder: str) -> Tuple[bool, str]:
    src = Path(scene.file)
    dest_dir = resolve_folder(folder, src.parent)
    if not src.is_file():
        return False, f"scene file not found: {src}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        if os.path.samefile(src, dest):
            return True, f"already in {dest_dir}"
        return False, f"{dest.name} already exists in {dest_dir}"
    shutil.move(str(src), str(dest))
    moved = [src.name]
    for suffix in SIDECARS:
        side = src.with_name(src.stem + suffix)
        if side.is_file():
            target = dest_dir / side.name
            if not target.exists():
                shutil.move(str(side), str(target))
                moved.append(side.name)
    scene.data["file"] = dest.as_posix()  # a later re-render finds it at the new place
    return True, f"moved {', '.join(moved)} to {dest_dir}"
