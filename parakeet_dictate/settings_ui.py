"""The Settings window: every everyday setting on a page of its own, saved back into config.toml.

It runs as its own process (ParakeetDictate.exe --settings, or python -m parakeet_dictate --settings from
source), for the same reasons as the "Correct last dictation…" window (see correct.py): the overlay owns
the tray process's only Tk root. When this process exits, the tray app reloads config.toml.

The window only shows and collects values; reading, validating and writing config.toml is in
settings_store.py (no Tk there, so it is tested directly).
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import APP_NAME, __version__, config, hotkey
from .commands import ACTIONS
from . import settings_store as store_mod
from .settings_store import Key, Store

log = logging.getLogger(__name__)


# --- what each page shows ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    key: Key | None          # None for things that aren't in config.toml (start with Windows, notes)
    kind: str                # switch | choice | combo | int | float | csv | list | pairs | commands |
                             # hotkey | startup | model | mic | vad | learned_auto | note | action | info
    label: str
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()   # (value, label)
    lo: float = 0
    hi: float = 0
    step: float = 1
    browse: str = ""         # list editors: "" (type entries), "folders", "files+folders"
    fact: str = ""           # info rows: which ABOUT fact is shown on the right (choices: buttons next to it)


S = Spec
# Pages, each made of titled groups (boxes) of settings. Everyday settings are on General; what improves
# recognition is on Improvements; what is rarely touched is on Extras.
PAGES: list[tuple[str, list[tuple[str, list[Spec]]]]] = [
    ("General", [
        ("Start-up", [
            S(None, "startup", "Start with Windows", "Start Parakeet Dictate when you sign in to Windows."),
            S(("output", "sounds"), "switch", "Sounds", "Short beeps when recording starts, stops or is cancelled."),
        ]),
        ("Microphone", [
            S(("audio", "input_device"), "mic", "Microphone",
              "When it isn't connected, the Windows default is used."),
            S(("audio", "silence_stop_seconds"), "int", "Stop after silence (seconds)",
              "Ends a tapped dictation when nothing has been said this long. 0 = never.",
              lo=0, hi=3600, step=5),
            S(("audio", "pause_media"), "switch", "Pause music while dictating",
              "Music or videos that are playing pause while you speak, then carry on."),
        ]),
        ("Speech model", [
            S(("model", "dir"), "model", "Model",
              "Automatic picks the best model found on this PC."),
            S(("model", "folders"), "list", "Your model files and folders",
              "Models in these .gguf files or folders are offered too.", browse="files+folders"),
            S(("model", "device"), "choice", "Runs on",
              "Automatic = the graphics card if there is one, else the CPU.",
              choices=(("auto", "Automatic"), ("gpu", "Graphics card"),
                       ("igpu", "Built-in graphics (Intel)"), ("cpu", "CPU"))),
            S(("model", "language"), "combo", "Language",
              "auto, or a code such as en, de, fr. It also picks which filler sounds are removed.",
              choices=tuple((c, c) for c in ("auto", "en", "de", "fr", "es", "it", "pt", "nl", "pl", "ja", "zh",
                                             "ko", "ru", "uk", "hi"))),
            S(None, "action", "Look for models again", "Searches this PC and WSL when this window closes.",
              choices=(("search_models", "Find models"),)),
        ]),
        ("Typing", [
            S(("output", "method"), "choice", "Typing method",
              "Type leaves the clipboard alone. Paste uses Ctrl+V, then restores the clipboard.",
              choices=(("type", "Type (keystrokes)"), ("paste", "Paste (clipboard)"))),
            S(("output", "type_while_speaking"), "switch", "Type while speaking",
              "Type each phrase as soon as it is ready, instead of everything after you stop."),
            S(("model", "stream_while_speaking"), "switch", "Show words as you speak",
              "Words appear about a second behind your voice (Type method only)."),
            S(("output", "press_enter"), "switch", "Press Enter after typing",
              "Sends a chat message or runs a command straight away."),
        ]),
        ("On-screen indicator", [
            S(("overlay", "enabled"), "switch", "Show the indicator",
              "Shows that you are being listened to, on top of all windows. It never takes the focus."),
            S(("overlay", "style"), "choice", "Style",
              "Pill: Listening, level, your words as you speak. Button: a round mic you can click.",
              choices=(("pill", "Pill"), ("button", "Button"))),
            S(("overlay", "button_visible"), "choice", "Show the button",
              "Button style: always on screen (click to start), or only while you dictate.",
              choices=(("always", "Always"), ("dictating", "Only while dictating"))),
            S(("overlay", "position"), "choice", "Position", "Used until you move it yourself.",
              choices=(("top", "Top centre"), ("bottom", "Bottom centre"), ("top-left", "Top left"),
                       ("top-right", "Top right"), ("bottom-left", "Bottom left"),
                       ("bottom-right", "Bottom right"))),
            S(("overlay", "margin"), "int", "Distance from the edge (pixels)",
              "-1 = the default (24 at the top, 56 at the bottom).", lo=-1, hi=600),
            S(None, "action", "Place it yourself", "Drag it anywhere; it stays there.",
              choices=(("move_pill", "Move…"), ("reset_pill", "Reset"))),
        ]),
    ]),
    ("Shortcuts", [
        ("Shortcuts", [
            S(("hotkey", "keys"), "hotkey", "Dictate",
              "Tap to start and again to stop, or hold while you talk."),
            S(("hotkey", "copy_last"), "hotkey_opt", "Copy last dictation",
              "Puts the last dictated text on the clipboard."),
            S(("hotkey", "correct_last"), "hotkey_opt", "Correct last dictation",
              "Opens the last dictation to fix a word; fixes are learned."),
            S(("hotkey", "history"), "hotkey_opt", "History",
              "Opens the list of recent dictations to copy one again."),
        ]),
        ("While recording", [
            S(("hotkey", "hold_threshold"), "float", "Hold time (seconds)",
              "Holding Dictate longer than this means hold-to-talk.", lo=0, hi=5, step=0.1),
            S(("hotkey", "esc_cancels"), "switch", "Esc cancels", "Esc stops a recording without typing anything."),
        ]),
    ]),
    ("Improvements", [
        ("Dictionary", [
            S(("dictionary", "terms"), "list", "Names and terms",
              'Spelled your way. "Keycloak" also fixes "key cloak" and "KEYCLOAK".'),
            S(("dictionary", "heard_as"), "pairs", "Heard as",
              "What the model writes when it mishears a term in a way spelling alone can't fix."),
            S(None, "action", "Learned words", "Fixes saved from \"Correct last dictation\".",
              choices=(("open_learned", "Open"),)),
        ]),
        ("Clean-up", [
            S(("cleanup", "language_fillers"), "switch", "Remove filler sounds",
              '"um" and "uh" in English, "äh" in German, and so on.'),
            S(("cleanup", "fillers"), "csv", "Your extra fillers",
              'Separated by commas. "uh" also removes "uhh".'),
            S(("cleanup", "remove_repeated_words"), "switch", "Remove repeated words",
              'Collapses stutters such as "the the".'),
            S(("cleanup", "numbers"), "switch", "Numbers as digits",
              '"port six zero four zero" → "port 6040", "twenty five percent" → "25%". Prose stays.'),
            S(("cleanup", "id_prefixes"), "csv", "Ticket prefixes",
              'Separated by commas. "SEC twelve" → "SEC-12".'),
        ]),
        ("Context words", [
            S(("context", "enabled"), "switch", "Use context words",
              'Spells code names your way ("mcp auth filter" → "McpAuthFilter").'),
            S(("context", "window_title"), "switch", "From the window title",
              "Names in the title of the window you dictate into."),
            S(("context", "folders"), "list", "Notes folders",
              "Folders of .md notes (an Obsidian vault) to take names from. Only read.", browse="folders"),
            S(("context", "code_apps"), "list", "Programs for code",
              "Where names made of ordinary words (userId) are used too."),
        ]),
        ("Learning in the background", [
            S(("learning", "enabled"), "switch", "Learn from my dictations",
              "Whisper re-listens while the PC is idle; repeated name mistakes become fixes."),
            S(("learning", "use_claude"), "switch", "Ask Claude to judge disagreements",
              "Sends text only, never your voice. A few cents a day at most."),
            S(None, "learned_auto", "Learned automatically", "Undo a fix and it is never learned again."),
        ]),
        ("Recognition", [
            S(("model", "vad"), "vad", "Voice detector",
              "auto = Silero VAD (finds where phrases start and end), off = loudness only."),
            S(("accuracy", "normalize_audio"), "switch", "Clean the recording",
              "Removes silence and raises quiet speech before transcribing."),
            S(("accuracy", "noise_reduction"), "choice", "Reduce background noise",
              "Turns down steady noise (fan, hum). Automatic = only when the phrase is noisy.",
              choices=(("off", "Off"), ("auto", "Automatic"), ("on", "Always"))),
            S(("accuracy", "hotwords"), "list", "Preferred words",
              "Only models that support boosting use these (Parakeet doesn't)."),
        ]),
    ]),
    ("Extras", [
        ("Performance", [
            S(("model", "unload_after_seconds"), "int", "Free memory after (seconds)",
              "The model (about 700 MB) is unloaded this long after a dictation. -1 = never.",
              lo=-1, hi=86400, step=10),
            S(("model", "threads"), "int", "CPU threads", "Used while transcribing on the CPU.", lo=1, hi=32),
            S(("model", "process_while_speaking"), "switch", "Transcribe at each pause",
              "Off = transcribe everything after you stop."),
            S(("model", "pause_seconds"), "float", "Pause length (seconds)",
              "A pause this long ends a phrase.", lo=0.3, hi=5, step=0.1),
            S(("output", "max_seconds"), "int", "Longest dictation (seconds)",
              "A dictation stops by itself after this long.", lo=5, hi=1800, step=10),
            S(None, "action", "Model memory", "Unloads the model now, when this window closes.",
              choices=(("free_memory", "Free now"),)),
        ]),
        ("Spoken commands", [
            S(("commands", "enabled"), "switch", "Spoken commands",
              "Said on its own between two pauses, a command is carried out instead of typed."),
            S(("commands", "phrases"), "commands", "Phrases",
              "newline, paragraph, undo (removes the last phrase) or stop (ends the dictation)."),
        ]),
        ("Details", [
            S(("output", "add_trailing_space"), "switch", "Space after each dictation",
              "So the next dictation doesn't stick to the last one."),
            S(("cleanup", "repeated_words_allowed"), "csv", "Words that may repeat",
              'Separated by commas, as in "he had had enough".'),
            S(("context", "max_terms"), "int", "Most context names kept", "0-500, most used first.", lo=0, hi=500),
            S(("context", "refresh_minutes"), "int", "Check notes for changes every (minutes)",
              "Only changed notes are read again.", lo=1, hi=1440),
        ]),
        ("Privacy and files", [
            S(("output", "log_transcripts"), "switch", "Write dictated text to the log",
              "Off by default: dictation can contain sensitive text."),
            S(("history", "keep"), "choice", "Keep a history of dictations",
              "For tray menu → History. Until the app quits = memory only; On this PC = also on disk.",
              choices=(("off", "Off"), ("session", "Until the app quits"), ("disk", "On this PC"))),
            S(("history", "max_items"), "int", "History length (dictations)",
              "10-5000, the oldest dropped first.", lo=10, hi=5000, step=10),
            S(("learning", "keep_recordings"), "switch", "Keep recordings for learning",
              "On this PC only, deleted after the number of days below."),
            S(("learning", "keep_days"), "int", "Keep recordings for (days)", "1-365.", lo=1, hi=365),
            S(None, "action", "Recordings",
              "What you said, as audio and text; the checked text stays in learning\\checked.jsonl until you delete it.",
              choices=(("delete_recordings", "Delete all"), ("open_recordings", "Open folder"))),
            S(None, "action", "Files", "Per-program settings and exact replacements are in the settings file.",
              choices=(("open_log", "Open log"), ("open_file", "Open settings file"))),
        ]),
    ]),
    ("About", [
        ("Parakeet Dictate", [
            S(None, "info", "Version", "Offline push-to-talk dictation for Windows. Your voice never leaves this PC.",
              fact="version"),
            S(None, "info", "Speech engine",
              "transcribe.cpp, the engine Orca uses. Models that can stream (Parakeet Unified EN) are fed your "
              "voice as you speak when they run on a graphics card; others read each phrase at the pause.",
              fact="engine"),
            S(None, "info", "Python", "", fact="python"),
        ]),
        ("Files", [
            S(None, "info", "Data folder", "Settings, log, history and learned words.", fact="data_dir",
              choices=(("open_data", "Open folder"), ("open_log", "Open log"))),
        ]),
        ("Credits", [
            S(None, "info", "Speech models", "Found in Orca's folder, or downloaded from Hugging Face.",
              fact="models"),
            S(None, "info", "Font", "", fact="font"),
            S(None, "info", "Look", "", fact="theme"),
        ]),
    ]),
]
del S


def _engine_version() -> str:
    from importlib import metadata
    try:
        return f"transcribe.cpp {metadata.version('transcribe-cpp')}"
    except Exception:
        return "transcribe.cpp"


# What the About page shows on the right of each info row (paths: config.Paths).
ABOUT: dict[str, Callable[[Any], str]] = {
    "version": lambda paths: __version__ + ("" if getattr(sys, "frozen", False) else " (run from source)"),
    "engine": lambda paths: _engine_version(),
    "python": lambda paths: platform.python_version(),
    "data_dir": lambda paths: str(paths.data_dir),
    "models": lambda paths: "NVIDIA Parakeet",
    "font": lambda paths: "Inter (SIL Open Font License 1.1)",
    "theme": lambda paths: "Sun Valley (sv-ttk)",
}

def normalize(spec: Spec, v: Any) -> Any:
    """Values as the window compares them (heard_as text -> list, None -> empty)."""
    if spec.kind == "pairs":
        return {str(k): ([x] if isinstance(x, str) else list(x)) for k, x in (v or {}).items()}
    if spec.kind == "commands":
        return dict(v or {})
    if spec.kind in ("list", "csv"):
        return list(v or [])
    return v


# --- the process: command line, launch ---------------------------------------------------------------

def command() -> tuple[list[str], str | None]:
    """(argv, working folder) that opens the window: the exe itself when installed, pythonw from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--settings"], None
    from .winutil import PROJECT_DIR, _pythonw
    return [_pythonw(), "-m", "parakeet_dictate", "--settings"], str(PROJECT_DIR)


def launch(popen=subprocess.Popen):
    """Open the Settings window without waiting for it. Returns the process (wait() for it to close)."""
    argv, cwd = command()
    return popen(argv, cwd=cwd, close_fds=True)


def windows_uses_light_theme() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return True


# --- outside world, replaceable in tests ------------------------------------------------------------

@dataclass
class Env:
    found_models: list = field(default_factory=list)       # models.find(include_wsl=False)
    mics: list[str] = field(default_factory=list)
    startup_on: bool = False
    set_startup: Callable[[bool], None] = lambda on: None
    available: Callable[[str], bool] = hotkey.available
    model_in_use: str = ""
    folder_models: Callable = store_mod.folder_models
    list_mics: Callable = store_mod.input_devices     # used when mics is None (read after the window shows)

    @classmethod
    def real(cls, paths: config.Paths) -> "Env":
        from . import models, winutil
        from .tray import write_ico
        try:
            found = models.find(include_wsl=False)
        except Exception:
            log.exception("settings: model search")
            found = []
        mics = None      # listed in the background once the window shows (starting PortAudio takes ~0.4 s)
        lnk = winutil.startup_shortcut()

        def set_startup(on: bool) -> None:
            if on and not lnk.exists():
                winutil.create_shortcut(lnk, write_ico(paths.data_dir / "icon.ico"))
            elif not on and lnk.exists():
                lnk.unlink()

        return cls(found, mics, lnk.exists(), set_startup, hotkey.available,
                   store_mod.model_in_use(paths.data_dir))


# --- the window --------------------------------------------------------------------------------------

class SettingsWindow:
    """All pages built at once (only the selected one is shown). Tk is imported here, not at module level."""

    def __init__(self, root, store: Store, env: Env, open_file: Callable[[Path], None] | None = None,
                 close: Callable[[], None] | None = None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.store, self.env = root, store, env
        self.open_file = open_file or (lambda p: os.startfile(p))
        self.close = close or root.destroy
        self.saved = False
        self.ready = False          # all pages built (controls report edits from then on)
        self.fields: list[Field] = []
        self.learned_auto: LearnedAuto | None = None    # not a setting (not in fields); set when its page is built
        self.pages: dict[str, Any] = {}
        self.current_hotkey = str(store.value(("hotkey", "keys"), store_mod.DEFAULT_HOTKEY))
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self._groups = dict(PAGES)
        self._styles()
        self._layout()
        # Only the first page is built now: building every control of every page up front took over a
        # second. The others are built the first time they are shown (or a field on them is needed).
        self.nav.selection_set(PAGES[0][0])
        self.show(PAGES[0][0])
        if store.config_error:
            self.message(f"The settings file has a problem: {store.config_error}", error=True)
        self.ready = True
        self.refresh_dirty()
        if env.mics is None:            # listing microphones starts the audio system: do it in the background
            self._load_mics()

    def _ensure_page(self, name: str) -> None:
        if name in self.pages:
            return
        was, self.ready = self.ready, False     # setting the shown values isn't an edit
        try:
            self._build_page(name, self._groups[name])
        finally:
            self.ready = was

    def _load_mics(self) -> None:
        import threading
        box: list = []

        def work():
            try:
                box.append(self.env.list_mics())
            except Exception:
                log.exception("settings: microphones")
                box.append([])
        threading.Thread(target=work, daemon=True, name="list-mics").start()

        def check():
            if not box:
                self.root.after(100, check)
                return
            self.env.mics = box[0]
            for f in self.fields:
                if f.spec.kind == "mic":
                    f.control.add_mics(box[0])
        self.root.after(100, check)

    # layout ---------------------------------------------------------------------------------------
    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def _styles(self) -> None:
        st = self.ttk.Style(self.root)
        dark = st.theme_use() == "sun-valley-dark"
        self.muted = "#a0a0a0" if dark else "#5c5c5c"
        self.error_fg = "#ff99a4" if dark else "#c42b1c"
        self.bg = st.lookup("TFrame", "background") or ("#1c1c1c" if dark else "#fafafa")
        fonts = set(self.root.tk.call("font", "names"))
        f = lambda name, fallback: name if name in fonts else fallback   # noqa: E731
        self.f_body = f("SunValleyBodyFont", ("Segoe UI", 10))
        self.f_strong = f("SunValleyBodyStrongFont", ("Segoe UI Semibold", 10))
        self.f_caption = f("SunValleyCaptionFont", ("Segoe UI", 9))
        self.f_title = f("SunValleySubtitleFont", ("Segoe UI Semibold", 15))
        self.border = "#3d3d3d" if dark else "#e3e3e3"     # the outline of a group box
        self.f_group = ("Segoe UI Semibold", 11)
        st.configure("Row.TFrame", background=self.bg)       # rows sit on the window colour: no stray boxes
        st.configure("Help.TLabel", foreground=self.muted, font=self.f_caption)
        st.configure("Strong.TLabel", font=self.f_strong)
        st.configure("Title.TLabel", font=self.f_title)
        st.configure("Group.TLabel", font=self.f_group)
        st.configure("Error.TLabel", foreground=self.error_fg)
        st.configure("Nav.Treeview", rowheight=self.px(34))
        st.configure("Treeview", rowheight=self.px(26))

    def _layout(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = self.outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        bar = ttk.Frame(outer, padding=(self.px(16), self.px(10)))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(outer).pack(side="bottom", fill="x")
        self.save_btn = ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save, width=10)
        self.save_btn.pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.cancel, width=10).pack(side="right", padx=(0, self.px(8)))
        self.msg = ttk.Label(bar, text="", wraplength=self.px(430), justify="left")
        self.msg.pack(side="left", fill="x", expand=True)

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        self.nav = ttk.Treeview(body, show="tree", selectmode="browse", style="Nav.Treeview", takefocus=True)
        self.nav.column("#0", width=self.px(190), stretch=False)
        for name, _ in PAGES:
            self.nav.insert("", "end", iid=name, text="  " + name)
        self.nav.pack(side="left", fill="y", padx=(self.px(12), 0), pady=self.px(12))
        self.nav.bind("<<TreeviewSelect>>", lambda _e: self.show(self.nav.selection()[0]))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        # Scrolling moves the page (a frame) inside a fixed viewport frame. A Tk canvas scrolled by
        # copying screen pixels and repainting only the edges, which on Windows could leave stale bits
        # of controls on screen while scrolling ("half-drawn rows"); moving a frame repaints properly.
        self.viewport = ttk.Frame(right)
        self.viewport.pack(side="left", fill="both", expand=True)
        sb = self.sb = ttk.Scrollbar(right, orient="vertical", command=self.sb_command)
        # The scrollbar lies over the page's right margin (place, not pack), so showing or hiding it
        # never changes the page's width: that used to lay the whole page out again several times.
        self.holder = ttk.Frame(self.viewport, padding=(self.px(20), self.px(14), self.px(22), self.px(16)))
        self.holder.place(x=0, y=0, relwidth=1.0)
        self._offset = 0                 # how far the page is scrolled, in pixels
        self._target = 0                 # where the next frame will scroll it to
        self._scroll_job = None
        self._wheel_delta = 0
        self._wheel_job = None
        self.holder.bind("<Configure>", lambda _e: self._scrolled())
        self.viewport.bind("<Configure>", lambda _e: self._scrolled())
        self.root.bind_all("<MouseWheel>", self._wheel)
        self._no_wheel_on_values()

    def _no_wheel_on_values(self) -> None:
        """Turning the wheel over a dropdown or a number box changed its value (Tk's default), which
        made scrolling the page change settings by accident. Values now only change by clicking or
        typing; the wheel scrolls the page (see _wheel)."""
        for cls in ("TCombobox", "TSpinbox", "Spinbox"):
            for seq in ("<MouseWheel>", "<Shift-MouseWheel>"):
                try:
                    self.root.unbind_class(cls, seq)
                except self.tk.TclError:
                    pass

    # scrolling ---------------------------------------------------------------------------------------
    def _room(self) -> int:
        """How far the page can scroll (0 when it fits)."""
        return max(0, self.holder.winfo_reqheight() - self.viewport.winfo_height())

    def scroll_to(self, y: float) -> None:
        """Scroll the page to `y` px from its top, at most once per frame (a dragged scrollbar or a fast
        wheel asks many times per frame; moving the page each time would repaint it each time)."""
        self._target = max(0, min(int(y), self._room()))
        if self._scroll_job is None:
            self._scroll_job = self.root.after(16, self._apply_scroll)

    def _apply_scroll(self) -> None:
        self._scroll_job = None
        if self._target != self._offset:
            self._offset = self._target
            self.holder.place_configure(y=-self._offset)    # moving the frame repaints what is visible
        self._update_scrollbar()

    def _scrolled(self) -> None:
        """The page or the window changed size: keep the scroll position valid and the scrollbar right."""
        room = self._room()
        if self._offset > room:
            self._offset = self._target = room
            self.holder.place_configure(y=-room)
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        """Only show the scrollbar on a page that doesn't fit, and keep its thumb in place."""
        total = self.holder.winfo_reqheight()
        view = self.viewport.winfo_height()
        need = total > view > 1
        if need:
            self.sb.set(self._offset / total, min(1.0, (self._offset + view) / total))
            if not self.sb.winfo_ismapped():
                self.sb.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        elif self.sb.winfo_ismapped():
            self.sb.place_forget()

    def sb_command(self, *args) -> None:
        """The scrollbar: ("moveto", fraction) while dragging, ("scroll", n, "units"/"pages") for its arrows."""
        if not args:
            return
        if args[0] == "moveto":
            self.scroll_to(float(args[1]) * self.holder.winfo_reqheight())
        elif args[0] == "scroll":
            n, what = int(args[1]), args[2]
            step = self.viewport.winfo_height() * 0.9 if what == "pages" else self.px(48)
            self.scroll_to(self._target + n * step)

    def _wheel(self, e) -> None:
        """The wheel scrolls the page wherever the mouse is: over a dropdown, a number box or a button
        too (their own wheel behaviour, changing the value, is switched off in _no_wheel_on_values).
        Only a list that has more lines than it shows scrolls itself, until it reaches its end."""
        try:
            w = e.widget
            cls = w.winfo_class()
            if ".popdown" in str(w):          # an open dropdown's list scrolls itself
                return
            if cls in ("Treeview", "Listbox"):
                first, last = (float(v) for v in w.yview())
                up = e.delta > 0
                if (up and first > 0.0) or (not up and last < 1.0):
                    return                    # the list itself scrolls (its own binding does it)
        except (AttributeError, ValueError, self.tk.TclError):
            pass
        if self._room() <= 0:
            return
        # A fast wheel sends many events per frame: add them up and scroll once per frame.
        self._wheel_delta += e.delta
        if self._wheel_job is None:
            self._wheel_job = self.root.after(16, self._apply_wheel)

    def _apply_wheel(self) -> None:
        self._wheel_job = None
        delta, self._wheel_delta = self._wheel_delta, 0
        steps = round(-delta / 120) or (-1 if delta > 0 else 1 if delta < 0 else 0)
        if steps:
            self.scroll_to(self._target + steps * self.px(48))      # one notch = about one row

    def show(self, name: str) -> None:
        self._ensure_page(name)
        for n, f in self.pages.items():
            if n != name:
                f.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        if self._scroll_job is not None:          # a new page starts at its top
            self.root.after_cancel(self._scroll_job)
            self._scroll_job = None
        self._offset = self._target = 0
        self.holder.place_configure(y=0)
        self._update_scrollbar()

    def _build_page(self, name: str, groups: list[tuple[str, list[Spec]]]) -> None:
        """A page: its title, then one outlined box per group, rows inside split by thin lines."""
        tk, ttk, px = self.tk, self.ttk, self.px
        page = ttk.Frame(self.holder)
        self.pages[name] = page
        ttk.Label(page, text=name, style="Title.TLabel").pack(anchor="w", pady=(0, px(2)))
        for gi, (group, specs) in enumerate(groups):
            ttk.Label(page, text=group, style="Group.TLabel").pack(anchor="w", pady=(px(14), px(6)))
            outline = tk.Frame(page, background=self.border, borderwidth=0, highlightthickness=0)
            outline.pack(fill="x")
            box = ttk.Frame(outline, style="Row.TFrame")
            box.pack(fill="both", expand=True, padx=1, pady=1)
            for si, spec in enumerate(specs):
                if si:
                    ttk.Separator(box).pack(fill="x", padx=px(14))
                row = ttk.Frame(box, style="Row.TFrame", padding=(px(14), px(10)))
                row.pack(fill="x")
                self._build_row(row, spec)

    def _build_row(self, card, spec: Spec) -> None:
        ttk = self.ttk
        card.columnconfigure(0, weight=1)
        text = ttk.Frame(card, style="Row.TFrame")
        text.grid(row=0, column=0, sticky="w")
        ttk.Label(text, text=spec.label, style="Strong.TLabel").pack(anchor="w")
        if spec.help:
            ttk.Label(text, text=spec.help, style="Help.TLabel", justify="left",
                      wraplength=self.px(540 if spec.kind in BELOW else 290)).pack(anchor="w")
        if spec.kind == "info":             # not a setting: a fact, and maybe buttons next to it
            right = ttk.Frame(card, style="Row.TFrame")
            right.grid(row=0, column=1, sticky="e", padx=(self.px(12), 0))
            try:
                value = ABOUT[spec.fact](self.store.paths)
            except Exception as e:
                value = f"unknown ({type(e).__name__})"
            label = ttk.Label(right, text=value, style="Strong.TLabel", justify="right",
                              wraplength=self.px(290))
            label.pack(side="left")
            self.about = getattr(self, "about", {})
            self.about[spec.fact] = label
            for action, text in spec.choices:
                ttk.Button(right, text=text, command=lambda a=action: self.do_action(a)).pack(
                    side="left", padx=(self.px(8), 0))
            return
        if spec.kind == "action":
            buttons = ttk.Frame(card, style="Row.TFrame")
            buttons.grid(row=0, column=1, sticky="e", padx=(self.px(12), 0))
            for i, (action, label) in enumerate(spec.choices):
                ttk.Button(buttons, text=label, command=lambda a=action: self.do_action(a)).pack(
                    side="left", padx=(self.px(6) if i else 0, 0))
            return
        ctl = CONTROLS[spec.kind](self, card, spec)
        if spec.kind in BELOW:
            ctl.frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(self.px(8), 0))
        else:
            ctl.frame.grid(row=0, column=1, sticky="e", padx=(self.px(12), 0))
        if spec.kind == "learned_auto":     # not a setting: nothing to compare, nothing to save
            self.learned_auto = ctl
            return
        if spec.key is None:
            original = self.env.startup_on
        else:
            original = normalize(spec, self.store.value(spec.key))
        ctl.set(original)
        self.fields.append(Field(spec, ctl, original))

    # actions (buttons that aren't a setting) ------------------------------------------------------------
    def request(self, action: str) -> None:
        """Ask the tray app to do something once this window closes (it reads settings_request.json)."""
        import json
        f = self.store.paths.data_dir / "settings_request.json"
        try:
            actions = json.loads(f.read_text("utf-8")).get("actions", [])
        except (OSError, ValueError, AttributeError):
            actions = []
        if action not in actions:
            actions.append(action)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"actions": actions}), "utf-8")
        except OSError as e:
            self.message(f"Couldn't pass that on to the tray app: {e}", error=True)

    def do_action(self, action: str) -> None:
        paths = self.store.paths
        if action == "move_pill":           # the tray app owns the pill: close (saving first), then it moves
            if self.dirty() and not self.save(close=False):
                return
            self.request(action)
            self.close()
        elif action in ("reset_pill", "search_models", "free_memory"):
            self.request(action)
            self.message({"reset_pill": "The pill goes back to its usual place when this window closes.",
                          "search_models": "The tray app looks for models when this window closes.",
                          "free_memory": "The model is unloaded when this window closes."}[action])
        elif action == "open_log":
            self._open_if_there(paths.log_file, "There is no log yet.")
        elif action == "open_data":
            self._open_if_there(paths.data_dir, "There is no data folder yet.")
        elif action == "open_learned":
            self._open_if_there(paths.learned_file,
                                'No learned words yet. Use "Correct last dictation" to teach some.')
        elif action == "open_file":
            self.open_settings_file()
        elif action == "delete_recordings":     # done here, in this process: the tray app isn't asked
            self.delete_recordings()
        elif action == "open_recordings":
            self._open_if_there(paths.recordings_dir, "No recordings yet.")

    def delete_recordings(self) -> None:
        from . import recordings
        try:
            n = recordings.delete_all(self.store.paths.recordings_dir)
        except OSError as e:
            self.message(f"Couldn't delete the recordings: {e}", error=True)
            return
        self.message(f"Deleted {n} recording{'' if n == 1 else 's'}." if n else "No recordings to delete.")

    def _open_if_there(self, path: Path, missing: str) -> None:
        try:
            there = path.exists() and (not path.is_dir() or any(path.iterdir()))     # an empty folder is nothing
        except OSError:
            there = False
        if not there:
            self.message(missing)
            return
        try:
            self.open_file(path)
        except OSError as e:
            self.message(f"Couldn't open {path}: {e}", error=True)

    # state -----------------------------------------------------------------------------------------
    def field(self, key) -> "Field":
        def find():
            return next((f for f in self.fields
                         if f.spec.key == key or (key == "startup" and f.spec.kind == "startup")), None)
        found = find()
        if found is None:               # on a page not built yet: build it (it stays hidden)
            for name, groups in PAGES:
                if any(s.key == key or (key == "startup" and s.kind == "startup")
                       for _, specs in groups for s in specs):
                    self._ensure_page(name)
                    found = find()
                    break
        if found is None:
            raise StopIteration(key)
        return found

    def changes(self) -> tuple[dict[Key, Any], list[str]]:
        """(changed config keys -> new values, problems with what was typed)."""
        out, errors = {}, []
        for f in self.fields:
            if f.spec.key is None:
                continue
            try:
                v = f.control.get()
            except ValueError as e:
                errors.append(str(e))
                continue
            if v != f.original:
                out[f.spec.key] = v
        return out, errors

    def startup_change(self) -> bool | None:
        f = self.field("startup")
        v = f.control.get()
        return None if v == f.original else v

    def dirty(self) -> bool:
        changes, errors = self.changes()
        return bool(changes or errors or self.startup_change() is not None)

    def refresh_dirty(self, *_):
        if not self.ready:
            return
        self.save_btn.state(["!disabled"] if self.dirty() else ["disabled"])

    def message(self, text: str, error: bool = False) -> None:
        self.msg.configure(text=text, style="Error.TLabel" if error else "TLabel")

    def refresh_models(self) -> None:
        """The model list follows the custom folders as they are edited."""
        try:
            folders = self.field(("model", "folders")).control.get()
            self.field(("model", "dir")).control.reload_choices(folders)
        except StopIteration:
            pass

    # buttons ---------------------------------------------------------------------------------------
    def save(self, close: bool = True) -> bool:
        changes, errors = self.changes()
        if errors:
            self.message(errors[0], error=True)
            return False
        try:
            self.store.save(changes)
        except config.ConfigError as e:
            self.message(f"Not saved: {e}", error=True)
            return False
        except OSError as e:
            self.message(f"Not saved: couldn't write {self.store.file}: {e}", error=True)
            return False
        startup = self.startup_change()
        if startup is not None:
            try:
                self.env.set_startup(startup)
            except Exception as e:
                self.message(f"Settings saved, but Start with Windows couldn't be changed: {e}", error=True)
                for f in self.fields:     # what was saved is the new baseline
                    if f.spec.key is not None:
                        f.original = normalize(f.spec, self.store.value(f.spec.key))
                self.refresh_dirty()
                return False
        log.info("settings saved (%s)", ", ".join(".".join(k) for k in changes) or "start-up only")
        self.saved = True
        if close:
            self.close()
        return True

    def cancel(self) -> None:
        if self.dirty():
            from tkinter import messagebox
            if not messagebox.askyesno(APP_NAME, "Close without saving your changes?", parent=self.root,
                                       icon="warning", default="no"):
                return
        self.close()

    def open_settings_file(self) -> None:
        try:
            self.store.paths.ensure()
            self.open_file(self.store.file)
        except OSError as e:
            self.message(f"Couldn't open {self.store.file}: {e}", error=True)


@dataclass
class Field:
    spec: Spec
    control: Any
    original: Any


BELOW = {"list", "pairs", "commands", "model", "vad", "learned_auto"}   # control under the text, full width


# --- controls: each has .frame, .get() and .set(value) ----------------------------------------------

class Control:
    def __init__(self, win: SettingsWindow, parent, spec: Spec):
        self.win, self.spec = win, spec
        self.tk, self.ttk = win.tk, win.ttk
        self.frame = self.ttk.Frame(parent, style="Row.TFrame")
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    def changed(self, *_):
        self.win.refresh_dirty()


class Switch(Control):
    def build(self):
        self.var = self.tk.BooleanVar(self.win.root)
        self.ttk.Checkbutton(self.frame, variable=self.var, style="Switch.TCheckbutton",
                             command=self.changed).pack()

    def get(self):
        return bool(self.var.get())

    def set(self, v):
        self.var.set(bool(v))


class Choice(Control):
    """A drop-down of (value, label); editable ones also take typed text (the text is the value)."""
    editable = False
    width = 26
    fill = False      # True: as wide as the card (controls shown under their text)

    def build(self):
        self.choices = list(self.spec.choices)
        self.var = self.tk.StringVar(self.win.root)
        self.box = self.ttk.Combobox(self.frame, textvariable=self.var, width=self.width,
                                     state="normal" if self.editable else "readonly")
        self.box.pack(side="left", fill="x" if self.fill else None, expand=self.fill)
        self._fill()
        self.var.trace_add("write", self.changed)

    def _fill(self):
        self.box.configure(values=[label for _, label in self.choices])

    def get(self):
        text = self.var.get()
        for value, label in self.choices:
            if text == label:
                return value
        return text.strip()

    def set(self, v):
        v = "" if v is None else str(v)
        for value, label in self.choices:
            if value == v:
                self.var.set(label)
                return
        if not self.editable:
            self.choices.append((v, v))
            self._fill()
        self.var.set(v)


class Combo(Choice):
    editable = True
    width = 12


class Mic(Choice):
    editable = True
    width = 24

    def build(self):
        self.spec = Spec(self.spec.key, self.spec.kind, self.spec.label, self.spec.help,
                         choices=(("", "Windows default"), *((n, n) for n in (self.win.env.mics or ()))))
        super().build()

    def add_mics(self, names) -> None:
        """The microphone list arrived (it is read in the background); keep what is selected."""
        known = {v for v, _ in self.choices}
        self.choices += [(n, n) for n in names if n not in known]
        self._fill()


class ModelChoice(Choice):
    fill = True

    def build(self):
        super().build()
        self.reload_choices(self.win.store.value(("model", "folders"), []))
        if self.win.env.model_in_use:
            self.ttk.Label(self.frame.master, text=f"Automatic is using: {self.win.env.model_in_use}",
                           style="Help.TLabel", wraplength=self.win.px(560)).grid(row=2, column=0, columnspan=2, sticky="w",
                                                     pady=(self.win.px(4), 0))

    def reload_choices(self, folders) -> None:
        """Rebuild the list (the custom folders changed), keeping the selection."""
        cur = self.get() if self.var.get() else str(self.win.store.value(("model", "dir"), "auto"))
        self.choices = store_mod.model_choices(self.win.env.found_models, folders, cur,
                                               inspect_folders=self.win.env.folder_models)
        self._fill()
        if self.var.get():
            self.set(cur)


