"""Idle conditions: when the background learning checker may run, and why it may not."""
import sys
from dataclasses import replace

import pytest

from parakeet_dictate import idle
from parakeet_dictate.idle import Conditions, on_mains_power, seconds_since_input, why_not

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="uses the Windows API")

GOOD = Conditions(enabled=True, teacher_ready=True, app_idle=True, idle_seconds=600.0, idle_needed=180.0,
                  mains=True, gpu_failed=False, teacher_on_cpu=False)

# Every blocker, in the order why_not reports them: (what to change, the text for the Settings status line).
BLOCKERS = [
    (dict(enabled=False), "Off"),
    (dict(teacher_ready=False), "Waiting for Whisper"),
    (dict(gpu_failed=True), "Paused: no graphics card"),
    (dict(teacher_on_cpu=True), "Paused: no graphics card"),
    (dict(mains=False), "Paused: on battery"),
    (dict(app_idle=False), "Waits until the PC is idle"),
    (dict(idle_seconds=10.0), "Waits until the PC is idle"),
]


def test_all_ok_returns_none():
    assert why_not(GOOD) is None


@pytest.mark.parametrize("change, reason", BLOCKERS)
def test_each_blocker_alone(change, reason):
    assert why_not(replace(GOOD, **change)) == reason


def test_why_not_each_condition():
    """With every blocker present, the reasons come out in the listed order as each one is cleared."""
    c = Conditions(enabled=False, teacher_ready=False, app_idle=False, idle_seconds=0.0, idle_needed=180.0,
                   mains=False, gpu_failed=True, teacher_on_cpu=True)
    assert why_not(c) == "Off"
    c = replace(c, enabled=True)
    assert why_not(c) == "Waiting for Whisper"
    c = replace(c, teacher_ready=True)
    assert why_not(c) == "Paused: no graphics card"
    c = replace(c, gpu_failed=False)
    assert why_not(c) == "Paused: no graphics card"      # still on the CPU
    c = replace(c, teacher_on_cpu=False)
    assert why_not(c) == "Paused: on battery"
    c = replace(c, mains=True)
    assert why_not(c) == "Waits until the PC is idle"    # app busy, and input just now
    c = replace(c, app_idle=True)
    assert why_not(c) == "Waits until the PC is idle"    # still typing
    c = replace(c, idle_seconds=180.0)
    assert why_not(c) is None


def test_input_gap_must_reach_idle_needed():
    assert why_not(replace(GOOD, idle_seconds=179.9)) == "Waits until the PC is idle"
    assert why_not(replace(GOOD, idle_seconds=180.0)) is None


# --- Windows calls ------------------------------------------------------------------------------

@windows_only
def test_seconds_since_input_is_non_negative():
    s = seconds_since_input()
    assert isinstance(s, float)
    assert s >= 0.0


@windows_only
def test_seconds_since_input_counts_from_the_last_input_tick(monkeypatch):
    def fake_last_input(ref):
        ref._obj.dwTime = 7_000
        return 1

    monkeypatch.setattr(idle.user32, "GetLastInputInfo", fake_last_input)
    monkeypatch.setattr(idle.kernel32, "GetTickCount64", lambda: 9_500)
    assert seconds_since_input() == 2.5


@windows_only
def test_seconds_since_input_survives_the_32_bit_tick_wrap(monkeypatch):
    """GetLastInputInfo's tick is 32-bit, GetTickCount64's is not: 500 ms past the wrap, last input 1.5 s before it."""
    def fake_last_input(ref):
        ref._obj.dwTime = 2**32 - 1_500
        return 1

    monkeypatch.setattr(idle.user32, "GetLastInputInfo", fake_last_input)
    monkeypatch.setattr(idle.kernel32, "GetTickCount64", lambda: 2**32 + 500)
    assert seconds_since_input() == 2.0


@windows_only
def test_seconds_since_input_is_zero_when_the_call_fails(monkeypatch):
    """Unknown idle time must not look like a long idle."""
    monkeypatch.setattr(idle.user32, "GetLastInputInfo", lambda ref: 0)
    assert seconds_since_input() == 0.0


@windows_only
def test_on_mains_power_returns_a_bool():
    assert isinstance(on_mains_power(), bool)


@windows_only
@pytest.mark.parametrize("ac_line_status, expected", [(0, False), (1, True), (255, True)])
def test_on_mains_power_reads_ac_line_status(monkeypatch, ac_line_status, expected):
    def fake_power_status(ref):
        ref._obj.ACLineStatus = ac_line_status
        return 1

    monkeypatch.setattr(idle.kernel32, "GetSystemPowerStatus", fake_power_status)
    assert on_mains_power() is expected


@windows_only
def test_on_mains_power_counts_unknown_as_mains(monkeypatch):
    monkeypatch.setattr(idle.kernel32, "GetSystemPowerStatus", lambda ref: 0)
    assert on_mains_power() is True
