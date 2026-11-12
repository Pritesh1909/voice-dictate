"""Output settings for the program being dictated into ([apps."<exe>"] over [output] and [commands]).

Looked up once at the start of a dictation from the foreground window's exe name, so a dictation keeps
the same settings to the end even if another window comes to the front meanwhile. Per-app dictionary
words are a separate thing ([dictionary.apps."<exe>"]) and are not handled here.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import AppOverride, Config, app_key


@dataclass(frozen=True)
class OutputSettings:
    method: str                  # "type" or "paste"
    add_trailing_space: bool
    type_while_speaking: bool
    press_enter: bool
    commands: bool
    app: str = ""                # the [apps] entry that applied ("" = none)


def override_for(exe: str, cfg: Config) -> tuple[str, AppOverride] | None:
    key = app_key(exe or "")
    if not key:
        return None
    for name, o in cfg.apps:
        if name == key:
            return name, o
    return None


def settings_for(exe: str, cfg: Config) -> OutputSettings:
    """The effective settings when dictating into `exe` (e.g. "WindowsTerminal.exe"; "" = unknown)."""
    base = OutputSettings(
        method=cfg.output_method,
        add_trailing_space=cfg.add_trailing_space,
        type_while_speaking=cfg.type_while_speaking,
        press_enter=bool(getattr(cfg, "press_enter", False)),   # [output] press_enter (another branch adds it)
        commands=cfg.commands_enabled,
    )
    found = override_for(exe, cfg)
    if found is None:
        return base
    name, o = found
    pick = lambda mine, default: default if mine is None else mine   # noqa: E731
    return OutputSettings(
        method=pick(o.method, base.method),
        add_trailing_space=pick(o.add_trailing_space, base.add_trailing_space),
        type_while_speaking=pick(o.type_while_speaking, base.type_while_speaking),
        press_enter=pick(o.press_enter, base.press_enter),
        commands=pick(o.commands, base.commands),
        app=name,
    )
