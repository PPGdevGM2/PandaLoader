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
}

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
        aid = str(a.get("id") or new_id())
        if aid in ids:
            raise ValueError("duplicate action id")
        ids.add(aid)
        label = str(a.get("label", "")).strip() or describe({"type": typ, "params": params})
        out.append({"id": aid, "type": typ, "label": label, "params": params})
    return out


def describe(action: dict) -> str:
    typ = action.get("type")
    p = action.get("params") or {}
    if typ == "move_scene":
        return f"Move scene to {p.get('folder', '?')}"
    return typ or "?"


def run_action(action: dict, scene) -> Tuple[bool, str]:
    """Run one action for a scene. Returns (ok, message). Never raises."""
    try:
        if action.get("type") == "move_scene":
            return _move_scene(scene, action["params"]["folder"])
        return False, f"unknown action type {action.get('type')!r}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


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
