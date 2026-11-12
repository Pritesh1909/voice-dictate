"""Small, careful edits to the user's config.toml from the app itself (tray → "Add a model file…").

The file is the user's: it is changed with tomlkit so comments, blank lines and the order of everything
else stay exactly as they were; the result must pass config.parse() before anything is written, and it
is written atomically (a temporary file renamed over the old one), keeping the file's line endings.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import tomlkit

from . import config as cfgmod


def edit(path: Path, change: Callable[[tomlkit.TOMLDocument], bool]) -> bool:
    """Apply `change(doc)` to config.toml. `change` returns False when there is nothing to change; then
    the file isn't touched. Raises ConfigError (the file, or the result, isn't valid) or OSError."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = cfgmod.DEFAULT_FILE.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    try:
        doc = tomlkit.parse(text)
    except Exception as e:  # tomlkit's parse errors don't share one base class with a useful name
        raise cfgmod.ConfigError(f"config.toml is not valid TOML: {e}") from None
    if not change(doc):
        return False
    new = tomlkit.dumps(doc)
    cfgmod.parse(new)                       # never write settings the app couldn't load
    data = new.replace("\n", "\r\n") if crlf else new
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data.encode("utf-8"))
    os.replace(tmp, path)
    return True


def _same(a: str, b: str) -> bool:
    norm = lambda s: os.path.normcase(os.path.normpath(os.path.expanduser(os.path.expandvars(s))))  # noqa: E731
    return norm(a) == norm(b)


def _toml_str(s: str):
    """A literal string ('D:\\models\\x.gguf' reads better than "D:\\\\models\\\\x.gguf") when it can be one."""
    if "'" in s or "\n" in s or "\r" in s:
        return s
    return tomlkit.string(s, literal=True)


def add_model_folder(path: Path, entry: str) -> bool:
    """Append `entry` (a .gguf file or a folder) to [model] folders. False when it is already listed."""
    def change(doc) -> bool:
        model = doc.get("model")
        if model is None:
            model = tomlkit.table()
            doc["model"] = model
        elif not isinstance(model, dict):
            raise cfgmod.ConfigError("[model] in config.toml isn't a table")
        folders = model.get("folders")
        if folders is None:
            arr = tomlkit.array()
            arr.append(_toml_str(entry))
            model["folders"] = arr
            return True
        if not isinstance(folders, list):
            raise cfgmod.ConfigError("[model] folders in config.toml must be a list")
        if any(isinstance(x, str) and _same(str(x), entry) for x in folders):
            return False
        folders.append(_toml_str(entry))
        return True
    return edit(path, change)
