"""History of recent dictations: where it is kept (off / session / disk), the hand-over to the History
window and back, the window's list logic, the tray menu item and shortcut, and the window itself on a
hidden Tk root (never shown). Made-up dictations only; all files live in pytest's tmp_path."""
import json
import threading
import types
from datetime import datetime

import pytest

from parakeet_dictate import config, history as hist, history_ui, output, winutil
from parakeet_dictate.history import Entry, History
from test_app_flow import FakeEngine, dictate, make_app, out, retry  # noqa: F401  (out is a fixture)
from test_shortcuts_menu import _app, visible_items

NOW = datetime(2026, 11, 12, 14, 30)


def entry(text, i=0, program="Code.exe", when="2026-11-12T14:03:05"):
    return Entry(f"id{i}", when, text, program, len(text.split()), 2.5)


# --- settings --------------------------------------------------------------------------------------

def test_history_settings_defaults_and_checks():
    c = config.parse("")
    assert (c.history_keep, c.history_max_items, c.history_hotkey) == ("session", 200, "")
    c = config.parse('[history]\nkeep = "disk"\nmax_items = 10\n[hotkey]\nhistory = "ctrl+alt+h"')
    assert (c.history_keep, c.history_max_items, c.history_hotkey) == ("disk", 10, "ctrl+alt+h")
    for bad in ('[history]\nkeep = "cloud"', "[history]\nmax_items = 9", "[history]\nmax_items = 5001",
                '[hotkey]\nhistory = "ctrl+nonsense"'):
        with pytest.raises(config.ConfigError):
            config.parse(bad)


# --- where it is kept -------------------------------------------------------------------------------

def test_session_keeps_the_last_n_in_memory_only(tmp_path):
    f = tmp_path / "history.jsonl"
    h = History(f, "session", 3)
    for i in range(5):
        h.add(f"Dictation number {i}.", "Code.exe", 1.24, now=NOW)
    assert [e.text for e in h.entries()] == ["Dictation number 2.", "Dictation number 3.", "Dictation number 4."]
    e = h.entries()[-1]
    assert (e.program, e.words, e.seconds, e.time) == ("Code.exe", 3, 1.2, "2026-11-12T14:30:00")
    assert not f.exists() and list(tmp_path.iterdir()) == []


def test_off_keeps_nothing_and_turning_it_off_forgets(tmp_path):
    h = History(tmp_path / "history.jsonl", "off", 200)
    assert h.add("Secret.") is None and len(h) == 0 and not h.enabled
    h.configure("session", 200)
    h.add("Kept for now.")
    h.configure("off", 200)
    assert len(h) == 0 and list(tmp_path.iterdir()) == []


def test_disk_survives_a_restart_and_trims_atomically(tmp_path):
    f = tmp_path / "history.jsonl"
    h = History(f, "disk", 10)
    for i in range(12):
        h.add(f"Line {i}.", "Notepad.exe", 1.0)
    lines = f.read_text("utf-8").splitlines()
    assert len(lines) == 10 and json.loads(lines[0])["text"] == "Line 2."
    assert not list(tmp_path.glob("*.tmp"))
    again = History(f, "disk", 10)                    # the app started again
    assert [e.text for e in again.entries()] == [f"Line {i}." for i in range(2, 12)]


def test_damaged_lines_are_skipped(tmp_path):
    f = tmp_path / "history.jsonl"
    good = json.dumps(entry("Good one.").to_dict())
    f.write_text("{broken\n" + good + "\n" + json.dumps({"id": "x", "time": "not a time", "text": "y"}) + "\n\n",
                 "utf-8")
    assert [e.text for e in History(f, "disk", 200).entries()] == ["Good one."]


def test_leaving_disk_deletes_the_file_and_going_to_disk_writes_it(tmp_path):
    f = tmp_path / "history.jsonl"
    h = History(f, "disk", 200)
    h.add("On disk.")
    assert f.exists()
    h.configure("session", 200)
    assert not f.exists() and [e.text for e in h.entries()] == ["On disk."]
    h.add("In memory.")
    h.configure("disk", 200)
    assert [e.text for e in hist.read_file(f)] == ["On disk.", "In memory."]
    h.configure("disk", 1 + 0)                         # shorter: trimmed, file rewritten
    assert [e.text for e in hist.read_file(f)] == ["In memory."]


