"""Reading and saving the Settings window's values, without any Tk.

The window shows the *effective* settings (the shipped defaults with the user's config.toml on top) and
saves only the keys the user changed back into config.toml, through tomlkit so every comment, the order
and every untouched value stay exactly as they were. The new text is checked with config.parse() before
anything is written, and written atomically (a temp file next to it, then a rename), so a mistake can
never leave a broken or half-written settings file behind.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import tomlkit
from tomlkit.items import Table

from . import config, hotkey

Key = tuple[str, ...]
DEFAULT_HOTKEY = "ctrl+q"


def _plain(v: Any) -> Any:
    """tomlkit/tomllib values -> plain dicts, lists, str, int, float, bool."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if hasattr(v, "unwrap"):
        return v.unwrap()
    return v


def _toml_value(v: Any):
    """A plain value as a tomlkit item; Windows paths become 'literal' strings so backslashes stay single."""
    if isinstance(v, str):
        literal = "\\" in v and "'" not in v and "\n" not in v
        return tomlkit.string(v, literal=literal)
    if isinstance(v, list):
        arr = tomlkit.array()
        for x in v:
            arr.append(_toml_value(x))
        if len(v) > 4 or sum(len(str(x)) + 4 for x in v) > 90:
            arr.multiline(True)
        return arr
    if isinstance(v, dict):
        t = tomlkit.table()
        for k, x in v.items():
            t[k] = _toml_value(x)
        return t
    return v


class Store:
    """The user's config.toml, the defaults under it, and saving changes back."""

    def __init__(self, paths: config.Paths, defaults_text: str | None = None):
        self.paths = paths
        self.defaults_text = defaults_text if defaults_text is not None else config.DEFAULT_FILE.read_text("utf-8")
        self.reload()

    @property
    def file(self) -> Path:
        return self.paths.config_file

    def _read_user(self) -> str:
        try:
            return self.file.read_text("utf-8-sig")
        except FileNotFoundError:
            return self.defaults_text      # Paths.ensure() normally copies the defaults; start from them

    def reload(self) -> None:
        self.user_text = self._read_user()
        base = tomllib.loads(self.defaults_text)
        try:
            user = tomllib.loads(self.user_text)
        except tomllib.TOMLDecodeError:
            user = {}          # the window still opens; Save then reports the file's error
        self.effective = config._merge(base, user)
        self.config_error: str | None = None
        try:
            self.config = config.parse(self.user_text, self.defaults_text)
        except config.ConfigError as e:
            self.config, self.config_error = None, str(e)

    def value(self, key: Key, default: Any = None) -> Any:
        """The effective value of e.g. ("model", "dir"): as written (environment variables not expanded)."""
        d: Any = self.effective
        for k in key:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return _plain(d)

    def render(self, changes: dict[Key, Any], text: str | None = None) -> str:
        """The config.toml text with `changes` applied (missing sections and keys are created)."""
        doc = tomlkit.parse(self.user_text if text is None else text)
        for key, value in changes.items():
            node = doc
            for k in key[:-1]:
                if k not in node:
                    node[k] = tomlkit.table()
                node = node[k]
            last = key[-1]
            old = node.get(last) if hasattr(node, "get") else None
            if isinstance(value, dict) and isinstance(old, Table):
                for k in list(old.keys()):     # refill in place: keeps the table where it is, comments above it
                    del old[k]
                for k, x in value.items():
                    old[k] = _toml_value(x)
            else:
                node[last] = _toml_value(value)
        return tomlkit.dumps(doc)

    def check(self, text: str) -> config.Config:
        """config.parse() plus the hotkey check the tray app does on start; raises ConfigError."""
        c = config.parse(text, self.defaults_text)
        try:
            hotkey.parse(c.hotkey)
        except hotkey.HotkeyError as e:
            raise config.ConfigError(f"[hotkey] keys: {e}") from None
        return c

    def save(self, changes: dict[Key, Any]) -> config.Config:
        """Apply `changes` to the file as it is on disk now, validate, write atomically. Raises ConfigError
        (and leaves the file untouched) when the result isn't valid."""
        current = self._read_user()
        text = self.render(changes, current)
        c = self.check(text)
        if changes:
            write_atomic(self.file, text, crlf=_uses_crlf(self.file))
        self.reload()
        return c


def _uses_crlf(path: Path) -> bool:
    try:
        return b"\r\n" in path.read_bytes()[:4096]
    except OSError:
        return False


