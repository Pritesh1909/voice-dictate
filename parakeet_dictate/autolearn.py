"""What background learning has found: fixes waiting for a second sighting, active fixes, the never-learn list.

The checker (tray process) and the Settings window (another process) both use a Store on the same
folder, so nothing is cached: every write happens inside lock(), after re-reading state.json from disk,
and every read goes to the disk too. A fix is activated once it was seen in two different recordings. A
fix the user undid is on the never-learn list and is never counted again.

Files:
- learning_dir/state.json    pending candidates, active fixes, the never-learn list, Claude calls per day, status
- learning_dir/state.lock    the cross-process lock (an exclusive msvcrt lock on its first byte)
- learning_dir/checked.jsonl one row per checked recording (each row has an "id"), the last 5000 kept
- learned_auto_file          the active fixes in the shape of learned.toml, written from state.json after
                             every change; the [meta."<entry key>"] tables and the [never] table (a copy
                             of the never-learn list) are ignored by learn.load

The two live in folders with different lifetimes: state.json in %LOCALAPPDATA% (the uninstaller deletes
it), learned_auto.toml in %APPDATA% (kept). So learned_auto.toml is also the backup: its never-learn list
is always merged back into what is read, and when state.json is missing, damaged or has no active fix
while the toml still has some, the active fixes are rebuilt from it (without the never-learn ones). The
toml is re-synced from state.json by sync() (the checker calls it at the start of every run) and by
every write of the fixes, so an undo whose toml write failed stops being applied.

Damaged files never raise: state.json (or a damaged part of it) starts from empty with a warning, and
the next write replaces it. A file that cannot be read at all (an I/O error such as PermissionError) is
not damage: the read-only methods show it as empty, but a write raises OSError and leaves the files
alone, so a hiccup can never wipe the never-learn list. A lock that stays taken for LOCK_WAIT_S seconds
raises TimeoutError.
"""
from __future__ import annotations

import json
import logging
import msvcrt
import os
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from . import learn
from .dictionary import normalize_terms, word_key
from .learn import Proposal, _q

log = logging.getLogger(__name__)

Entry = tuple[str, str]   # (term, heard_as), the order of learn.Proposal.entry

LOCK_WAIT_S = 5.0         # how long lock() waits for the other process
SIGHTINGS_TO_ACTIVATE = 2   # different recordings
CHECKED_KEEP = 5000       # lines kept in checked.jsonl
RECS_KEPT = 20            # recording ids remembered per active fix, to count one recording once

HEADER = ("# Fixes learned automatically by Parakeet Dictate (background learning).\n"
          "# The app rewrites this file whenever a fix is learned or undone. Undo fixes in Settings;\n"
          "# the [never] and [meta] tables are bookkeeping and are ignored when the dictionary is loaded.\n")

_T = TypeVar("_T")


def load_active(path: Path) -> list[Entry]:
    """The active fixes for the clean-up, read from learned_auto.toml (learn.load: the [meta] tables are
    ignored, and a missing or damaged file is [] and never an error)."""
    return learn.load(path)


def entry_key(e: Entry) -> str:
    term, heard_as = e
    return f"{heard_as} -> {term}"


@dataclass
class AutoFix:
    term: str
    heard_as: str
    first_learned: str
    seen: int
    example: str


# --- state.json ------------------------------------------------------------------------------------

def _empty() -> dict:
    return {"pending": {}, "active": {}, "never": [], "claude": {"day": "", "count": 0}, "status": ""}


