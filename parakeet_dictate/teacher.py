"""The "teacher": Whisper Large v3 Turbo, a bigger speech model that re-listens to saved dictations in the
background to find words the everyday model gets wrong.

This module only gets the model file. It lives in its own folder (data_dir/teacher/whisper-large-v3-turbo/),
never in the folder dictation models come from, so it never shows up in the Model menu. If Orca already
downloaded the same build, that copy is used instead (after its SHA-256 has been checked once).

The tray process never loads the speech engine (transcribe_cpp), so whether this PC has a discrete graphics
card is asked of a short-lived child process: the app itself run with --list-devices.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
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


def has_discrete_gpu(exe_cmd: list[str], timeout: float = LIST_DEVICES_TIMEOUT) -> bool:
    """True if the speech engine sees a discrete graphics card. Runs `exe_cmd --list-devices` (the app's own
    command line, see __main__.list_devices), which prints [[device_type, name], ...] as JSON. Any failure
    (missing program, timeout, bad output) counts as no card: learning then simply waits."""
    try:
        done = subprocess.run([*exe_cmd, "--list-devices"], capture_output=True, text=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if done.returncode != 0:
            return False
        return any(isinstance(d, list) and d and d[0] == "gpu" for d in json.loads(done.stdout))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as e:
        log.info("could not list graphics devices: %s", e)
        return False
