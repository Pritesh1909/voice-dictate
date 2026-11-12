"""Settings: a user-editable TOML file in %APPDATA%, layered over the shipped defaults."""
from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_ID
from .commands import ACTIONS, normalize_phrase

DEFAULT_FILE = Path(__file__).with_name("config.default.toml")


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
    model_dir: str          # "auto", a .gguf file or a folder holding one (environment variables expanded)
    device: str             # auto | gpu | igpu | cpu
    threads: int
    unload_after_seconds: int
    process_while_speaking: bool
    pause_seconds: float
    vad: str                # "auto", "off", or a .gguf path (environment variables expanded)
    hotwords: tuple[str, ...]
    normalize_audio: bool
    fillers: tuple[str, ...]
    remove_repeated_words: bool
    repeated_words_allowed: tuple[str, ...]
    replacements: tuple[tuple[str, str], ...]
    dictionary: tuple[tuple[str, str], ...]   # (term, heard_as or "")
    output_method: str
    type_while_speaking: bool
    add_trailing_space: bool
    sounds: bool
    max_seconds: int
    log_transcripts: bool
    overlay: bool
    overlay_position: str
    input_device: str
    silence_stop_seconds: int  # 0 = off
    commands_enabled: bool
    commands: tuple[tuple[str, str], ...]          # (normalized phrase, action)
    context_enabled: bool
    context_window_title: bool
    context_folders: tuple[str, ...]               # environment variables expanded
    context_max_terms: int
    context_refresh_minutes: int
    apps: tuple[tuple[str, AppOverride], ...]      # (exe name, lower case, without ".exe"; overrides)
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
        model_dir=_model_dir(_get(d, "model", "dir", str)),
        device=_get(d, "model", "device", str, lambda v: v in ("auto", "gpu", "igpu", "cpu")),
        threads=_get(d, "model", "threads", int, lambda v: 1 <= v <= 32),
        unload_after_seconds=_get(d, "model", "unload_after_seconds", int, lambda v: v >= -1),
        process_while_speaking=_get(d, "model", "process_while_speaking", bool),
        pause_seconds=_get(d, "model", "pause_seconds", float, lambda v: 0.3 <= v <= 5),
        vad=_vad(_get(d, "model", "vad", str)),
        hotwords=_str_list(d, "accuracy", "hotwords"),
        normalize_audio=_get(d, "accuracy", "normalize_audio", bool),
        fillers=_str_list(d, "cleanup", "fillers"),
        remove_repeated_words=_get(d, "cleanup", "remove_repeated_words", bool),
        repeated_words_allowed=_str_list(d, "cleanup", "repeated_words_allowed"),
        replacements=tuple((k, v) for k, v in repl.items()),
        dictionary=_dictionary(d),
        output_method=_get(d, "output", "method", str, lambda v: v in ("type", "paste")),
        type_while_speaking=_get(d, "output", "type_while_speaking", bool),
        add_trailing_space=_get(d, "output", "add_trailing_space", bool),
        sounds=_get(d, "output", "sounds", bool),
        max_seconds=_get(d, "output", "max_seconds", int, lambda v: 5 <= v <= 1800),
        log_transcripts=_get(d, "output", "log_transcripts", bool),
        overlay=_get(d, "overlay", "enabled", bool),
        overlay_position=_get(d, "overlay", "position", str, lambda v: v in ("top", "bottom")),
        input_device=_get(d, "audio", "input_device", str),
        silence_stop_seconds=_get(d, "audio", "silence_stop_seconds", int, lambda v: v == 0 or 5 <= v <= 3600),
        commands_enabled=_get(d, "commands", "enabled", bool),
        commands=_commands(d),
        context_enabled=_get(d, "context", "enabled", bool),
        context_window_title=_get(d, "context", "window_title", bool),
        context_folders=tuple(os.path.expandvars(f) for f in _str_list(d, "context", "folders")),
        context_max_terms=_get(d, "context", "max_terms", int, lambda v: 0 <= v <= 500),
        context_refresh_minutes=_get(d, "context", "refresh_minutes", int, lambda v: 1 <= v <= 1440),
        apps=_apps(d),
    )


def _dictionary(d: dict) -> tuple[tuple[str, str], ...]:
    terms = _str_list(d, "dictionary", "terms")
    heard = d.get("dictionary", {}).get("heard_as", {})
    if not isinstance(heard, dict):
        raise ConfigError("[dictionary.heard_as] entries must look like  \"Term\" = [\"heard\", ...]")
    out = [(t, "") for t in terms]
    for term, v in heard.items():
        spellings = [v] if isinstance(v, str) else v
        if not isinstance(spellings, list) or not all(isinstance(x, str) for x in spellings):
            raise ConfigError(f"[dictionary.heard_as] {term!r} must be text or a list of text values")
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


def app_key(exe: str) -> str:
    """How [apps] names are matched: case-insensitive, with or without ".exe"."""
    exe = exe.strip().lower()
    return exe[:-4] if exe.endswith(".exe") else exe


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


def _vad(v: str) -> str:
    v = v.strip()
    if v.lower() in ("", "auto", "off"):
        return v.lower() or "auto"
    return os.path.expandvars(v)
