"""Saved recordings: what the user said, kept for a few days so a second model can listen again later.

Each dictation is stored as two files named by its id (new_id: YYYYMMDD-HHMMSS-ffffff, local time):

- <id>.wav   16 kHz mono 16-bit audio, exactly what Parakeet heard
- <id>.json  the Record: what Parakeet wrote, what was typed after fixes, which app, model and language

Both files are written atomically (name.tmp, then os.replace) and the .json goes last, so a .json always
has a complete .wav next to it. A crash between the two leaves a .wav alone, which list_records ignores
and prune clears.

Only files named like a new_id are ever pruned or deleted here, so anything else a user (or another part
of the app) puts in the folder is left alone. Damaged files never raise: list_records skips them and
load_audio returns None.
"""
from __future__ import annotations

import json
import logging
import os
import re
import wave
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .audio import SR

log = logging.getLogger(__name__)

_ID_FORMAT = "%Y%m%d-%H%M%S-%f"
# <id>.wav / <id>.json, plus their .tmp forms left behind by a crash mid-save
_FILE = re.compile(r"^(\d{8}-\d{6}-\d{6})\.(?:wav|json)(?:\.tmp)?$")
_MB = 1024 * 1024


@dataclass
class Record:
    id: str
    time: str            # when it was dictated, ISO 8601 local time
    exe: str             # the app that had focus
    raw: str             # what the speech model wrote
    phrases: list[str]   # the same, split per spoken phrase
    typed: str           # what was finally typed (after dictionary, fixes and commands)
    model: str
    language: str


def new_id(now: datetime) -> str:
    return now.strftime(_ID_FORMAT)


def _write_atomically(path: Path, write) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _write_wav(path: Path, audio16k: np.ndarray) -> None:
    pcm = audio16k
    if pcm.dtype != np.int16:   # float in [-1, 1]; anything louder is clipped, not wrapped
        pcm = np.clip(np.rint(pcm * 32768.0), -32768, 32767)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.astype("<i2").tobytes())


def save(dir: Path, audio16k: np.ndarray, rec: Record) -> None:
    """Write <id>.wav, then <id>.json (last, so it never points at a missing or partial wav)."""
    dir.mkdir(parents=True, exist_ok=True)
    _write_atomically(dir / f"{rec.id}.wav", lambda tmp: _write_wav(tmp, audio16k))
    _write_atomically(dir / f"{rec.id}.json",
                      lambda tmp: tmp.write_text(json.dumps(asdict(rec), ensure_ascii=False, indent=1), "utf-8"))


def _read_record(path: Path) -> Record | None:
    """The Record in a .json, or None if it is damaged, incomplete or named for a different id."""
    try:
        data = json.loads(path.read_text("utf-8"))
        rec = Record(**{f.name: data[f.name] for f in fields(Record)})
    except (OSError, ValueError, KeyError, TypeError):   # ValueError covers bad JSON and bad UTF-8
        return None
    strings = (rec.id, rec.time, rec.exe, rec.raw, rec.typed, rec.model, rec.language)
    ok = (all(isinstance(s, str) for s in strings) and isinstance(rec.phrases, list)
          and all(isinstance(p, str) for p in rec.phrases) and rec.id == path.stem)
    return rec if ok else None


def list_records(dir: Path) -> list[Record]:
    """Every complete recording, oldest first. A .json without its .wav, or with bad contents, is skipped."""
    out = []
    for path in sorted(dir.glob("*.json")) if dir.is_dir() else []:
        rec = _read_record(path)
        if rec is not None and path.with_suffix(".wav").is_file():
            out.append(rec)
    return out


def load_audio(dir: Path, id: str) -> np.ndarray | None:
    """The recording as float32 in [-1, 1] at 16 kHz, or None if the wav is missing, truncated or foreign."""
    try:
        with wave.open(str(dir / f"{id}.wav")) as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SR):
                return None
            want = w.getnframes() * 2
            raw = w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    if len(raw) != want:   # the header promises more audio than the file holds
        return None
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _groups(dir: Path) -> dict[str, list[Path]]:
    """Recording files in the folder, grouped by id (a recording is its .wav and .json, plus any .tmp)."""
    groups: dict[str, list[Path]] = {}
    for path in dir.iterdir() if dir.is_dir() else []:
        m = _FILE.match(path.name)
        if m and path.is_file():
            groups.setdefault(m.group(1), []).append(path)
    return groups


def _delete(files: list[Path]) -> bool:
    """Remove one recording's files, .json first so it is never listed with its audio gone."""
    ok = True
    for path in sorted(files, key=lambda p: p.suffix != ".json"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", path, exc_info=True)
            ok = False
    return ok


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def prune(dir: Path, keep_days: int, max_mb: int, now: datetime) -> int:
    """Delete recordings older than keep_days, then the oldest ones until the folder fits in max_mb.

    Returns how many recordings were deleted. Age comes from the id, not file times."""
    now = now.replace(tzinfo=None)
    groups = _groups(dir)
    deleted = 0
    for id in sorted(groups):   # ids sort by time, so this is oldest first
        try:
            age = now - datetime.strptime(id, _ID_FORMAT)
        except ValueError:      # looks like an id but is not a real date
            continue
        if age > timedelta(days=keep_days) and _delete(groups[id]):
            del groups[id]
            deleted += 1
    sizes = {id: sum(_size(p) for p in files) for id, files in groups.items()}
    total = sum(sizes.values())
    for id in sorted(groups):
        if total <= max_mb * _MB:
            break
        if _delete(groups[id]):
            total -= sizes[id]
            deleted += 1
    return deleted


def delete_all(dir: Path) -> int:
    """Delete every recording in the folder (nothing else). Returns how many."""
    return sum(1 for files in _groups(dir).values() if _delete(files))
