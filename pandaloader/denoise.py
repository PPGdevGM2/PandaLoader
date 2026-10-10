"""Post-render denoising with the "Denoiser OI" command line tool
(Declan Russell's Intel Open Image Denoise wrapper, Denoiser.exe).

    Denoiser.exe -i <in> -o <out> -hdr 0 -v 1

The tool exits with 0 even on failure, so the result is judged from the
output file. The render is denoised in place: the tool writes a temp file
next to the target, which then replaces the target. Nothing is copied
anywhere else.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def validate_denoiser(path: Optional[str]) -> Tuple[bool, str]:
    """(valid, reason). Valid means an existing Denoiser executable file."""
    if not path:
        return False, "no denoiser path set"
    p = Path(path)
    if p.is_dir():
        for cand in (p / "Denoiser.exe", p / "Denoiser_v1.6" / "Denoiser.exe"):
            if cand.is_file():
                return True, str(cand)
        return False, "folder contains no Denoiser.exe"
    if not p.is_file():
        return False, "file not found"
    if p.suffix.lower() != ".exe":
        return False, "not an .exe"
    return True, str(p)


def resolve_denoiser(path: Optional[str]) -> Optional[str]:
    ok, info = validate_denoiser(path)
    return info if ok else None


DENOISED_SUFFIX = "_denoised"


def denoised_path(target: str) -> Path:
    """Where the denoised copy goes when the original is kept: <name>_denoised.<ext>."""
    p = Path(target)
    return p.with_name(f"{p.stem}{DENOISED_SUFFIX}{p.suffix}")


def denoise_in_place(exe: str, target: str, timeout_s: float = 600.0, hdr: bool = False,
                     keep_original: bool = False) -> Tuple[bool, str, float]:
    """Denoise ``target``. Default: overwrite it (via a temp file). With
    ``keep_original`` the result is written to <name>_denoised.<ext> and the
    render stays untouched. Returns (ok, message, seconds)."""
    src = Path(target)
    if not src.is_file():
        return False, f"render missing: {target}", 0.0
    tmp = denoised_path(target) if keep_original else src.with_name(f"{src.stem}.denoise-tmp{src.suffix}")
    if tmp.exists():
        tmp.unlink()
    cmd = [exe, "-i", str(src), "-o", str(tmp), "-hdr", "1" if hdr else "0", "-v", "1"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False,
                           cwd=str(Path(exe).parent), creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        _cleanup(tmp)
        return False, f"denoiser timed out after {int(timeout_s)} s", time.time() - t0
    except OSError as e:
        return False, f"denoiser could not start: {e}", time.time() - t0
    secs = time.time() - t0
    out = (r.stdout or "") + (r.stderr or "")
    if not tmp.is_file() or tmp.stat().st_size == 0:
        _cleanup(tmp)
        err = next((ln.split("|", 1)[-1].strip() for ln in out.splitlines() if "ERROR" in ln), "").strip()
        return False, f"denoiser produced no output{': ' + err if err else ''} (exit {r.returncode})", secs
    if keep_original:
        return True, f"denoised in {secs:.1f} s -> {tmp.name} (original kept)", secs
    try:
        os.replace(tmp, src)
    except OSError as e:
        _cleanup(tmp)
        return False, f"could not replace the render: {e}", secs
    return True, f"denoised in {secs:.1f} s (replaced in place)", secs


def _cleanup(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass
