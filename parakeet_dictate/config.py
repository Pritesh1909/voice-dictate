"""Settings: a user-editable TOML file in %APPDATA%, layered over the shipped defaults."""
from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_ID
from .commands import ACTIONS, normalize_phrase

DEFAULT_FILE = Path(__file__).with_name("config.default.toml")


# Where the indicator sits: centred at the top or bottom, or in a corner.
POSITIONS = ("top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "dictate.log"

    @property
    def learned_file(self) -> Path:
        """Dictionary words learned from "Correct last dictation…" (kept out of the user's config.toml)."""
        return self.config_dir / "learned.toml"

    @property
    def context_cache(self) -> Path:
        """Terms harvested from the [context] folders, with the file times they came from."""
        return self.data_dir / "context_terms.json"

    @property
    def history_file(self) -> Path:
        """Recent dictations, only with [history] keep = "disk"."""
        return self.data_dir / "history.jsonl"

    @property
    def recordings_dir(self) -> Path:
        """Kept recordings (a .wav and a .json record each), only with [learning] keep_recordings."""
        return self.data_dir / "recordings"

    @property
    def learning_dir(self) -> Path:
        """What the background checker keeps between runs (its state and the list of recordings it has read)."""
        return self.data_dir / "learning"

    @property
    def teacher_dir(self) -> Path:
        """The Whisper model the background checker listens with (not a dictation model: not in the Model menu)."""
        return self.data_dir / "teacher"

    @property
    def learned_auto_file(self) -> Path:
        """Fixes found by the background checker (learned.toml stays the user's own)."""
        return self.config_dir / "learned_auto.toml"

    @classmethod
    def default(cls) -> "Paths":
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return cls(roaming / APP_ID, local / APP_ID)

    def ensure(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_file.exists():
            shutil.copyfile(DEFAULT_FILE, self.config_file)


@dataclass(frozen=True)
class AppOverride:
    """[apps."<exe>"]: output settings for one program. None = use the global setting."""
    method: str | None = None
    add_trailing_space: bool | None = None
    type_while_speaking: bool | None = None
    press_enter: bool | None = None
    commands: bool | None = None


APP_OVERRIDE_KEYS = {"method": str, "add_trailing_space": bool, "type_while_speaking": bool,
                     "press_enter": bool, "commands": bool}


@dataclass(frozen=True)
class Config:
    hotkey: str
    hold_threshold: float
    esc_cancels: bool
    copy_last_hotkey: str     # copies the last dictation to the clipboard; "" = no shortcut
    correct_last_hotkey: str  # opens "Correct last dictation"; "" = no shortcut
    history_hotkey: str       # opens the History window; "" = no shortcut
    model_dir: str          # "auto", a .gguf file or a folder holding one (environment variables expanded)
    model_folders: tuple[str, ...]   # [model] folders: more .gguf files / folders to list (variables expanded)
    device: str             # auto | gpu | igpu | cpu
    threads: int
    unload_after_seconds: int
    process_while_speaking: bool
    stream_while_speaking: bool   # type words before the pause (re-reads the open phrase)
    pause_seconds: float
    vad: str                # "auto", "off", or a .gguf path (environment variables expanded)
    language: str           # "auto" or an ISO-639-1 code ("de"); passed to models that take one
    hotwords: tuple[str, ...]
    normalize_audio: bool
    noise_reduction: str    # off | auto | on: turn down steady background noise before transcribing
    fillers: tuple[str, ...]
    language_fillers: bool
    remove_repeated_words: bool
    repeated_words_allowed: tuple[str, ...]
    numbers: bool                                  # spoken numbers as digits where clearly numeric
    id_prefixes: tuple[str, ...]                   # ticket prefixes, upper case: "SEC twelve" -> "SEC-12"
    replacements: tuple[tuple[str, str], ...]
    dictionary: tuple[tuple[str, str], ...]   # (term, heard_as or "")
    app_dictionaries: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]   # (exe in lower case, terms)
    output_method: str
    type_while_speaking: bool
    add_trailing_space: bool
    press_enter: bool
    sounds: bool
    max_seconds: int
    log_transcripts: bool
    overlay: bool
    overlay_position: str
    overlay_margin: int     # pixels from the screen edge; -1 = default
    overlay_style: str      # "pill" (long, with live words) or "button" (round mic button)
    overlay_always: bool    # button style: stay on screen when idle (click to start dictating)
    input_device: str
    silence_stop_seconds: int  # 0 = off
    commands_enabled: bool
    commands: tuple[tuple[str, str], ...]          # (normalized phrase, action)
    context_enabled: bool
    context_window_title: bool
    context_folders: tuple[str, ...]               # environment variables expanded
    context_max_terms: int
    context_refresh_minutes: int
    context_code_apps: tuple[str, ...]             # app_key() names
    apps: tuple[tuple[str, AppOverride], ...]      # (exe name, lower case, without ".exe"; overrides)
    history_keep: str        # off | session (memory only) | disk (history.jsonl)
    history_max_items: int
    learning_enabled: bool   # check kept recordings in the background and learn fixes from them
    keep_recordings: bool    # keep each dictation's audio (and a record of it) in recordings/
    keep_days: int           # delete kept recordings older than this
    max_recordings_mb: int   # keep at most this much audio, the oldest deleted first
    idle_minutes: int        # the checker waits for this long without keyboard or mouse input
    use_claude: bool         # may send text (never audio) to Claude to confirm a fix
    claude_calls_per_day: int   # 0 = never ask Claude
    source_mtime: float = field(default=0.0, compare=False)


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("replacements", "heard_as", "phrases"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _get(d: dict, section: str, key: str, typ, check=None):
    try:
        v = d[section][key]
    except KeyError:
        raise ConfigError(f"[{section}] {key} is missing") from None
    if typ is float and isinstance(v, int) and not isinstance(v, bool):
        v = float(v)
    if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
        raise ConfigError(f"[{section}] {key} must be {typ.__name__}, got {v!r}")
    if check and not check(v):
        raise ConfigError(f"[{section}] {key} has an invalid value: {v!r}")
    return v


def _str_list(d: dict, section: str, key: str) -> tuple[str, ...]:
    v = _get(d, section, key, list)
    if not all(isinstance(x, str) for x in v):
        raise ConfigError(f"[{section}] {key} must be a list of text values")
    return tuple(x for x in (s.strip() for s in v) if x)


def _id_prefixes(v: tuple[str, ...]) -> tuple[str, ...]:
    bad = [p for p in v if not re.fullmatch(r"[A-Za-z]{1,10}", p)]
    if bad:
        raise ConfigError(f"[cleanup] id_prefixes must be letters only (like \"SEC\"), got {bad[0]!r}")
    return tuple(dict.fromkeys(p.upper() for p in v))


def parse(text: str, defaults_text: str | None = None) -> Config:
    base = tomllib.loads(defaults_text if defaults_text is not None else DEFAULT_FILE.read_text("utf-8"))
    try:
        user = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml is not valid TOML: {e}") from None
    d = _merge(base, user)
    repl = d.get("cleanup", {}).get("replacements", {})
    if not isinstance(repl, dict) or not all(isinstance(v, str) for v in repl.values()):
        raise ConfigError("[cleanup.replacements] entries must look like  \"heard\" = \"wanted\"")
    return Config(
        hotkey=_get(d, "hotkey", "keys", str),
        hold_threshold=_get(d, "hotkey", "hold_threshold", float, lambda v: 0 <= v <= 5),
        esc_cancels=_get(d, "hotkey", "esc_cancels", bool),
        copy_last_hotkey=_optional_hotkey(d, "copy_last"),
        correct_last_hotkey=_optional_hotkey(d, "correct_last"),
        history_hotkey=_optional_hotkey(d, "history"),
        model_dir=_model_dir(_get(d, "model", "dir", str)),
        model_folders=_model_folders(_str_list(d, "model", "folders")),
        device=_get(d, "model", "device", str, lambda v: v in ("auto", "gpu", "igpu", "cpu")),
        threads=_get(d, "model", "threads", int, lambda v: 1 <= v <= 32),
        unload_after_seconds=_get(d, "model", "unload_after_seconds", int, lambda v: v >= -1),
        process_while_speaking=_get(d, "model", "process_while_speaking", bool),
        stream_while_speaking=_get(d, "model", "stream_while_speaking", bool),
        pause_seconds=_get(d, "model", "pause_seconds", float, lambda v: 0.3 <= v <= 5),
        vad=_vad(_get(d, "model", "vad", str)),
        language=_language(_get(d, "model", "language", str)),
        hotwords=_str_list(d, "accuracy", "hotwords"),
        normalize_audio=_get(d, "accuracy", "normalize_audio", bool),
        noise_reduction=_get(d, "accuracy", "noise_reduction", str, lambda v: v in ("off", "auto", "on")),
        fillers=_str_list(d, "cleanup", "fillers"),
        language_fillers=_get(d, "cleanup", "language_fillers", bool),
        remove_repeated_words=_get(d, "cleanup", "remove_repeated_words", bool),
        repeated_words_allowed=_str_list(d, "cleanup", "repeated_words_allowed"),
        numbers=_get(d, "cleanup", "numbers", bool),
        id_prefixes=_id_prefixes(_str_list(d, "cleanup", "id_prefixes")),
        replacements=tuple((k, v) for k, v in repl.items()),
        dictionary=_dictionary(d.get("dictionary", {}), "dictionary"),
        app_dictionaries=_app_dictionaries(d),
        output_method=_get(d, "output", "method", str, lambda v: v in ("type", "paste")),
        type_while_speaking=_get(d, "output", "type_while_speaking", bool),
        add_trailing_space=_get(d, "output", "add_trailing_space", bool),
        press_enter=_get(d, "output", "press_enter", bool),
        sounds=_get(d, "output", "sounds", bool),
        max_seconds=_get(d, "output", "max_seconds", int, lambda v: 5 <= v <= 1800),
        log_transcripts=_get(d, "output", "log_transcripts", bool),
        overlay=_get(d, "overlay", "enabled", bool),
        overlay_position=_get(d, "overlay", "position", str, lambda v: v in POSITIONS),
        overlay_margin=_get(d, "overlay", "margin", int, lambda v: -1 <= v <= 600),
        overlay_style=_get(d, "overlay", "style", str, lambda v: v in ("pill", "button")),
        overlay_always=_get(d, "overlay", "button_visible", str, lambda v: v in ("always", "dictating"))
        == "always",
        input_device=_get(d, "audio", "input_device", str),
        silence_stop_seconds=_get(d, "audio", "silence_stop_seconds", int, lambda v: v == 0 or 5 <= v <= 3600),
        commands_enabled=_get(d, "commands", "enabled", bool),
        commands=_commands(d),
        context_enabled=_get(d, "context", "enabled", bool),
        context_window_title=_get(d, "context", "window_title", bool),
        context_folders=tuple(os.path.expandvars(f) for f in _str_list(d, "context", "folders")),
        context_max_terms=_get(d, "context", "max_terms", int, lambda v: 0 <= v <= 500),
        context_refresh_minutes=_get(d, "context", "refresh_minutes", int, lambda v: 1 <= v <= 1440),
        context_code_apps=tuple(app_key(x) for x in _str_list(d, "context", "code_apps")),
        apps=_apps(d),
        history_keep=_get(d, "history", "keep", str, lambda v: v in ("off", "session", "disk")),
        history_max_items=_get(d, "history", "max_items", int, lambda v: 10 <= v <= 5000),
        learning_enabled=_get(d, "learning", "enabled", bool),
        keep_recordings=_get(d, "learning", "keep_recordings", bool),
        keep_days=_get(d, "learning", "keep_days", int, lambda v: 1 <= v <= 365),
        max_recordings_mb=_get(d, "learning", "max_recordings_mb", int, lambda v: 100 <= v <= 20000),
        idle_minutes=_get(d, "learning", "idle_minutes", int, lambda v: 1 <= v <= 120),
        use_claude=_get(d, "learning", "use_claude", bool),
        claude_calls_per_day=_get(d, "learning", "claude_calls_per_day", int, lambda v: 0 <= v <= 200),
    )


def _dictionary(table: dict, where: str) -> tuple[tuple[str, str], ...]:
    terms = _str_list({where: table}, where, "terms") if "terms" in table else ()
    heard = table.get("heard_as", {})
    if not isinstance(heard, dict):
        raise ConfigError(f"[{where}.heard_as] entries must look like  \"Term\" = [\"heard\", ...]")
    out = [(t, "") for t in terms]
    for term, v in heard.items():
        spellings = [v] if isinstance(v, str) else v
        if not isinstance(spellings, list) or not all(isinstance(x, str) for x in spellings):
            raise ConfigError(f"[{where}.heard_as] {term!r} must be text or a list of text values")
        out.append((term, ""))
        out += [(term, x.strip()) for x in spellings if x.strip()]
    return tuple(out)


def _commands(d: dict) -> tuple[tuple[str, str], ...]:
    phrases = d.get("commands", {}).get("phrases", {})
    if not isinstance(phrases, dict):
        raise ConfigError('[commands.phrases] entries must look like  "new line" = "newline"')
    out = []
    for phrase, action in phrases.items():
        if action not in ACTIONS:
            raise ConfigError(f"[commands.phrases] {phrase!r} must be one of {', '.join(ACTIONS)}, got {action!r}")
        if not normalize_phrase(phrase):
            raise ConfigError(f"[commands.phrases] {phrase!r} has no words")
        out.append((normalize_phrase(phrase), action))
    return tuple(out)


def _app_dictionaries(d: dict) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """[dictionary.apps."code.exe"] tables: terms that apply only while dictating into that program."""
    apps = d.get("dictionary", {}).get("apps", {})
    if not isinstance(apps, dict):
        raise ConfigError('[dictionary.apps] must hold tables like  [dictionary.apps."code.exe"]')
    out = []
    for exe, table in apps.items():
        where = f'dictionary.apps."{exe}"'
        if not isinstance(table, dict):
            raise ConfigError(f"[{where}] must be a table with terms = [...] and/or a heard_as table")
        name = app_key(exe)
        if not name:
            raise ConfigError(f"[{where}] needs a program name such as \"code.exe\"")
        out.append((name, _dictionary(table, where)))
    return tuple(out)


def app_key(exe: str) -> str:
    """How a program is matched: its file name in lower case, ".exe" added when left out ("Code" = "code.exe")."""
    name = exe.strip().replace("/", "\\").rsplit("\\", 1)[-1].lower()
    return name if not name or "." in name else name + ".exe"


def _apps(d: dict) -> tuple[tuple[str, AppOverride], ...]:
    apps = d.get("apps", {})
    if not isinstance(apps, dict):
        raise ConfigError('[apps] must hold tables like  [apps."idea64.exe"]')
    out = []
    for exe, table in apps.items():
        where = f'[apps."{exe}"]'
        if not isinstance(table, dict) or not app_key(exe):
            raise ConfigError(f"{where} must be a table of settings")
        for k, v in table.items():
            typ = APP_OVERRIDE_KEYS.get(k)
            if typ is None:
                raise ConfigError(f"{where} {k} is not a setting that can be changed per app "
                                  f"(these can: {', '.join(APP_OVERRIDE_KEYS)})")
            if not isinstance(v, typ):
                raise ConfigError(f"{where} {k} must be {typ.__name__}, got {v!r}")
        if table.get("method", "type") not in ("type", "paste"):
            raise ConfigError(f'{where} method must be "type" or "paste"')
        out.append((app_key(exe), AppOverride(**table)))
    return tuple(out)


def load(paths: Paths) -> Config:
    f = paths.config_file
    cfg = parse(f.read_text("utf-8-sig"))
    return Config(**{**cfg.__dict__, "source_mtime": f.stat().st_mtime})


def _model_dir(v: str) -> str:
    v = v.strip()
    return "auto" if v.lower() in ("", "auto") else os.path.expandvars(v)


def _model_folders(v: tuple[str, ...]) -> tuple[str, ...]:
    """[model] folders with environment variables and ~ expanded, each once (capitals don't matter)."""
    seen, out = set(), []
    for x in v:
        p = os.path.expanduser(os.path.expandvars(x))
        if "\n" in p or "\0" in p:
            raise ConfigError(f"[model] folders has an invalid path: {x!r}")
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return tuple(out)


def _language(v: str) -> str:
    v = v.strip().lower()
    if v in ("", "auto"):
        return "auto"
    if not re.fullmatch(r"[a-z]{2,3}", v):
        raise ConfigError(f'[model] language must be "auto" or a language code such as "en" or "de", got {v!r}')
    return v


def _optional_hotkey(d: dict, key: str) -> str:
    """An extra shortcut: "" (none) or a combination like "ctrl+alt+c"."""
    from .hotkey import HotkeyError, parse
    v = _get(d, "hotkey", key, str).strip()
    if v:
        try:
            parse(v)
        except HotkeyError as e:
            raise ConfigError(f"[hotkey] {key}: {e}") from None
    return v


def _vad(v: str) -> str:
    v = v.strip()
    if v.lower() in ("", "auto", "off"):
        return v.lower() or "auto"
    return os.path.expandvars(v)
