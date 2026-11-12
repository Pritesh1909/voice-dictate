"""Recent dictations, for the History window. Dictated text can be sensitive, so where it is kept is a
setting ([history] keep):

- "off": nothing is kept.
- "session" (default): the last [history] max_items dictations in the tray process's memory only. The
  History window gets them in a temporary JSON file in %LOCALAPPDATA%\\ParakeetDictate that it deletes as
  soon as it has read it (as the correction window does). Nothing survives quitting the app.
- "disk": also written to %LOCALAPPDATA%\\ParakeetDictate\\history.jsonl (one entry per line), so the
  history survives restarts. Trimming and deleting rewrite the file atomically (no old lines left behind).

Switching from "disk" to "session" or "off" deletes history.jsonl; switching to "off" also forgets the
entries in memory. No Tk here: the window (history_ui.py) is a separate process.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

KEEP_CHOICES = ("off", "session", "disk")
MAX_ITEMS = (10, 5000)
RESULT_FILE = "history_result.json"     # what the window deleted, read by the tray when it closes
REQUEST_PREFIX = "history-"             # history-<random>.json: the entries handed to the window


@dataclass(frozen=True)
class Entry:
    id: str
    time: str          # local time, ISO 8601 to the second ("2026-11-12T14:03:05")
    text: str
    program: str       # the program it was typed into ("Code.exe"), "" if unknown
    words: int
    seconds: float     # seconds of speech recorded

    @classmethod
    def new(cls, text: str, program: str = "", seconds: float = 0.0, now: datetime | None = None) -> "Entry":
        return cls(uuid.uuid4().hex[:12], (now or datetime.now()).isoformat(timespec="seconds"), text,
                   program or "", len(text.split()), round(float(seconds), 1))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d) -> "Entry | None":
        """An entry from JSON, or None when it isn't one (a damaged line is skipped, not fatal)."""
        try:
            e = cls(str(d["id"]), str(d["time"]), str(d["text"]), str(d.get("program") or ""),
                    int(d.get("words", 0)), float(d.get("seconds", 0.0)))
            datetime.fromisoformat(e.time)
        except (KeyError, TypeError, ValueError):
            return None
        return e if e.text else None


# --- the file ----------------------------------------------------------------------------------------

def read_file(path: Path) -> list[Entry]:
    """Entries in history.jsonl, oldest first; damaged lines are skipped."""
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = Entry.from_dict(json.loads(line)) if line.strip() else None
        except ValueError:
            e = None
        if e is not None:
            out.append(e)
    return out


def write_file(path: Path, entries: Iterable[Entry]) -> None:
    """Replace history.jsonl in one step (a temporary file, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remove_from_file(path: Path, ids: Iterable[str]) -> None:
    """Drop entries from history.jsonl (the window does this as you delete, the tray again when it closes)."""
    ids = set(ids)
    if not ids or not path.exists():
        return
    write_file(path, [e for e in read_file(path) if e.id not in ids])


def delete_file(path: Path) -> None:
    for p in (path, path.with_name(path.name + ".tmp")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("history: couldn't delete %s: %s", p.name, e)


# --- what the tray keeps -----------------------------------------------------------------------------

class History:
    """The recent dictations, oldest first. Thread-safe: dictations finish on worker threads."""

    def __init__(self, file: Path, keep: str = "session", max_items: int = 200):
        self.file = file
        self.keep = "off"
        self.max_items = max_items
        self._items: list[Entry] = []
        self._lock = threading.Lock()
        self.configure(keep, max_items)

    @property
    def enabled(self) -> bool:
        return self.keep != "off"

    def __len__(self) -> int:
        return len(self._items)

    def entries(self) -> list[Entry]:
        with self._lock:
            return list(self._items)

    def configure(self, keep: str, max_items: int) -> None:
        """Apply [history] settings (at start and whenever they change)."""
        keep = keep if keep in KEEP_CHOICES else "session"
        with self._lock:
            before = self.keep
            self.keep, self.max_items = keep, max(1, int(max_items))
            if keep == "off":
                self._items = []
            elif keep == "disk" and before != "disk":
                on_disk = read_file(self.file)
                known = {e.id for e in on_disk}
                self._items = on_disk + [e for e in self._items if e.id not in known]
            trimmed = len(self._items) > self.max_items
            del self._items[:-self.max_items]
            try:
                if keep == "disk" and (before != "disk" or trimmed):
                    write_file(self.file, self._items)
                elif keep != "disk":
                    delete_file(self.file)    # not wanted on disk (any more): don't leave it there
            except OSError as e:
                log.warning("history: %s", e)

    def add(self, text: str, program: str = "", seconds: float = 0.0, now: datetime | None = None) -> Entry | None:
        if not self.enabled or not text.strip():
            return None
        e = Entry.new(text, program, seconds, now)
        with self._lock:
            self._items.append(e)
            trimmed = len(self._items) > self.max_items
            del self._items[:-self.max_items]
            if self.keep == "disk":
                try:
                    if trimmed:
                        write_file(self.file, self._items)
                    else:
                        self.file.parent.mkdir(parents=True, exist_ok=True)
                        with open(self.file, "a", encoding="utf-8", newline="\n") as f:
                            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
                except OSError as err:
                    log.warning("history: couldn't write %s: %s", self.file.name, err)
        return e

    def remove(self, ids: Iterable[str]) -> int:
        """Forget these entries (deleted in the History window). Returns how many were removed."""
        ids = set(ids)
        with self._lock:
            before = len(self._items)
            self._items = [e for e in self._items if e.id not in ids]
            removed = before - len(self._items)
            if self.keep == "disk":
                try:
                    write_file(self.file, self._items)    # also repairs a concurrent append the window missed
                except OSError as e:
                    log.warning("history: couldn't rewrite %s: %s", self.file.name, e)
        return removed


# --- the hand-over to the window and back --------------------------------------------------------------

def write_request(data_dir: Path, keep: str, entries: Iterable[Entry]) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{REQUEST_PREFIX}{uuid.uuid4().hex}.json"
    path.write_text(json.dumps({"keep": keep, "entries": [e.to_dict() for e in entries]}, ensure_ascii=False),
                    "utf-8")
    return path


def read_request(path: Path) -> tuple[str, list[Entry]]:
    """(keep, entries oldest first). The file is deleted right away: it holds dictated text."""
    try:
        data = json.loads(path.read_text("utf-8"))
        keep = data.get("keep", "session")
        entries = [e for e in (Entry.from_dict(d) for d in data.get("entries", [])) if e is not None]
    except (OSError, ValueError, AttributeError, TypeError):
        return "session", []
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return (keep if keep in KEEP_CHOICES else "session"), entries


def drop_requests(data_dir: Path) -> None:
    """Delete hand-over files a window that crashed before reading them left behind."""
    for p in data_dir.glob(f"{REQUEST_PREFIX}*.json"):
        try:
            p.unlink()
        except OSError:
            pass


def write_result(data_dir: Path, deleted: Iterable[str]) -> None:
    """Only ids, never text: what the window deleted, for the tray to forget too."""
    path = data_dir / RESULT_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"deleted": sorted(set(deleted))}), "utf-8")
    os.replace(tmp, path)


def read_result(data_dir: Path) -> list[str]:
    path = data_dir / RESULT_FILE
    try:
        ids = json.loads(path.read_text("utf-8")).get("deleted", [])
    except (OSError, ValueError, AttributeError):
        return []
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
