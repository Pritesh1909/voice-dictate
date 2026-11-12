"""The History window: recent dictations, newest first, to search, copy again or delete.

It runs as its own process (ParakeetDictate.exe --history <file>, or python -m parakeet_dictate --history
<file> from source), for the same reasons as the correction window (see correct.py). The tray hands the
entries over in a JSON file in %LOCALAPPDATA%\\ParakeetDictate (not on the command line, where other
programs can read it), and this window deletes it as soon as it has read it.

Deleting works in both directions: the ids of what was deleted (never the text) go into
history_result.json, which the tray reads when the window closes and forgets those entries too; with
[history] keep = "disk", history.jsonl is also rewritten straight away.

The list logic (HistoryModel) has no Tk, so it is tested directly; the window only shows it.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from . import APP_NAME
from . import history as hist
from .history import Entry

log = logging.getLogger(__name__)

KEEP_NOTE = {
    "session": "Kept until Parakeet Dictate quits; not saved anywhere.",
    "disk": "Kept on this PC (history.jsonl in your local app data).",
    "off": "History is off.",
}


# --- the list, without Tk ------------------------------------------------------------------------------

class HistoryModel:
    """The entries newest first, the search, and deleting (passed on to the tray and history.jsonl)."""

    def __init__(self, entries: Iterable[Entry], keep: str, data_dir: Path, history_file: Path | None = None,
                 copy: Callable[[str], None] | None = None):
        self.entries = list(reversed(list(entries)))     # handed over oldest first
        self.keep = keep
        self.data_dir = data_dir
        self.history_file = history_file
        self._copy = copy
        self.query = ""
        self.deleted: list[str] = []

    # what is shown
    def visible(self) -> list[Entry]:
        words = self.query.casefold().split()
        if not words:
            return list(self.entries)
        return [e for e in self.entries if all(w in f"{e.text}\n{e.program}".casefold() for w in words)]

    def set_query(self, q: str) -> None:
        self.query = q

    def get(self, entry_id: str) -> Entry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    @staticmethod
    def when(e: Entry, now: datetime | None = None) -> str:
        t, now = datetime.fromisoformat(e.time), now or datetime.now()
        if t.date() == now.date():
            return t.strftime("%H:%M")
        return t.strftime("%d %b %H:%M" if t.year == now.year else "%d %b %Y %H:%M").lstrip("0")

    @staticmethod
    def program(e: Entry) -> str:
        p = e.program.rsplit("\\", 1)[-1]
        return (p[:-4] if p.lower().endswith(".exe") else p) or "–"

    @staticmethod
    def first_line(e: Entry, width: int = 90) -> str:
        line = next((x.strip() for x in e.text.splitlines() if x.strip()), "")
        return line if len(line) <= width else line[:width - 1].rstrip() + "…"

    def row(self, e: Entry, now: datetime | None = None) -> tuple[str, str, str]:
        return self.when(e, now), self.program(e), self.first_line(e)

    @staticmethod
    def details(e: Entry) -> str:
        t = datetime.fromisoformat(e.time)
        parts = [f"{t:%A} {t.day} {t:%B %Y}, {t:%H:%M}", HistoryModel.program(e),
                 f"{e.words} word{'' if e.words == 1 else 's'}"]
        if e.seconds:
            parts.append(f"{e.seconds:.1f} s spoken")
        return " · ".join(parts)

    def count_text(self) -> str:
        n, shown = len(self.entries), len(self.visible())
        total = f"{n} dictation{'' if n == 1 else 's'}"
        return total if not self.query.strip() else f"{shown} of {total}"

    # what can be done
    def copy(self, entry_id: str) -> str | None:
        """Put an entry's text on the clipboard. Returns a problem, or None."""
        e = self.get(entry_id)
        if e is None:
            return "Pick a dictation first."
        try:
            (self._copy or _clipboard)(e.text)
        except OSError as err:
            return f"Couldn't copy: {err}"
        return None

    def delete(self, ids: Iterable[str]) -> str | None:
        ids = [i for i in ids if self.get(i) is not None]
        if not ids:
            return None
        self.entries = [e for e in self.entries if e.id not in ids]
        self.deleted += ids
        return self._pass_on()

    def clear(self) -> str | None:
        return self.delete([e.id for e in self.entries])

    def _pass_on(self) -> str | None:
        """Tell the tray (when the window closes) and, with keep = "disk", fix history.jsonl now."""
        try:
            hist.write_result(self.data_dir, self.deleted)
            if self.keep == "disk" and self.history_file is not None:
                hist.remove_from_file(self.history_file, self.deleted)
        except OSError as e:
            log.warning("history window: %s", e)
            return f"Couldn't save the change: {e}"
        return None