def test_session_at_start_removes_an_old_history_file(tmp_path):
    f = tmp_path / "history.jsonl"
    hist.write_file(f, [entry("Left from disk mode.")])
    h = History(f, "session", 200)
    assert not f.exists() and len(h) == 0


def test_remove_rewrites_the_file(tmp_path):
    f = tmp_path / "history.jsonl"
    h = History(f, "disk", 200)
    a, b = h.add("Keep me."), h.add("Delete me.")
    assert h.remove([b.id, "unknown"]) == 1
    assert [e.id for e in hist.read_file(f)] == [a.id] and [e.id for e in h.entries()] == [a.id]


# --- hand-over to the window and back ----------------------------------------------------------------

def test_request_is_deleted_on_reading_and_result_holds_only_ids(tmp_path):
    req = hist.write_request(tmp_path, "session", [entry("Private text.")])
    assert req.name.startswith("history-")
    keep, entries = hist.read_request(req)
    assert keep == "session" and [e.text for e in entries] == ["Private text."] and not req.exists()
    hist.write_result(tmp_path, ["id0", "id1", "id0"])
    raw = (tmp_path / hist.RESULT_FILE).read_text("utf-8")
    assert "Private" not in raw
    assert hist.read_result(tmp_path) == ["id0", "id1"] and not (tmp_path / hist.RESULT_FILE).exists()
    assert hist.read_result(tmp_path) == []
    (tmp_path / "history-old.json").write_text("{}", "utf-8")
    hist.drop_requests(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_launch_hands_over_a_file_not_the_command_line(tmp_path):
    seen = {}

    def popen(argv, **kw):
        seen["argv"] = argv
        return "proc"

    proc, req = history_ui.launch("session", [entry("Not on the command line.")], tmp_path, popen=popen)
    assert proc == "proc" and req.exists() and seen["argv"][-2:] == ["--history", str(req)]
    assert "Not on the command line." not in " ".join(seen["argv"])

    def broken(argv, **kw):
        raise OSError("no")
    with pytest.raises(OSError):
        history_ui.launch("session", [entry("x")], tmp_path, popen=broken)
    assert len(list(tmp_path.glob("history-*.json"))) == 1        # only the first one's


# --- the window's list logic ---------------------------------------------------------------------------

def model(tmp_path, keep="session", copy=None):
    entries = [entry("Deploy core before CRM.", 0, "Code.exe", "2026-11-10T09:05:00"),
               entry("Lunch at noon?\nSecond line.", 1, "Teams.exe", "2026-11-12T11:00:00"),
               entry("Check the McpAuthFilter logs.", 2, "", "2026-11-12T14:03:05")]
    f = tmp_path / "history.jsonl"
    if keep == "disk":
        hist.write_file(f, entries)
    return history_ui.HistoryModel(entries, keep, tmp_path, f, copy=copy)


def test_model_lists_newest_first_and_searches(tmp_path):
    m = model(tmp_path)
    assert [e.id for e in m.visible()] == ["id2", "id1", "id0"]
    m.set_query("  core  CRM ")
    assert [e.id for e in m.visible()] == ["id0"]
    m.set_query("teams")                          # the program counts too
    assert [e.id for e in m.visible()] == ["id1"] and m.count_text() == "1 of 3 dictations"
    m.set_query("nothing like this")
    assert m.visible() == []


def test_model_rows_and_details(tmp_path):
    m = model(tmp_path)
    e2, e1, e0 = m.visible()
    assert m.row(e2, NOW) == ("14:03", "–", "Check the McpAuthFilter logs.")
    assert m.row(e1, NOW) == ("11:00", "Teams", "Lunch at noon?")
    assert m.row(e0, NOW)[0] == "10 Nov 09:05"
    assert m.row(entry("x", when="2025-01-02T08:00:00"), NOW)[0] == "2 Jan 2025 08:00"
    assert m.details(e0) == "Tuesday 10 November 2026, 09:05 · Code · 4 words · 2.5 s spoken"
    assert m.first_line(entry("word " * 40), 20).endswith("…")


def test_model_copy_uses_the_given_clipboard(tmp_path):
    copied = []
    m = model(tmp_path, copy=copied.append)
    assert m.copy("id1") is None and copied == ["Lunch at noon?\nSecond line."]
    assert m.copy("gone") == "Pick a dictation first."


def test_model_delete_and_clear_reach_the_tray_and_the_file(tmp_path):
    m = model(tmp_path, keep="disk")
    assert m.delete(["id1"]) is None
    assert [e.id for e in m.visible()] == ["id2", "id0"]
    assert [e.id for e in hist.read_file(tmp_path / "history.jsonl")] == ["id0", "id2"]
    assert json.loads((tmp_path / hist.RESULT_FILE).read_text("utf-8")) == {"deleted": ["id1"]}
    assert m.clear() is None and m.visible() == []
    assert hist.read_file(tmp_path / "history.jsonl") == []
    assert hist.read_result(tmp_path) == ["id0", "id1", "id2"]


def test_session_model_never_writes_a_history_file(tmp_path):
    m = model(tmp_path)
    m.delete(["id0"])
    assert not (tmp_path / "history.jsonl").exists()


# --- the tray: recording dictations, the menu, the shortcut, the window process -----------------------------

def test_finished_dictations_are_added(out, tmp_path):  # noqa: F811
    app = make_app(FakeEngine("Hello world."))
    app.history = History(tmp_path / "history.jsonl", "session", 200)
    app.target_exe = "Notepad.exe"
    dictate(app)
    (e,) = app.history.entries()
    assert (e.text, e.program, e.words) == ("Hello world.", "Notepad.exe", 2) and e.seconds > 1


def test_empty_and_failed_dictations_are_not_added(out, tmp_path):  # noqa: F811
    app = make_app(FakeEngine("um", RuntimeError("boom")))
    app.history = History(tmp_path / "history.jsonl", "session", 200)
    dictate(app)
    dictate(app)
    assert len(app.history) == 0


def test_a_retried_recording_is_added(out, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(winutil, "foreground_exe", lambda: "Outlook.exe")
    app = make_app(FakeEngine(RuntimeError("x"), "Hello again."))
    app.history = History(tmp_path / "history.jsonl", "session", 200)
    dictate(app)
    assert len(app.history) == 0
    retry(app)
    assert [(e.text, e.program) for e in app.history.entries()] == [("Hello again.", "Outlook.exe")]


def test_menu_item_follows_the_setting(tmp_path):
    app = _app(tmp_path, history_hotkey="ctrl+alt+h")
    assert "History…\tCtrl+Alt+H" not in visible_items(app._menu())        # no history object: hidden
    app.history = History(tmp_path / "h.jsonl", "session", 200)
    items = [i for i in app._menu().items if i.visible and i.text.startswith("History")]
    assert [i.text for i in items] == ["History…\tCtrl+Alt+H"] and not items[0].enabled
    texts = visible_items(app._menu())
    assert texts.index("History…\tCtrl+Alt+H") == texts.index("Correct last dictation…") + 1
    app.history.add("Something.")
    assert next(i for i in app._menu().items if i.text.startswith("History")).enabled
    app.history.configure("off", 200)
    assert not any(t.startswith("History") for t in visible_items(app._menu()))


def test_history_shortcut_is_registered_with_the_others():
    c = config.parse('[hotkey]\nhistory = "ctrl+alt+h"')
    from parakeet_dictate.app import App
    assert App._extra_hotkeys(c)["history"] == "ctrl+alt+h"
    assert "History shortcut not set" in App._extras_problem({"history": "taken"})


def test_opening_when_off_or_empty_says_so(tmp_path):
    app = _app(tmp_path)
    app.history = History(tmp_path / "h.jsonl", "off", 200)
    app.open_history_window()
    app.history.configure("session", 200)
    app.open_history_window()
    assert app.notes == ["History is off (Settings → Extras → Privacy and files).",
                         "No dictations in the history yet."]


def test_tray_opens_one_window_and_forgets_what_it_deleted(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.history = History(tmp_path / "h.jsonl", "session", 200)
    keep_me, drop_me = app.history.add("Keep me."), app.history.add("Delete me.")
    gate, done = threading.Event(), threading.Event()
    launched = []

    class Proc:
        def poll(self):
            return 0 if gate.is_set() else None

        def wait(self):
            gate.wait(5)
            hist.write_result(app.paths.data_dir, [drop_me.id])      # what the window would leave
            return 0

    def launch(keep, entries, data_dir):
        req = hist.write_request(data_dir, keep, entries)
        launched.append(json.loads(req.read_text("utf-8")))
        return Proc(), req

    monkeypatch.setattr(history_ui, "launch", launch)
    app._refresh = done.set
    (app.paths.data_dir / "history-stale.json").write_text("{}", "utf-8")
    app.open_history_window()
    app.open_history_window()                               # still open: no second window
    assert len(launched) == 1 and launched[0]["keep"] == "session"
    assert [e["text"] for e in launched[0]["entries"]] == ["Keep me.", "Delete me."]
    assert not (app.paths.data_dir / "history-stale.json").exists()   # a crashed window's leftover
    gate.set()
    assert done.wait(5)
    assert [e.id for e in app.history.entries()] == [keep_me.id]
    assert list(app.paths.data_dir.glob("history*")) == []     # request and result both gone


def test_command_from_source_runs_the_module(tmp_path):
    argv, cwd = history_ui.command(tmp_path / "r.json")
    assert argv[1:4] == ["-m", "parakeet_dictate", "--history"] and cwd


# --- the window, built on a hidden root (never shown) -----------------------------------------------

@pytest.fixture(scope="module")
def hidden_root():
    tk = pytest.importorskip("tkinter")
    from parakeet_dictate import settings_ui
    try:
        root = settings_ui.make_root("light")
    except tk.TclError as e:
        pytest.skip(f"no Tk: {e}")
    yield root
    root.destroy()


@pytest.fixture
def window(tmp_path, hidden_root):
    copied, asked, closed = [], [], []
    m = model(tmp_path, keep="disk", copy=copied.append)
    answer = types.SimpleNamespace(value=True)
    win = history_ui.HistoryWindow(hidden_root, m, close=lambda: closed.append(1),
                                   confirm=lambda q: asked.append(q) or answer.value)
    win.copied, win.asked, win.closed, win.answer = copied, asked, closed, answer
    yield win
    win.outer.destroy()


def test_window_shows_newest_first_with_the_text_below(window):
    assert window.root.state() == "withdrawn"
    assert list(window.tree.get_children()) == ["id2", "id1", "id0"]
    assert window.selected() == "id2"
    assert window.text.get("1.0", "end-1c") == "Check the McpAuthFilter logs."
    assert str(window.text.cget("state")) == "disabled"               # read-only
    assert window.count.cget("text") == "3 dictations"


def test_window_search_filters_as_you_type(window):
    window.query.set("lunch")
    assert list(window.tree.get_children()) == ["id1"] and window.selected() == "id1"
    assert window.text.get("1.0", "end-1c") == "Lunch at noon?\nSecond line."
    window.query.set("zzz")
    assert window.tree.get_children() == () and window.copy_btn.instate(["disabled"])
    assert "No dictations match" in window.meta.cget("text")
    window.query.set("")
    assert len(window.tree.get_children()) == 3


def test_window_copy_and_copy_and_close(window):
    window.tree.selection_set("id0")
    window.show_selected()
    window.copy()
    assert window.copied == ["Deploy core before CRM."] and window.closed == []
    window.copy_and_close()
    assert window.copied[-1] == "Deploy core before CRM." and window.closed == [1]


def test_window_delete_selects_the_next_one(window, tmp_path):
    window.tree.selection_set("id1")
    window.show_selected()
    window.delete()
    assert list(window.tree.get_children()) == ["id2", "id0"] and window.selected() == "id0"
    assert [e.id for e in hist.read_file(tmp_path / "history.jsonl")] == ["id0", "id2"]
    assert hist.read_result(tmp_path) == ["id1"]


def test_window_clear_all_asks_first(window, tmp_path):
    window.answer.value = False
    window.clear()
    assert len(window.tree.get_children()) == 3 and window.asked == ["Delete all 3 dictations from the history?"]
    window.answer.value = True
    window.clear()
    assert window.tree.get_children() == () and window.clear_btn.instate(["disabled"])
    assert hist.read_file(tmp_path / "history.jsonl") == []


def test_copy_failure_is_shown(tmp_path, hidden_root):
    def broken(text):
        raise OSError("clipboard busy")
    win = history_ui.HistoryWindow(hidden_root, model(tmp_path, copy=broken), close=lambda: None)
    try:
        assert win.copy() is False and "clipboard busy" in win.msg.cget("text")
    finally:
        win.outer.destroy()


def test_default_clipboard_is_the_apps(monkeypatch, tmp_path):
    copied = []
    monkeypatch.setattr(output, "copy_to_clipboard", copied.append)
    m = history_ui.HistoryModel([entry("Via output.")], "session", tmp_path)
    assert m.copy("id0") is None and copied == ["Via output."]
