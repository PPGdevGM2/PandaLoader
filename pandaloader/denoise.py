"""Post-render denoising with the "Denoiser OI" command line tool
(Declan Russell's Intel Open Image Denoise wrapper, Denoiser.exe).

    Denoiser.exe -i <in> -o <out> -hdr 0 -v 1

The tool exits with 0 even on failure, so the result is judged from the
output file. The render is copied into a scratch folder in the work dir and
denoised there, so no temp files appear next to the render (a synced folder
such as Dropbox would grab and lock them). The result is then copied back over
the render, or beside it as <name>_denoised.<ext> when the original is kept.
Reading the render and writing the result back are retried for a while: a sync
client typically holds a freshly written file for a few seconds.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
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


LOCK_RETRY_S = 120.0  # how long a locked render (sync client, viewer) is waited for


def _retry_io(fn, what: str, patience_s: float = LOCK_RETRY_S) -> Tuple[bool, str, int]:
    """Run fn() until it stops raising OSError or patience runs out.
    Returns (ok, last error, retries needed)."""
    deadline, delay, tries = time.time() + patience_s, 0.5, 0
    while True:
        try:
            fn()
            return True, "", tries
        except OSError as e:
            if time.time() + delay > deadline:
                return False, f"{what}: {e}", tries
            tries += 1
            time.sleep(delay)
            delay = min(delay * 2, 8.0)


def denoise_in_place(exe: str, target: str, timeout_s: float = 600.0, hdr: bool = False,
                     keep_original: bool = False, scratch_dir: Optional[str] = None,
                     lock_patience_s: float = LOCK_RETRY_S) -> Tuple[bool, str, float]:
    """Denoise ``target``. Default: overwrite it. With ``keep_original`` the result
    is written to <name>_denoised.<ext> and the render stays untouched.
    The work happens in ``scratch_dir`` (default: a folder beside the render, the
    old behaviour). Returns (ok, message, seconds)."""
    src = Path(target)
    if not src.is_file():
        return False, f"render missing: {target}", 0.0
    final = denoised_path(target) if keep_original else src
    work = Path(scratch_dir) if scratch_dir else src.parent
    tag = uuid.uuid4().hex[:8]
    w_in = work / f"{src.stem}.{tag}.in{src.suffix}"
    w_out = work / f"{src.stem}.{tag}.out{src.suffix}"
    t0 = time.time()
    try:
        work.mkdir(parents=True, exist_ok=True)
        ok, err, waited_in = _retry_io(lambda: shutil.copyfile(src, w_in), "could not read the render", lock_patience_s)
        if not ok:
            return False, err, time.time() - t0
        cmd = [exe, "-i", str(w_in), "-o", str(w_out), "-hdr", "1" if hdr else "0", "-v", "1"]
        t1 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False,
                               cwd=str(Path(exe).parent), creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False, f"denoiser timed out after {int(timeout_s)} s", time.time() - t0
        except OSError as e:
            return False, f"denoiser could not start: {e}", time.time() - t0
        secs = time.time() - t1
        out = (r.stdout or "") + (r.stderr or "")
        if not w_out.is_file() or w_out.stat().st_size == 0:
            err = next((ln.split("|", 1)[-1].strip() for ln in out.splitlines() if "ERROR" in ln), "").strip()
            return False, f"denoiser produced no output{': ' + err if err else ''} (exit {r.returncode})", secs
        ok, err, waited_out = _retry_io(lambda: shutil.copyfile(w_out, final), "could not write the result", lock_patience_s)
        if not ok:
            return False, err + " (the render is unchanged)", secs
        waits = waited_in + waited_out
        note = f", waited for a locked file ({waits} retries)" if waits else ""
        if keep_original:
            return True, f"denoised in {secs:.1f} s -> {final.name} (original kept){note}", secs
        return True, f"denoised in {secs:.1f} s (replaced in place){note}", secs
    except OSError as e:
        return False, f"denoise failed: {e}", time.time() - t0
    finally:
        _cleanup(w_in)
        _cleanup(w_out)


def _cleanup(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


# -- installing the tool ------------------------------------------------------------
REPO_URL = "https://github.com/DeclanRussell/IntelOIDenoiser"
RELEASES_API = "https://api.github.com/repos/DeclanRussell/IntelOIDenoiser/releases/latest"
INSTALL_SUBDIR = "denoiser"


def latest_release() -> Tuple[str, str, int]:
    """(tag, zip url, size) of the newest prebuilt Windows release on GitHub."""
    import json
    import urllib.request
    req = urllib.request.Request(RELEASES_API, headers={"User-Agent": "PandaLoader", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.load(r)
    for a in rel.get("assets", []):
        if a.get("name", "").lower().endswith(".zip"):
            return rel.get("tag_name", "?"), a["browser_download_url"], int(a.get("size") or 0)
    raise RuntimeError("the latest release has no .zip asset")


def install_denoiser(work_dir: str, progress=None) -> str:
    """Download the latest prebuilt release zip into <work_dir>/denoiser and unpack it.
    The repo itself holds only source (CMake + Conan), so the exe comes from the
    Releases tab. ``progress(phase, fraction)`` is called while downloading.
    Returns the path of the unpacked Denoiser.exe."""
    import shutil
    import urllib.request
    import zipfile

    def report(phase: str, frac: float) -> None:
        if progress is not None:
            progress(phase, frac)

    report("looking up the latest release", 0.0)
    tag, url, size = latest_release()
    dest = Path(work_dir) / INSTALL_SUBDIR
    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / f"Denoiser_{tag}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "PandaLoader"})
    with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or size or 0)
        got = 0
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            report(f"downloading release {tag} ({got / 1e6:.0f} of {total / 1e6:.0f} MB)" if total else f"downloading release {tag} ({got / 1e6:.0f} MB)",
                   got / total if total else 0.0)
    if not zipfile.is_zipfile(zpath):
        zpath.unlink(missing_ok=True)
        raise RuntimeError("the download is not a zip file")
    report("unpacking", 1.0)
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        if any(n.startswith("/") or ".." in Path(n).parts for n in names):
            raise RuntimeError("zip contains unsafe paths")
        exe_members = [n for n in names if Path(n).name.lower() == "denoiser.exe"]
        if not exe_members:
            raise RuntimeError("zip contains no Denoiser.exe")
        # The zip holds LICENSE, README and a Denoiser_vX.Y folder; unpack it straight into
        # <work dir>/denoiser, replacing whatever an earlier install left there.
        for child in dest.iterdir():
            if child != zpath:
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        z.extractall(dest)
    zpath.unlink(missing_ok=True)
    exe = next(dest.rglob("Denoiser.exe"), None)
    if exe is None or not exe.is_file():
        raise RuntimeError("Denoiser.exe missing after unpacking")
    return exe.as_posix()