def _text(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _count(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _recs(v: object) -> list[str]:
    return [r for r in v if isinstance(r, str)] if isinstance(v, list) else []


def _clean(raw: object) -> dict:
    """The parts of a parsed state.json that make sense; a damaged part is left out, never an error."""
    st = _empty()
    if not isinstance(raw, dict):
        return st
    for section, active in (("pending", False), ("active", True)):
        items = raw.get(section)
        for v in items.values() if isinstance(items, dict) else ():
            if not isinstance(v, dict):
                continue
            term, heard_as = _text(v.get("term")), _text(v.get("heard_as"))
            if not term or heard_as is None:
                continue
            item = {"term": term, "heard_as": heard_as, "recs": _recs(v.get("recs")),
                    "example": _text(v.get("example")) or ""}
            if active:
                item["first_learned"] = _text(v.get("first_learned")) or ""
                item["seen"] = _count(v.get("seen"))
            st[section][entry_key((term, heard_as))] = item
    never = raw.get("never")
    st["never"] = [k for k in never if isinstance(k, str)] if isinstance(never, list) else []
    claude = raw.get("claude")
    if isinstance(claude, dict):
        st["claude"] = {"day": _text(claude.get("day")) or "", "count": _count(claude.get("count"))}
    st["status"] = _text(raw.get("status")) or ""
    return st


def _retry(fn: Callable[[], _T]) -> _T:
    """Windows refuses to replace (or open) a file the other process has open at that very moment."""
    for _ in range(9):
        try:
            return fn()
        except PermissionError:
            time.sleep(0.03)
    return fn()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))   # bytes: text mode would turn every "\n" into "\r\n"
    _retry(lambda: os.replace(tmp, path))


def _toml(active: dict, never: list[str]) -> str:
    entries = [(a["term"], a["heard_as"]) for a in active.values()]
    lines = [HEADER + learn.dumps(entries).removeprefix(learn.HEADER).rstrip("\n")]
    if never:
        lines += ["", "[never]", "keys = [" + ", ".join(_q(k) for k in never) + "]"]
    for key, a in active.items():
        lines += ["", f"[meta.{_q(key)}]", f"first_learned = {_q(a['first_learned'])}",
                  f"seen = {a['seen']}", f"example = {_q(a['example'])}"]
    return "\n".join(lines) + "\n"


