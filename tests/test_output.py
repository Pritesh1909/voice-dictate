"""Delivery decisions in output.py. SendInput and the clipboard are replaced by fakes: nothing is typed or copied."""
import os

import pytest

from parakeet_dictate import output

REAL_HAS_TYPING_TARGET = output.has_typing_target


@pytest.fixture(autouse=True)
def no_real_input(monkeypatch):
    sent, clip = [], []

    def fake_send(events):
        sent.extend((e.u.ki.wVk, e.u.ki.wScan, e.u.ki.dwFlags) for e in events)

    monkeypatch.setattr(output, "_send", fake_send)
    monkeypatch.setattr(output, "_set_clipboard_text", clip.append)
    monkeypatch.setattr(output, "_get_clipboard_text", lambda: None)
    monkeypatch.setattr(output, "modifiers_down", lambda: False)
    monkeypatch.setattr(output, "has_typing_target", lambda: True)
    return sent, clip


def test_press_enter_is_one_return_down_and_up(no_real_input):
    sent, _ = no_real_input
    output.press_enter()
    assert sent == [(output.VK_RETURN, 0, 0), (output.VK_RETURN, 0, output.KEYEVENTF_KEYUP)]


def test_deliver_with_no_window_raises_and_types_nothing(monkeypatch, no_real_input):
    sent, _ = no_real_input
    monkeypatch.setattr(output, "has_typing_target", lambda: False)
    with pytest.raises(output.DeliveryError) as e:
        output.deliver("hello", "type")
    assert e.value.no_target and sent == []


def test_deliver_reports_blocked_sendinput(monkeypatch):
    def blocked(events):
        raise OSError("SendInput blocked (error 5); is the target window elevated?")
    monkeypatch.setattr(output, "_send", blocked)
    for method in ("type", "paste"):
        with pytest.raises(output.DeliveryError) as e:
            output.deliver("hello", method)
        assert not e.value.no_target and "blocked" in str(e.value)


def test_copy_instead_puts_text_on_clipboard_and_says_why(no_real_input):
    _, clip = no_real_input
    assert output.copy_instead("Hi.", output.DeliveryError("x", no_target=True)) == output.NO_TARGET_NOTE
    assert output.copy_instead("Yo.", OSError("SendInput blocked")) == output.BLOCKED_NOTE
    assert clip == ["Hi.", "Yo."]


@pytest.mark.parametrize("fg, ok", [
    ((0, 0, ""), False),                              # nothing has focus
    ((5, 1234, "Progman"), False),                    # desktop
    ((5, 1234, "WorkerW"), False),
    ((5, 1234, "Shell_TrayWnd"), False),              # taskbar
    ((5, os.getpid(), "pystray"), False),             # our own tray window (after a tray menu click)
    ((5, 1234, "Chrome_WidgetWin_1"), True),
    ((5, 1234, "CASCADIA_HOSTING_WINDOW_CLASS"), True),
])
def test_typing_target(monkeypatch, fg, ok):
    monkeypatch.setattr(output, "_foreground", lambda: fg)
    assert REAL_HAS_TYPING_TARGET() is ok


def test_foreground_query_works():
    hwnd, pid, cls = output._foreground()     # read-only: which window has focus
    assert isinstance(hwnd, int) and isinstance(pid, int) and isinstance(cls, str)