class Number(Control):
    integer = True

    def build(self):
        self.var = self.tk.StringVar(self.win.root)
        s = self.spec
        self.ttk.Spinbox(self.frame, from_=s.lo, to=s.hi, increment=s.step, textvariable=self.var,
                         width=8).pack()
        self.var.trace_add("write", self.changed)

    def get(self):
        text = self.var.get().strip().replace(",", ".")
        try:
            return int(text) if self.integer else round(float(text), 3)
        except ValueError:
            raise ValueError(f"{self.spec.label}: enter a {'whole ' if self.integer else ''}number.") from None

    def set(self, v):
        self.var.set(str(v) if self.integer else f"{float(v):g}")


class Decimal(Number):
    integer = False


class Csv(Control):
    def build(self):
        self.var = self.tk.StringVar(self.win.root)
        self.ttk.Entry(self.frame, textvariable=self.var, width=26).pack()
        self.var.trace_add("write", self.changed)

    def get(self):
        return [x.strip() for x in self.var.get().split(",") if x.strip()]

    def set(self, v):
        self.var.set(", ".join(v or []))


class ListEditor(Control):
    """A list of text values: type one and Add, or pick files/folders; Remove takes the selected ones."""

    def build(self):
        ttk, px = self.ttk, self.win.px
        self.tree = ttk.Treeview(self.frame, columns=("v",), show="", height=4, selectmode="extended")
        self.tree.pack(fill="x")
        row = ttk.Frame(self.frame, style="Row.TFrame")
        row.pack(fill="x", pady=(px(6), 0))
        if self.spec.browse:
            if "files" in self.spec.browse:
                ttk.Button(row, text="Add file…", command=self.add_file).pack(side="left", padx=(0, px(6)))
            ttk.Button(row, text="Add folder…", command=self.add_folder).pack(side="left", padx=(0, px(6)))
        else:
            self.entry_var = self.tk.StringVar(self.win.root)
            e = ttk.Entry(row, textvariable=self.entry_var, width=28)
            e.pack(side="left", padx=(0, px(6)))
            e.bind("<Return>", lambda _e: self.add_typed())
            ttk.Button(row, text="Add", command=self.add_typed).pack(side="left", padx=(0, px(6)))
        ttk.Button(row, text="Remove", command=self.remove).pack(side="left")

    def get(self):
        return [self.tree.set(i, "v") for i in self.tree.get_children()]

    def set(self, v):
        self.tree.delete(*self.tree.get_children())
        for x in v or []:
            self.tree.insert("", "end", values=(x,))

    def add(self, text: str) -> None:
        text = text.strip()
        if text and text not in self.get():
            self.tree.insert("", "end", values=(text,))
            self.edited()

    def add_typed(self):
        self.add(self.entry_var.get())
        self.entry_var.set("")

    def add_file(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(parent=self.win.root, title="Add a speech model file",
                                       filetypes=[("Speech models", "*.gguf"), ("All files", "*.*")])
        if p:
            self.add(os.path.normpath(p))

    def add_folder(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(parent=self.win.root, title="Add a folder", mustexist=True)
        if p:
            self.add(os.path.normpath(p))

    def remove(self):
        sel = self.tree.selection()
        if sel:
            self.tree.delete(*sel)
            self.edited()

    def edited(self):
        if self.spec.key == ("model", "folders"):
            self.win.refresh_models()
        self.changed()


class PairsEditor(Control):
    """Two columns, one row per pair; saved as a table. Heard as: term -> [spellings]."""
    headings = ("Term", "Heard as")

    def build(self):
        ttk, px = self.ttk, self.win.px
        self.tree = ttk.Treeview(self.frame, columns=("a", "b"), show="headings", height=5, selectmode="extended")
        for col, title in zip(("a", "b"), self.headings):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=px(220), anchor="w")
        self.tree.pack(fill="x")
        row = ttk.Frame(self.frame, style="Row.TFrame")
        row.pack(fill="x", pady=(px(6), 0))
        self.a = self.tk.StringVar(self.win.root)
        self.b = self.tk.StringVar(self.win.root)
        ttk.Entry(row, textvariable=self.a, width=18).pack(side="left", padx=(0, px(6)))
        self.second(row).pack(side="left", padx=(0, px(6)))
        ttk.Button(row, text="Add", command=self.add_typed).pack(side="left", padx=(0, px(6)))
        ttk.Button(row, text="Remove", command=self.remove).pack(side="left")
        self.tree.bind("<<TreeviewSelect>>", self._pick)

    def second(self, row):
        return self.ttk.Entry(row, textvariable=self.b, width=22)

    def rows(self) -> list[tuple[str, str]]:
        return [(self.tree.set(i, "a"), self.tree.set(i, "b")) for i in self.tree.get_children()]

    def get(self):
        out: dict[str, list[str]] = {}
        for a, b in self.rows():
            out.setdefault(a, []).append(b)
        return out

    def set(self, v):
        self.tree.delete(*self.tree.get_children())
        for a, spellings in (v or {}).items():
            for b in spellings:
                self.tree.insert("", "end", values=(a, b))

    def _pick(self, _e=None):
        sel = self.tree.selection()
        if len(sel) == 1:
            self.a.set(self.tree.set(sel[0], "a"))
            self.b.set(self.tree.set(sel[0], "b"))

    def add_pair(self, a: str, b: str) -> bool:
        a, b = a.strip(), b.strip()
        if not a or not b:
            self.win.message(f"Fill in both {self.headings[0]} and {self.headings[1]}.", error=True)
            return False
        if (a, b) not in self.rows():
            self.tree.insert("", "end", values=(a, b))
        self.win.message("")
        self.changed()
        return True

    def add_typed(self):
        if self.add_pair(self.a.get(), self.b.get()):
            self.a.set("")
            self.b.set("")

    def remove(self):
        sel = self.tree.selection()
        if sel:
            self.tree.delete(*sel)
            self.changed()


class CommandsEditor(PairsEditor):
    """Phrase -> one action; a phrase added again replaces its action."""
    headings = ("Phrase", "Does")

    def second(self, row):
        self.b.set(ACTIONS[0])
        return self.ttk.Combobox(row, textvariable=self.b, values=ACTIONS, state="readonly", width=12)

    def get(self):
        return {a: b for a, b in self.rows()}

    def set(self, v):
        super().set({k: [x] for k, x in (v or {}).items()})

    def add_pair(self, a: str, b: str) -> bool:
        for i in self.tree.get_children():
            if self.tree.set(i, "a").strip().lower() == a.strip().lower():
                self.tree.delete(i)
        return super().add_pair(a, b)

    def add_typed(self):
        if self.add_pair(self.a.get(), self.b.get()):
            self.a.set("")


class VadChoice(Combo):
    fill = True

    def build(self):
        self.spec = Spec(self.spec.key, self.spec.kind, self.spec.label, self.spec.help,
                         choices=(("auto", "auto"), ("off", "off")))
        super().build()
        self.ttk.Button(self.frame, text="Browse…", command=self.browse).pack(side="left",
                                                                             padx=(self.win.px(6), 0))

    def browse(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(parent=self.win.root, title="Voice detector model",
                                       filetypes=[("Voice detector", "*.gguf"), ("All files", "*.*")])
        if p:
            self.var.set(os.path.normpath(p))


class Startup(Switch):
    pass


class HotkeyRecorder(Control):
    """Shows the shortcut; "Change…" listens for the next combination (Esc cancels, Backspace = default)."""

    def build(self):
        ttk, px = self.ttk, self.win.px
        self.spec_value = ""
        self.listening = False
        top = ttk.Frame(self.frame, style="Row.TFrame")
        top.pack(anchor="e")
        self.shown = ttk.Label(top, text="", style="Strong.TLabel", width=16, anchor="e")
        self.shown.pack(side="left", padx=(0, px(10)))
        self.button = ttk.Button(top, text="Change…", command=self.start, width=10)
        self.button.pack(side="left")
        self.status = ttk.Label(self.frame, text="", style="Help.TLabel", wraplength=px(240), justify="right")
        self.status.pack(anchor="e")
        self.shown.bind("<KeyPress>", self._key)
        self.shown.bind("<KeyRelease>", lambda _e: "break" if self.listening else None)
        self.shown.bind("<FocusOut>", lambda _e: self.stop("") if self.listening else None)

    def get(self):
        return self.spec_value

    default = store_mod.DEFAULT_HOTKEY      # what Backspace goes back to ("" = no shortcut)

    def set(self, v):
        self.spec_value = str(v or "")
        self.shown.configure(text=self._shown_text())

    def _shown_text(self) -> str:
        return hotkey.display(self.spec_value) if self.spec_value else "None"

    def start(self):
        self.listening = True
        self.shown.configure(text="Press keys…")
        back = f"goes back to {hotkey.display(self.default)}" if self.default else "removes the shortcut"
        self.status.configure(text=f"Press the new shortcut. Esc cancels, Backspace {back}.",
                              style="Help.TLabel")
        self.button.state(["disabled"])
        self.shown.focus_set()

    def stop(self, text: str, error: bool = False):
        self.listening = False
        self.button.state(["!disabled"])
        self.shown.configure(text=self._shown_text())
        self.status.configure(text=text, style="Error.TLabel" if error else "Help.TLabel")

    def others(self) -> dict[str, str]:
        """The other shortcuts in the window right now, by name ("Dictate", "Copy last dictation")."""
        return {f.spec.label: f.control.get() for f in self.win.fields
                if f.spec.kind in ("hotkey", "hotkey_opt") and f.control is not self}

    @staticmethod
    def held_modifiers() -> set[str]:
        down = hotkey.key_down
        mods = {m for m, vks in (("ctrl", (0x11,)), ("alt", (0x12,)), ("shift", (0x10,)), ("win", (0x5B, 0x5C)))
                if any(down(vk) for vk in vks)}
        return mods

    def _key(self, e):
        if not self.listening:
            return None
        self.handle(e.keycode, self.held_modifiers())   # on Windows, Tk's keycode is the virtual-key code
        return "break"      # nothing else reacts while recording (Space on a button, Alt to a menu, Tab)

    def handle(self, vk: int, mods: set[str]) -> None:
        """One key press while listening (the Tk binding calls this; tests call it directly)."""
        held = str(self.win.store.value(self.spec.key, "") or "")      # what the running tray app holds
        r = store_mod.record_key(vk, mods, held, self.win.env.available, self.default, self.others())
        if r.action == "listen":
            held = [m.capitalize() for m in hotkey.MOD_ORDER if m in mods]
            self.shown.configure(text="+".join(held + ["…"]))
        elif r.action == "cancel":
            self.stop("")
        elif r.action == "error":
            self.status.configure(text=r.message + " Try again, or Esc to cancel.", style="Error.TLabel")
            self.shown.configure(text="Press keys…")
        else:
            self.set(r.spec)
            self.stop(r.message)
            self.changed()


class OptionalHotkeyRecorder(HotkeyRecorder):
    """An extra shortcut: none until one is recorded; Backspace removes it."""
    default = ""


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class LearnedAuto(Control):
    """The fixes background learning made by itself: a status line, one row per fix with an Undo button,
    and Reset all (asked again in the window first).

    Not a setting. It reads and changes the learning files through autolearn.Store, the same files the
    tray app's checker writes (every write re-reads them under a file lock), so it has no get/set, never
    makes the window dirty, and nothing here is saved with the settings."""
    BUSY = "Couldn't change the learned fixes right now. Try again."

    def build(self):
        from . import autolearn
        px = self.win.px
        paths = self.win.store.paths
        self.auto = autolearn.Store(paths.learning_dir, paths.learned_auto_file)
        self.status = self.ttk.Label(self.frame, wraplength=px(540), justify="left")
        self.status.pack(anchor="w")
        self.listing = self.ttk.Frame(self.frame, style="Row.TFrame")
        self.listing.pack(fill="x", pady=(px(6), px(4)))
        self.footer = self.ttk.Frame(self.frame, style="Row.TFrame")
        self.footer.pack(fill="x")
        self.refresh()

    def refresh(self) -> None:
        """Read the learning files again and redraw everything."""
        ttk, px = self.ttk, self.win.px
        fixes, waiting = self.auto.active(), self.auto.pending_count()
        self.status.configure(text=" · ".join([
            self.auto.status() or "Not started yet",
            _plural(len(self.auto.checked_ids()), "dictation", "dictations") + " checked",
            _plural(len(fixes), "fix", "fixes") + " learned",
            f"{waiting} waiting for a second sighting"]))
        for child in (*self.listing.winfo_children(), *self.footer.winfo_children()):
            child.destroy()
        if not fixes:
            ttk.Label(self.listing, text="No fixes learned yet.", style="Help.TLabel").pack(anchor="w")
        for fix in fixes:
            self._row(fix)
        reset = ttk.Button(self.footer, text="Reset all", command=self.ask_reset)
        reset.pack(anchor="w", pady=(px(4), 0))
        if not (fixes or waiting):
            reset.state(["disabled"])       # nothing to clear

    def _row(self, fix) -> None:
        """heard → term, the day it was learned, how often it was seen, Undo."""
        ttk, px = self.ttk, self.win.px
        row = ttk.Frame(self.listing, style="Row.TFrame")
        row.pack(fill="x", pady=px(2))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, minsize=px(92))
        row.columnconfigure(2, minsize=px(70))
        ttk.Label(row, text=self.shown(fix), wraplength=px(290), justify="left").grid(row=0, column=0, sticky="w")
        ttk.Label(row, text=fix.first_learned[:10], style="Help.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(row, text=f"seen ×{fix.seen}", style="Help.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Button(row, text="Undo", command=lambda: self.undo(fix)).grid(row=0, column=3, padx=(px(12), 0))

    @staticmethod
    def shown(fix) -> str:
        return f"{fix.heard_as} → {fix.term}" if fix.heard_as else fix.term

    def undo(self, fix) -> None:
        self._change(lambda: self.auto.undo((fix.term, fix.heard_as)),
                     f"Undone: {self.shown(fix)}. It won't be learned again.")

    def ask_reset(self) -> None:
        """Turn the Reset all button into the question, in the window (no pop-up)."""
        ttk, px = self.ttk, self.win.px
        for child in self.footer.winfo_children():
            child.destroy()
        row = ttk.Frame(self.footer, style="Row.TFrame")
        row.pack(anchor="w", pady=(px(4), 0))
        ttk.Label(row, text="Really reset all?", style="Strong.TLabel").pack(side="left")
        ttk.Button(row, text="Yes", command=self.reset).pack(side="left", padx=(px(10), 0))
        cancel = ttk.Button(row, text="Cancel", command=self.refresh)
        cancel.pack(side="left", padx=(px(6), 0))
        cancel.focus_set()                  # the safe answer is the one Enter gives

    def reset(self) -> None:
        self._change(self.auto.reset_all, "Every automatic fix was cleared. None will be learned again.")

    def _change(self, change: Callable[[], None], done: str) -> None:
        try:
            change()
        except OSError:     # the lock stayed busy (TimeoutError) or the disk refused; damage is Store's job
            self.win.message(self.BUSY, error=True)
        else:
            self.win.message(done)
        self.refresh()


CONTROLS: dict[str, type[Control]] = {
    "hotkey_opt": OptionalHotkeyRecorder,
    "switch": Switch, "startup": Startup, "choice": Choice, "combo": Combo, "mic": Mic, "model": ModelChoice,
    "int": Number, "float": Decimal, "csv": Csv, "list": ListEditor, "pairs": PairsEditor,
    "commands": CommandsEditor, "vad": VadChoice, "hotkey": HotkeyRecorder, "learned_auto": LearnedAuto,
}


def make_root(theme: str | None = None):
    """A themed Tk root (Windows light/dark unless `theme` is given), not yet shown."""
    import tkinter as tk

    import sv_ttk
    root = tk.Tk()
    root.withdraw()
    try:
        sv_ttk.set_theme(theme or ("light" if windows_uses_light_theme() else "dark"), root)
    except Exception:
        log.exception("settings: theme")
    return root


def run(paths: config.Paths) -> int:
    """Entry point of the --settings process. Exit code 0 always; the tray app checks the file itself."""
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)    # crisp text on scaled screens
    except (AttributeError, OSError):
        pass
    root = make_root()
    root.title(f"{APP_NAME} settings")
    try:
        from .tray import write_ico
        root.iconbitmap(default=str(write_ico(paths.data_dir / "icon.ico")))
    except Exception:
        pass
    win = SettingsWindow(root, Store(paths), Env.real(paths))
    w, h = win.px(860), win.px(600)
    x = max(0, (root.winfo_screenwidth() - w) // 2)
    y = max(0, (root.winfo_screenheight() - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.minsize(win.px(700), win.px(460))
    root.protocol("WM_DELETE_WINDOW", win.cancel)
    recording = lambda: any(getattr(f.control, "listening", False) for f in win.fields)   # noqa: E731
    root.bind("<Escape>", lambda _e: None if recording() else win.cancel())
    root.deiconify()
    root.attributes("-topmost", True)     # opened from the tray: make sure it isn't hidden behind
    root.after(200, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    return 0
