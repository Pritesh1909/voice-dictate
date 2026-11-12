"""The "Correct last dictation…" window: fix the text, tick the words to learn, Save.

It runs as its own short-lived process (ParakeetDictate.exe --correct <file>, or
python -m parakeet_dictate --correct <file> from source). Why not a window inside the tray process:
the overlay already owns a Tk root on its own thread, and Tk must only be used from the thread that
created it; a second tk.Tk() on another thread (the tray menu's) is unsafe and can crash the whole app.
A separate process has its own Tk, takes focus normally (the overlay never does), can't freeze or
crash the tray, works whether or not the overlay is on, and in the installed build sys.executable is
ParakeetDictate.exe itself, so nothing extra has to ship.

The dictated text is handed over in a small JSON file in %LOCALAPPDATA%\\ParakeetDictate (not on the
command line, where other programs can read it), and the dialog deletes it as soon as it has read it.
Saving merges the ticked entries into learned.toml; the tray app picks them up on the next dictation.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import uuid
from pathlib import Path

from . import APP_NAME, learn

log = logging.getLogger(__name__)


class CorrectionModel:
    """Everything the window shows, without any Tk: original text, the user's fix and the proposals."""

    def __init__(self, original: str, known: list[tuple[str, str]] = ()):
        self.original = original
        self.corrected = original
        self._known = set(known)
        self.proposals: list[learn.Proposal] = []
        self.checked: list[bool] = []

    def update(self, corrected: str) -> None:
        """Re-diff after an edit, keeping the user's ticks for proposals that are still there."""
        before = {p.entry: c for p, c in zip(self.proposals, self.checked)}
        self.corrected = corrected
        self.proposals = [p for p in learn.propose(self.original, corrected) if p.entry not in self._known]
        self.checked = [before.get(p.entry, p.checked) for p in self.proposals]

    def toggle(self, i: int, value: bool | None = None) -> None:
        self.checked[i] = (not self.checked[i]) if value is None else value

    def selected(self) -> list[tuple[str, str]]:
        return [p.entry for p, c in zip(self.proposals, self.checked) if c]

    @staticmethod
    def label(p: learn.Proposal) -> str:
        return f"{p.term}   ({p.note})"


# --- process hand-off -----------------------------------------------------------------------------

def write_request(data_dir: Path, text: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"correct-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps({"text": text}), "utf-8")
    return path


def read_request(path: Path) -> str:
    """The text to correct; the request file is deleted right away (it holds dictated text)."""
    try:
        return json.loads(path.read_text("utf-8")).get("text", "")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def command(request: Path) -> tuple[list[str], str | None]:
    """(argv, working folder) that opens the dialog: the exe itself when installed, pythonw from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--correct", str(request)], None
    from .winutil import PROJECT_DIR, _pythonw
    return [_pythonw(), "-m", "parakeet_dictate", "--correct", str(request)], str(PROJECT_DIR)


def launch(text: str, data_dir: Path, popen=subprocess.Popen):
    """Open the dialog for `text` without waiting for it. Returns the process (wait() for Save/Cancel)."""
    request = write_request(data_dir, text)
    argv, cwd = command(request)
    try:
        return popen(argv, cwd=cwd, close_fds=True)
    except Exception:
        request.unlink(missing_ok=True)
        raise


def known_entries(paths) -> list[tuple[str, str]]:
    """Entries the user already has (config.toml and learned.toml), so they aren't proposed again."""
    from . import config
    known = learn.load(paths.learned_file)
    try:
        known += list(config.load(paths).dictionary)
    except Exception as e:   # a broken config.toml shouldn't stop the user from correcting text
        log.warning("correct: settings not read: %s", e)
    return known


def run(paths, request: Path) -> int:
    """Entry point of the --correct process."""
    text = read_request(request)
    if not text.strip():
        return 0
    model = CorrectionModel(text, known_entries(paths))
    model.update(text)
    if _show(model):
        chosen = model.selected()
        if chosen:
            learn.add(paths.learned_file, chosen)
            log.info("learned %d dictionary entr%s", len(chosen), "y" if len(chosen) == 1 else "ies")
    return 0


# --- the window (not exercised by the automated tests) ----------------------------------------------

def _show(model: CorrectionModel) -> bool:
    """Show the window; True when the user clicked Save."""
    import ctypes
    import tkinter as tk
    from tkinter import ttk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on scaled screens
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title(f"{APP_NAME} – correct last dictation")
    root.minsize(520, 360)
    saved = [False]
    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Fix the text below. Words you correct can be learned for next time.").pack(anchor="w")
    box = tk.Text(frame, height=7, wrap="word", undo=True, font=("Segoe UI", 11))
    box.insert("1.0", model.original)
    box.pack(fill="both", expand=True, pady=(6, 10))
    ttk.Label(frame, text="Learn these spellings:").pack(anchor="w")
    checks = ttk.Frame(frame)
    checks.pack(fill="x", pady=(4, 10))
    empty = ttk.Label(checks, text="(nothing yet: correct a word above)", foreground="#777")
    variables: list[tk.BooleanVar] = []

    def redraw() -> None:
        for w in checks.winfo_children():
            if w is not empty:
                w.destroy()
        variables.clear()
        if not model.proposals:
            empty.pack(anchor="w")
            return
        empty.pack_forget()
        for i, p in enumerate(model.proposals):
            v = tk.BooleanVar(value=model.checked[i])
            variables.append(v)
            ttk.Checkbutton(checks, text=model.label(p), variable=v,
                            command=lambda i=i, v=v: model.toggle(i, v.get())).pack(anchor="w")

    def on_edit(_event=None) -> None:
        text = box.get("1.0", "end-1c")
        if text != model.corrected:
            model.update(text)
            redraw()

    def save() -> None:
        saved[0] = True
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Cancel", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="Save", command=save).pack(side="right", padx=(0, 8))
    box.bind("<KeyRelease>", on_edit)   # typing, Backspace, Ctrl+V and Ctrl+Z all end in a key release
    root.bind("<Escape>", lambda _e: root.destroy())
    redraw()
    root.attributes("-topmost", True)    # opened from the tray: make sure it isn't hidden behind
    root.after(200, lambda: root.attributes("-topmost", False))
    root.focus_force()
    box.focus_set()
    root.mainloop()
    return saved[0]