def _clipboard(text: str) -> None:
    from . import output
    output.copy_to_clipboard(text)


# --- the process: command line, launch ---------------------------------------------------------------

def command(request: Path) -> tuple[list[str], str | None]:
    """(argv, working folder) that opens the window: the exe itself when installed, pythonw from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--history", str(request)], None
    from .winutil import PROJECT_DIR, _pythonw
    return [_pythonw(), "-m", "parakeet_dictate", "--history", str(request)], str(PROJECT_DIR)


def launch(keep: str, entries: Iterable[Entry], data_dir: Path, popen=subprocess.Popen):
    """Open the window without waiting for it. Returns (process, request file)."""
    request = hist.write_request(data_dir, keep, entries)
    argv, cwd = command(request)
    try:
        return popen(argv, cwd=cwd, close_fds=True), request
    except Exception:
        request.unlink(missing_ok=True)
        raise


# --- the window ----------------------------------------------------------------------------------------

class HistoryWindow:
    """Search box, the list, the selected dictation's text and the buttons. Tk is imported here."""

    def __init__(self, root, model: HistoryModel, close: Callable[[], None] | None = None,
                 confirm: Callable[[str], bool] | None = None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.model = root, model
        self.close = close or root.destroy
        self.confirm = confirm or self._ask
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self._styles()
        self._layout()
        self.refresh()

    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def _styles(self) -> None:
        st = self.ttk.Style(self.root)
        dark = st.theme_use() == "sun-valley-dark"
        self.muted = "#a0a0a0" if dark else "#5c5c5c"
        self.error_fg = "#ff99a4" if dark else "#c42b1c"
        self.border = "#3d3d3d" if dark else "#e3e3e3"
        self.field_bg = "#2b2b2b" if dark else "#fdfdfd"
        self.field_fg = "#fafafa" if dark else "#1c1c1c"
        self.select_bg = "#2d6aa3" if dark else "#cce4f7"
        self.select_fg = "#ffffff" if dark else "#000000"
        self.bg = st.lookup("TFrame", "background") or ("#1c1c1c" if dark else "#fafafa")
        fonts = set(self.root.tk.call("font", "names"))
        f = lambda name, fallback: name if name in fonts else fallback   # noqa: E731
        self.f_body = f("SunValleyBodyFont", ("Segoe UI", 10))
        self.f_caption = f("SunValleyCaptionFont", ("Segoe UI", 9))
        self.f_title = f("SunValleySubtitleFont", ("Segoe UI Semibold", 15))
        st.configure("Help.TLabel", foreground=self.muted, font=self.f_caption)
        st.configure("Title.TLabel", font=self.f_title)
        st.configure("Error.TLabel", foreground=self.error_fg)
        st.configure("History.Treeview", rowheight=self.px(30))
        st.configure("Hint.TLabel", foreground="#8a8a8a" if dark else "#8f8f8f", background=self.field_bg)

    def _layout(self) -> None:
        tk, ttk, px = self.tk, self.ttk, self.px
        outer = self.outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        bar = ttk.Frame(outer, padding=(px(16), px(10)))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(outer).pack(side="bottom", fill="x")
        self.copy_close_btn = ttk.Button(bar, text="Copy and close", style="Accent.TButton",
                                         command=self.copy_and_close)
        self.copy_close_btn.pack(side="right")
        self.copy_btn = ttk.Button(bar, text="Copy", command=self.copy, width=8)
        self.copy_btn.pack(side="right", padx=(0, px(8)))
        self.delete_btn = ttk.Button(bar, text="Delete", command=self.delete, width=8)
        self.delete_btn.pack(side="left")
        self.clear_btn = ttk.Button(bar, text="Clear all…", command=self.clear)
        self.clear_btn.pack(side="left", padx=(px(8), 0))
        self.msg = ttk.Label(bar, text="", style="Help.TLabel")
        self.msg.pack(side="left", fill="x", expand=True, padx=(px(14), px(8)))

        body = ttk.Frame(outer, padding=(px(20), px(14), px(20), px(12)))
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="History", style="Title.TLabel").pack(side="left")
        self.count = ttk.Label(head, text="", style="Help.TLabel")
        self.count.pack(side="right", anchor="s", pady=(0, px(3)))
        ttk.Label(body, text=KEEP_NOTE.get(self.model.keep, ""), style="Help.TLabel").pack(anchor="w")

        self.query = tk.StringVar(self.root)
        self.search = ttk.Entry(body, textvariable=self.query)
        self.search.pack(fill="x", pady=(px(12), px(10)))
        self.hint = ttk.Label(self.search, text="Search", style="Hint.TLabel", cursor="xterm")
        self.hint.bind("<Button-1>", lambda _e: self.search.focus_set())
        self.query.trace_add("write", lambda *_: self.on_search())

        lower = ttk.Frame(body)
        lower.pack(side="bottom", fill="x", pady=(px(10), 0))
        self.meta = ttk.Label(lower, text="", style="Help.TLabel")
        self.meta.pack(anchor="w", pady=(0, px(4)))
        outline = tk.Frame(lower, background=self.border, borderwidth=0, highlightthickness=0)
        outline.pack(fill="x")
        self.text = tk.Text(outline, height=6, wrap="word", font=self.f_body, relief="flat", borderwidth=0,
                            padx=px(10), pady=px(8), background=self.field_bg, foreground=self.field_fg,
                            selectbackground=self.select_bg, selectforeground=self.select_fg,
                            inactiveselectbackground=self.select_bg, highlightthickness=0,
                            insertwidth=0, cursor="xterm", state="disabled", takefocus=True)
        text_sb = ttk.Scrollbar(outline, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self._autohide(text_sb, self.text, side="right", fill="y", padx=(0, 1), pady=1))
        self.text.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        self.text.bind("<Button-1>", lambda _e: self.text.focus_set())   # read-only, but selectable and copyable

        listing = ttk.Frame(body)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("when", "program", "text"), show="headings",
                                 selectmode="browse", style="History.Treeview")
        for col, title, width, stretch in (("when", "Time", 110, False), ("program", "Program", 120, False),
                                           ("text", "Text", 360, True)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=px(width), minwidth=px(60), stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._autohide(sb, self.tree, side="right", fill="y"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_selected())
        self.tree.bind("<Delete>", lambda _e: self.delete())
        self.tree.bind("<Return>", lambda _e: self.copy_and_close())
        self.tree.bind("<Double-1>", lambda _e: self.copy_and_close())
        self.search.bind("<Down>", lambda _e: self._to_list())
        self.search.bind("<Return>", lambda _e: self.copy_and_close())
        self.root.bind("<Control-f>", lambda _e: self.search.focus_set())

    @staticmethod
    def _autohide(sb, content, **pack):
        """A yscrollcommand that shows the scrollbar only while there is something to scroll."""
        def set_(lo, hi):
            sb.set(lo, hi)
            need = float(lo) > 0 or float(hi) < 1
            if need and not sb.winfo_ismapped():
                sb.pack(**pack, before=content)
            elif not need and sb.winfo_ismapped():
                sb.pack_forget()
        return set_

    # showing --------------------------------------------------------------------------------------------
    def refresh(self, keep_selection: str | None = None) -> None:
        """Fill the list from the model (after a search or a delete) and select an entry."""
        self.tree.delete(*self.tree.get_children())
        now = datetime.now()
        shown = self.model.visible()
        for e in shown:
            self.tree.insert("", "end", iid=e.id, values=self.model.row(e, now))
        self.count.configure(text=self.model.count_text())
        if shown:
            pick = keep_selection if keep_selection in {e.id for e in shown} else shown[0].id
            self.tree.selection_set(pick)
            self.tree.focus(pick)
            self.tree.see(pick)
        self.show_selected()
        self._placeholder()

    def selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def show_selected(self) -> None:
        e = self.model.get(self.selected() or "")
        if e is None:
            empty = ("No dictations match the search." if self.model.entries else "No dictations in the history.")
            self._set_text("")
            self.meta.configure(text=empty)
        else:
            self._set_text(e.text)
            self.meta.configure(text=self.model.details(e))
        for b in (self.copy_btn, self.copy_close_btn, self.delete_btn):
            b.state(["!disabled"] if e else ["disabled"])
        self.clear_btn.state(["!disabled"] if self.model.entries else ["disabled"])

    def _set_text(self, text: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.configure(state="disabled")

    def _placeholder(self) -> None:
        if self.query.get():
            self.hint.place_forget()
        else:
            self.hint.place(x=self.px(11), rely=0.5, anchor="w")

    def message(self, text: str, error: bool = False) -> None:
        self.msg.configure(text=text, style="Error.TLabel" if error else "Help.TLabel")

    def _to_list(self):
        self.tree.focus_set()
        return "break"

    # doing ----------------------------------------------------------------------------------------------
    def on_search(self) -> None:
        self.model.set_query(self.query.get())
        self.refresh(self.selected())

    def copy(self) -> bool:
        problem = self.model.copy(self.selected() or "")
        self.message(problem or "Copied to the clipboard.", error=bool(problem))
        return problem is None

    def copy_and_close(self):
        if self.copy():
            self.close()
        return "break"

    def delete(self):
        current = self.selected()
        if current is None:
            return "break"
        shown = [e.id for e in self.model.visible()]
        i = shown.index(current)
        after = (shown[i + 1:] or shown[:i][-1:] or [None])[0]       # the next one, else the one before
        problem = self.model.delete([current])
        self.refresh(after)
        self.message(problem or "Deleted.", error=bool(problem))
        return "break"

    def clear(self) -> None:
        n = len(self.model.entries)
        if not n or not self.confirm(f"Delete all {n} dictation{'' if n == 1 else 's'} from the history?"):
            return
        problem = self.model.clear()
        self.query.set("")
        self.refresh()
        self.message(problem or "History cleared.", error=bool(problem))

    def _ask(self, question: str) -> bool:
        from tkinter import messagebox
        return messagebox.askyesno(APP_NAME, question, parent=self.root, icon="warning", default="no")


def run(paths, request: Path) -> int:
    """Entry point of the --history process."""
    import ctypes

    from .settings_ui import make_root
    keep, entries = hist.read_request(request)    # deletes the request file
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)    # crisp text on scaled screens
    except (AttributeError, OSError):
        pass
    root = make_root()
    root.title(f"{APP_NAME} history")
    try:
        from .tray import write_ico
        root.iconbitmap(default=str(write_ico(paths.data_dir / "icon.ico")))
    except Exception:
        pass
    win = HistoryWindow(root, HistoryModel(entries, keep, paths.data_dir, paths.history_file))
    w, h = win.px(760), win.px(580)
    x = max(0, (root.winfo_screenwidth() - w) // 2)
    y = max(0, (root.winfo_screenheight() - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.minsize(win.px(520), win.px(420))
    root.bind("<Escape>", lambda _e: win.close())
    root.deiconify()
    root.attributes("-topmost", True)     # opened from the tray: make sure it isn't hidden behind
    root.after(200, lambda: root.attributes("-topmost", False))
    root.focus_force()
    win.search.focus_set()
    root.mainloop()
    return 0
