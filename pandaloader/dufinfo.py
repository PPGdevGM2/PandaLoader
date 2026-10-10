"""Read the few facts PandaLoader needs out of DAZ .duf files.

* Scene files: which camera was active when the scene was saved. The file
  stores it as ``scene.current_camera`` = ``"#<url-encoded node id>"``; the
  node with that id carries the user-visible ``label`` that
  ``Scene.findCameraByLabel`` expects inside DAZ.
* Render-settings presets: image size and the Iray limits, used for progress
  estimates (the preset is merged by the job script, not applied from here).

Scene files are gzip-compressed JSON of about 50 MB; parsing takes a few
seconds, which is negligible next to a render.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Optional
from urllib.parse import unquote


def load_duf(path) -> dict:
    """Load a .duf whether it is gzip-compressed or plain JSON."""
    p = Path(path)
    with open(p, "rb") as f:
        head = f.read(2)
    opener = gzip.open if head == b"\x1f\x8b" else open
    with opener(p, "rb") as f:  # type: ignore[operator]
        return json.load(f)


def current_camera_label(path, doc: Optional[dict] = None) -> Optional[str]:
    """Return the label of the camera that was active when the scene was saved.

    Returns None when the file records no current camera or the node is missing.
    """
    doc = doc if doc is not None else load_duf(path)
    scene = doc.get("scene", {})
    ref = scene.get("current_camera")
    if not ref:
        return None
    node_id = unquote(ref[1:] if ref.startswith("#") else ref)
    for node in scene.get("nodes", []):
        if node.get("id") == node_id:
            return node.get("label") or node.get("name")
    return None


def camera_labels(path, doc: Optional[dict] = None) -> list:
    """All camera labels in the scene (nodes whose asset url points at a camera)."""
    doc = doc if doc is not None else load_duf(path)
    scene = doc.get("scene", {})
    cams = {n.get("id") for n in doc.get("node_library", []) if n.get("type") == "camera"}
    out = []
    for node in scene.get("nodes", []):
        url = unquote(str(node.get("url", "")))
        base = url[1:] if url.startswith("#") else url
        if base in cams:
            out.append(node.get("label") or node.get("name"))
    return out


def preset_render_info(path, doc: Optional[dict] = None) -> dict:
    """Extract image size and Iray limits from a render-settings preset.

    Returns keys: image_size (w, h) or None, max_samples, max_time, converged_ratio,
    each None when absent.
    """
    doc = doc if doc is not None else load_duf(path)
    info = {"image_size": None, "max_samples": None, "max_time": None, "converged_ratio": None}
    for extra in doc.get("scene", {}).get("extra", []):
        if extra.get("type") != "studio_render_settings":
            continue
        ro = extra.get("render_options", {})
        size = ro.get("imageSize")
        if isinstance(size, list) and len(size) == 2:
            info["image_size"] = (int(size[0]), int(size[1]))
        for element in ro.get("render_elements", []):
            for ch in element.get("channels", []):
                c = ch.get("channel", {})
                cid, val = c.get("id"), c.get("current_value")
                if cid == "Max Samples":
                    info["max_samples"] = int(val)
                elif cid == "Max Time":
                    info["max_time"] = int(val)
                elif cid == "Rendering Converged Ratio":
                    info["converged_ratio"] = float(val)
    return info