def _from_toml(d: dict) -> tuple[dict, list[str]]:
    """The active fixes (in state.json's shape, in the order they were written: the [meta] tables') and the
    never-learn keys kept in a parsed learned_auto.toml. Damaged parts are left out, never an error."""
    meta = d.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    found: list[Entry] = []
    sec = d.get("dictionary")
    if isinstance(sec, dict):
        terms = sec.get("terms")
        found += [(t, "") for t in terms if isinstance(t, str)] if isinstance(terms, list) else []
        heard = sec.get("heard_as")
        for term, v in heard.items() if isinstance(heard, dict) else ():
            if entry_key((term, "")) in meta:   # a term-only fix next to a heard_as one has no line of its own
                found.append((term, ""))
            spellings = [v] if isinstance(v, str) else v if isinstance(v, list) else []
            found += [(term, x) for x in spellings if isinstance(x, str)]
    by_key: dict[str, Entry] = {}
    for e in found:
        n = _norm(e)
        if n is not None:
            by_key.setdefault(entry_key(n), n)
    active = {}
    for key in [*(k for k in meta if k in by_key), *(k for k in by_key if k not in meta)]:
        m = meta.get(key)
        m = m if isinstance(m, dict) else {}
        term, heard_as = by_key[key]
        active[key] = {"term": term, "heard_as": heard_as, "recs": [], "example": _text(m.get("example")) or "",
                       "first_learned": _text(m.get("first_learned")) or "",
                       "seen": _count(m.get("seen")) or SIGHTINGS_TO_ACTIVATE}
    never = d.get("never")
    keys = never.get("keys") if isinstance(never, dict) else None
    return active, [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _norm(e: Entry) -> Entry | None:
    out = normalize_terms([e])
    return out[0] if out else None


def _spelling_keys(user_entries: list[Entry]) -> dict[str, set[str]]:
    """word_key of what each user entry matches (its heard_as spelling, else the term itself) -> its terms."""
    out: dict[str, set[str]] = {}
    for term, heard_as in normalize_terms(user_entries):
        out.setdefault(word_key(heard_as or term), set()).add(term)
    return out


class Store:
    def __init__(self, learning_dir: Path, learned_auto_file: Path):
        self.learning_dir = Path(learning_dir)
        self.learned_auto_file = Path(learned_auto_file)
        self._state_file = self.learning_dir / "state.json"
        self._checked_file = self.learning_dir / "checked.jsonl"

    # --- locking and disk ----------------------------------------------------------------------

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive across processes. Every write to state.json, learned_auto.toml and checked.jsonl
        happens inside it, after re-reading from disk. Raises TimeoutError after LOCK_WAIT_S."""
        self.learning_dir.mkdir(parents=True, exist_ok=True)
        with open(self.learning_dir / "state.lock", "a+b") as fh:
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"the learning state is locked by another process ({fh.name})") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

    def _load(self) -> dict:
        """state.json as it is on disk, with what learned_auto.toml keeps merged back (_merge_toml), for a
        read-modify-write. A missing file or damaged content is an empty state; an I/O error propagates,
        so a write never replaces a state it could not read."""
        return self._load_merged()[0]

    def _load_merged(self) -> tuple[dict, bool]:
        """_load, and whether learned_auto.toml added something state.json didn't have."""
        try:
            raw = json.loads(_retry(lambda: self._state_file.read_text("utf-8")))
        except FileNotFoundError:
            st = _empty()
        except (ValueError, RecursionError) as e:   # ValueError: bad JSON or bad UTF-8
            log.warning("learning state not loaded (%s), starting empty: %s", self._state_file.name, e)
            st = _empty()
        else:
            st = _clean(raw)
        return st, self._merge_toml(st)

    def _read_toml(self) -> bytes | None:
        """learned_auto.toml's bytes, None when it is missing; an I/O error propagates."""
        try:
            return _retry(self.learned_auto_file.read_bytes)
        except FileNotFoundError:
            return None

    def _merge_toml(self, st: dict) -> bool:
        """Merge learned_auto.toml's never-learn list into st, and when st has no active fix, take back the
        ones the toml still has (a lost or damaged state.json), except those on the never-learn list.
        True when st changed. A damaged toml adds nothing."""
        data = self._read_toml()
        if data is None:
            return False
        try:
            active, never = _from_toml(tomllib.loads(data.decode("utf-8-sig")))
        except (ValueError, RecursionError) as e:   # ValueError: bad TOML or bad UTF-8
            log.warning("learned fixes not read (%s): %s", self.learned_auto_file.name, e)
            return False
        added = [k for k in never if k not in st["never"]]
        st["never"] += added
        taken = {} if st["active"] else {k: a for k, a in active.items() if k not in st["never"]}
        if taken:
            log.info("learning state: %d fix(es) taken back from %s", len(taken), self.learned_auto_file.name)
            st["active"] = taken
            for key in taken:
                st["pending"].pop(key, None)
        return bool(added or taken)

    def _peek(self) -> dict:
        """_load for the read-only methods: a file that cannot be read shows as empty instead of raising."""
        try:
            return self._load()
        except OSError as e:
            log.warning("learning state not read (%s): %s", self._state_file.name, e)
            return _empty()

    def _save_state(self, st: dict) -> None:
        _write(self._state_file, json.dumps(st, ensure_ascii=False, indent=1))

    def _save(self, st: dict) -> None:
        """state.json first (the truth), then learned_auto.toml made from it."""
        self._save_state(st)
        self._sync_toml(st)

    def _sync_toml(self, st: dict) -> bool:
        """Write learned_auto.toml from st when it is out of date (only then: the tray reloads its clean-up
        when the file changes); True when it was written. A missing toml with nothing to say stays missing."""
        text = _toml(st["active"], st["never"])
        current = self._read_toml()
        if current == text.encode("utf-8") or (current is None and not st["active"] and not st["never"]):
            return False
        _write(self.learned_auto_file, text)
        return True

    def sync(self) -> None:
        """Make learned_auto.toml match state.json again, and save to state.json what was taken back from
        the toml. The checker calls this at the start of every run. Raises OSError or TimeoutError."""
        with self.lock():
            st, merged = self._load_merged()
            if merged:
                self._save_state(st)
            if self._sync_toml(st):
                log.info("learned fixes written again from the learning state (%s)", self.learned_auto_file.name)

    # --- fixes -----------------------------------------------------------------------------------

    def note(self, rec_id: str, proposals: list[Proposal], user_entries: list[Entry],
             example: str, now: str) -> list[AutoFix]:
        """Count one recording's proposals; returns the fixes that became active by it.

        Only proposals with checked=True count; one recording counts once per fix; a fix on the never-learn
        list or clashing with a user entry (same heard_as spelling, different term) is dropped."""
        user = _spelling_keys(user_entries)
        with self.lock():
            st, changed = self._load_merged()   # what the toml gave back is saved too
            never = set(st["never"])
            became: list[AutoFix] = []
            for p in proposals:
                e = _norm(p.entry) if p.checked else None
                if e is None:
                    continue
                key = entry_key(e)
                if key in never:
                    continue
                if any(t != e[0] for t in user.get(word_key(e[1] or e[0]), ())):
                    changed |= st["pending"].pop(key, None) is not None
                    continue
                if key in st["active"]:
                    fix = st["active"][key]
                    if rec_id not in fix["recs"]:
                        fix["recs"] = (fix["recs"] + [rec_id])[-RECS_KEPT:]
                        fix["seen"] += 1
                        fix["example"] = example or fix["example"]
                        changed = True
                    continue
                cand = st["pending"].setdefault(key, {"term": e[0], "heard_as": e[1], "recs": [], "example": ""})
                if rec_id in cand["recs"]:
                    continue
                cand["recs"].append(rec_id)
                cand["example"] = example or cand["example"]
                changed = True
                if len(cand["recs"]) >= SIGHTINGS_TO_ACTIVATE:
                    del st["pending"][key]
                    st["active"][key] = {**cand, "first_learned": now, "seen": len(cand["recs"])}
                    became.append(AutoFix(e[0], e[1], now, len(cand["recs"]), cand["example"]))
            if changed:
                self._save(st)
            else:
                self._sync_toml(st)   # a toml left behind by a failed write is put right here too
            return became

    def active(self) -> list[AutoFix]:
        return [AutoFix(a["term"], a["heard_as"], a["first_learned"], a["seen"], a["example"])
                for a in self._peek()["active"].values()]

    def pending_count(self) -> int:
        return len(self._peek()["pending"])

    def undo(self, e: Entry) -> None:
        """Remove a fix and never learn it again (also when it was not active or pending any more)."""
        e = _norm(e)
        if e is None:
            return
        key = entry_key(e)
        with self.lock():
            st = self._load()
            st["active"].pop(key, None)
            st["pending"].pop(key, None)
            if key not in st["never"]:
                st["never"].append(key)
            self._save(st)

    def reset_all(self) -> None:
        """Clear every active fix and every pending candidate, and never learn any of them again."""
        with self.lock():
            st = self._load()
            for key in [*st["active"], *st["pending"]]:
                if key not in st["never"]:
                    st["never"].append(key)
            st["active"], st["pending"] = {}, {}
            self._save(st)

    # --- Claude calls and status -------------------------------------------------------------------

    def claude_calls_today(self, today: str) -> int:
        claude = self._peek()["claude"]
        return claude["count"] if claude["day"] == today else 0

    def count_claude_call(self, today: str) -> None:
        with self.lock():
            st = self._load()
            claude = st["claude"]
            st["claude"] = {"day": today, "count": (claude["count"] if claude["day"] == today else 0) + 1}
            self._save_state(st)

    def set_status(self, text: str) -> None:
        with self.lock():
            st = self._load()
            if st["status"] != text:
                st["status"] = text
                self._save_state(st)

    def status(self) -> str:
        return self._peek()["status"]

    # --- checked.jsonl -------------------------------------------------------------------------------

    def _checked_raw(self) -> str:
        """checked.jsonl as text; a missing file is empty, an I/O error propagates."""
        try:
            return _retry(self._checked_file.read_bytes).decode("utf-8", "replace")
        except FileNotFoundError:
            return ""

    def append_checked(self, row: dict) -> None:
        """Add one row and keep the last CHECKED_KEEP lines. The row needs the recording's "id" (str),
        or checked_ids() would never see it and the recording would be checked again for ever.

        Always .tmp + replace, so a reader sees the old file or the new one, never half a line. If the
        old file cannot be read this raises and leaves it alone."""
        if not isinstance(row.get("id"), str):
            raise ValueError('a checked row needs an "id" (the recording id, a str)')
        line = json.dumps(row, ensure_ascii=False)   # one line: json escapes newlines inside strings
        with self.lock():
            lines = [s for s in (ln.strip() for ln in self._checked_raw().split("\n")) if s]
            _write(self._checked_file, "\n".join([*lines, line][-CHECKED_KEEP:]) + "\n")

    def checked_ids(self) -> set[str]:
        ids: set[str] = set()
        try:
            lines = self._checked_raw().split("\n")
        except OSError as e:
            log.warning("checked results not read (%s): %s", self._checked_file.name, e)
            return ids
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                ids.add(row["id"])
        return ids