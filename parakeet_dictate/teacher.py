"""The "teacher": Whisper Large v3 Turbo, a bigger speech model that re-listens to saved dictations in the
background to find words the everyday model gets wrong.

This module only gets the model file. It lives in its own folder (data_dir/teacher/whisper-large-v3-turbo/),
never in the folder dictation models come from, so it never shows up in the Model menu. If Orca already
downloaded the same build, that copy is used instead (after its SHA-256 has been checked once).

The tray process never loads the speech engine (transcribe_cpp), so whether this PC has a discrete graphics
card is asked of a short-lived child process: the app itself run with --list-devices <file>.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from . import models

log = logging.getLogger(__name__)

KEY = "whisper-large-v3-turbo"          # catalog key in models.KNOWN, and the name of the folder it lives in
VERIFIED_FILE = "teacher_verified.json"  # in the learning folder: Orca copies whose SHA-256 was checked once
LIST_DEVICES_TIMEOUT = 30.0


def _file_size(p: Path) -> int | None:
    try:
        return p.stat().st_size if p.is_file() else None
    except OSError:
        return None


def find(teacher_dir: Path, orca_dir: Path, *, learning_dir: Path | None = None) -> Path | None:
    """The Whisper file ready to use, or None. Our own download (teacher_dir/<KEY>/<file>) first, else Orca's
    copy (orca_dir/<KEY>/<file>) when its size matches and its SHA-256 has been verified. A verified copy is
    remembered in learning_dir/teacher_verified.json (default: "learning" next to teacher_dir), together with
    its size and file time, so the 886 MB file is hashed once and again only if it changes."""
    _, _, name, size, sha = models.KNOWN[KEY]
    own = teacher_dir / KEY / name
    if _file_size(own) == size:            # only renamed into place after its SHA-256 was checked
        return own
    theirs = orca_dir / KEY / name
    if _file_size(theirs) != size:
        return None
    cache_file = (learning_dir or teacher_dir.parent / "learning") / VERIFIED_FILE
    try:
        stamp = [size, theirs.stat().st_mtime_ns]
        verified = _read_verified(cache_file)
        if verified.get(str(theirs)) == stamp:
            return theirs
        if models._sha256(theirs) != sha:
            log.warning("Orca's copy of %s does not match the expected checksum; not using it", name)
            return None
    except OSError as e:
        log.warning("could not check Orca's copy of %s: %s", name, e)
        return None
    verified[str(theirs)] = stamp
    _write_verified(cache_file, verified)
    return theirs


def _read_verified(path: Path) -> dict:
    """{path: [size, mtime_ns]}; empty when the file is missing or damaged."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_verified(path: Path, verified: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(verified, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:               # worst case the file is hashed again next time
        log.warning("could not save %s: %s", path, e)


def ensure(teacher_dir: Path, orca_dir: Path, progress: models.Progress = lambda done, total: None,
           *, learning_dir: Path | None = None, base_url: str = models.HF_URL) -> Path:
    """The Whisper file, downloaded into teacher_dir if neither our own nor Orca's copy is there. An
    interrupted download carries on where it stopped. Raises OSError when the download fails."""
    got = find(teacher_dir, orca_dir, learning_dir=learning_dir)
    if got:
        size = models.KNOWN[KEY][3]
        progress(size, size)
        return got
    return models.download(teacher_dir, progress, base_url=base_url, key=KEY, resume=True).path


def _read_error(path: Path) -> str:
    """The text --list-devices wrote to its .err file, which is then removed; "" when there is none."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        path.unlink()
    except OSError:
        return ""
    return text


def has_discrete_gpu(exe_cmd: list[str], timeout: float = LIST_DEVICES_TIMEOUT) -> bool:
    """True if the speech engine sees a discrete graphics card. Runs `exe_cmd --list-devices <out.json>` (the
    app's own command line, see __main__.list_devices; a file because the windowed .exe has no stdout) and
    reads [[device_type, name], ...] from that file once the child has exited. Any failure (missing program,
    timeout, non-zero exit, no or bad file) counts as no card: learning then simply waits. The child's error
    is logged, from <file>.err (the windowed .exe has no stderr) or else the tail of its stderr."""
    try:
        with tempfile.TemporaryDirectory(prefix="parakeet-devices-", ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "devices.json"
            done = subprocess.run([*exe_cmd, "--list-devices", str(out)], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True, errors="replace", timeout=timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                devices = json.loads(out.read_text(encoding="utf-8")) if done.returncode == 0 else None
            except (OSError, ValueError):
                devices = None
            if not isinstance(devices, list):
                log.info("--list-devices gave no usable answer (exit code %s): %s", done.returncode,
                         (_read_error(out.with_name(out.name + ".err")) or done.stderr or "").strip()[-500:])
                return False
            return any(isinstance(d, list) and d and d[0] == "gpu" for d in devices)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.info("could not list graphics devices: %s", e)
        return False