def write_atomic(path: Path, text: str, crlf: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# --- the dictation shortcut recorder ---------------------------------------------------------------

@dataclass(frozen=True)
class Recorded:
    action: str          # "listen" (only modifiers so far) | "cancel" | "set" | "error"
    spec: str = ""
    message: str = ""


def same_hotkey(a: str, b: str) -> bool:
    try:
        return hotkey.parse(a) == hotkey.parse(b)
    except hotkey.HotkeyError:
        return a.strip().lower() == b.strip().lower()


def record_key(vk: int, mods: Iterable[str], current: str,
               available: Callable[[str], bool] = hotkey.available,
               default: str = DEFAULT_HOTKEY, others: dict[str, str] | None = None) -> Recorded:
    """What a key press means while the recorder listens. `mods` = modifiers held ("ctrl", "alt", "shift",
    "win"); `current` = the shortcut the running tray app holds for this action (not checked: it is ours).
    `default` = what Backspace goes back to ("" = no shortcut, for the optional ones); `others` = the
    other shortcuts {name: spec}, which this one may not repeat."""
    mods = set(mods)
    if vk in hotkey.MODIFIER_VKS:
        return Recorded("listen")
    if not mods and vk == 0x1B:
        return Recorded("cancel")
    if not mods and vk == 0x08:
        if not default:
            return Recorded("set", "", "No shortcut.")
        return Recorded("set", default, f"Back to the default, {hotkey.display(default)}.")
    try:
        spec = hotkey.spec_for(mods, vk)
    except hotkey.HotkeyError as e:
        return Recorded("error", message=f"{str(e)[0].upper()}{str(e)[1:]}.")
    if not mods and not (0x70 <= vk <= 0x87 or vk in (0x13, 0x91)):
        return Recorded("error", spec, f"{hotkey.display(spec)} alone would stop that key working. "
                                       "Hold Ctrl, Alt or Win with it.")
    problem = hotkey.check_combo(spec)
    if problem:
        return Recorded("error", spec, problem)
    for name, other in (others or {}).items():
        if other and same_hotkey(spec, other):
            return Recorded("error", spec, f"{hotkey.display(spec)} is already the {name} shortcut.")
    if not same_hotkey(spec, current) and not available(spec):
        return Recorded("error", spec, f"{hotkey.display(spec)} is in use by another app. Pick another shortcut.")
    return Recorded("set", spec)


# --- choices the window offers ---------------------------------------------------------------------

AUTO_MODEL_LABEL = "Automatic (the best one found, or the one picked in the tray menu)"


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expandvars(p.strip()))) if p.strip() else ""


def folder_models(folders: Iterable[str]) -> list:
    """Models in the [model] folders entries (.gguf files or folders, searched 2 levels deep)."""
    from . import models
    out = []
    for raw in folders:
        p = Path(os.path.expandvars(raw.strip()))
        if not raw.strip():
            continue
        if p.is_dir():
            out += [m for f in models._gguf_files(p, 2) if (m := models.inspect(f, "your folders"))]
        elif (m := models.inspect(p, "your folders")) is not None:
            out.append(m)
    return out


def model_choices(found: Iterable, folders: Iterable[str], current: str,
                  inspect_folders: Callable[[Iterable[str]], list] = folder_models) -> list[tuple[str, str]]:
    """(value for [model] dir, label) pairs: "auto", every model found, every model in the folders, and
    the current setting if it is none of those (kept as written, e.g. with %APPDATA%)."""
    out: list[tuple[str, str]] = [("auto", AUTO_MODEL_LABEL)]
    seen = {"auto"}
    cur = _norm(current) if current.strip().lower() not in ("", "auto") else "auto"
    for m in [*found, *inspect_folders(folders)]:
        key = _norm(str(m.path))
        if key in seen:
            continue
        seen.add(key)
        value = current if key == cur else str(m.path)
        out.append((value, m.label()))
    if cur not in seen:
        out.append((current, f"{current} (from your settings)"))
    return out


def input_devices(devices: Iterable[dict] | None = None, hostapi: int | None = None) -> list[str]:
    """Names of the microphones, as the recorder matches them (the default input's host API, each name
    once). Only lists devices; nothing is opened."""
    if devices is None:
        import sounddevice as sd
        devices = list(sd.query_devices())
        try:
            hostapi = sd.query_devices(kind="input")["hostapi"]
        except Exception:
            hostapi = None
    names: list[str] = []
    for d in devices:
        if d.get("max_input_channels", 0) <= 0:
            continue
        if hostapi is not None and d.get("hostapi") != hostapi:
            continue
        name = str(d.get("name", "")).strip()
        if name and name not in names and "microsoft sound mapper" not in name.lower():
            names.append(name)
    return names


def model_in_use(data_dir: Path) -> str:
    """The model the tray picked last (with [model] dir = "auto"), from its model_path.txt; "" if unknown."""
    try:
        return (data_dir / "model_path.txt").read_text("utf-8").strip()
    except OSError:
        return ""
