"""The Settings window's controls for background learning: the switches, the list of fixes learned
automatically (Undo, Reset all) and the recordings buttons. Windows are built on a hidden Tk root (never
shown, no main loop); the learning state is a Store on a temp folder."""
from datetime import datetime

import numpy as np
import pytest

from parakeet_dictate import autolearn, config, learn, recordings
from parakeet_dictate import settings_store as ss
from parakeet_dictate import settings_ui

MAILZZY = ("Mailzzy", "mail sey")
KEYCLOAK = ("Keycloak", "key cloak")
GAVE_UP = "Couldn't change the learned fixes right now. Try again."


@pytest.fixture
def paths(tmp_path):
    p = config.Paths(tmp_path / "cfg", tmp_path / "data")
    p.config_dir.mkdir()
    p.data_dir.mkdir()
    p.config_file.write_text(config.DEFAULT_FILE.read_text("utf-8"), "utf-8")
    return p


@pytest.fixture
def auto(paths):
    """The learning state, as the tray process would have written it."""
    return autolearn.Store(paths.learning_dir, paths.learned_auto_file)


def learn_fix(auto, entry, recs=("rec-1", "rec-2"), when="2026-11-12 09:00"):
    for rec in recs:
        auto.note(rec, [learn.Proposal(entry[0], entry[1], True, "")], [], "send the Mailzzy campaign", when)


# --- what the pages offer ----------------------------------------------------------------------------

def group(page, name):
    return dict(dict(settings_ui.PAGES)[page])[name]


def test_pages_have_learning_specs():
    improvements = [g for g, _ in dict(settings_ui.PAGES)["Improvements"]]
    assert improvements.index("Learning in the background") == improvements.index("Context words") + 1
    enabled, claude, fixes = group("Improvements", "Learning in the background")
    assert (enabled.key, enabled.kind, enabled.label, enabled.help) == (
        ("learning", "enabled"), "switch", "Learn from my dictations",
        "Whisper re-listens while the PC is idle; repeated name mistakes become fixes.")
    assert (claude.key, claude.kind, claude.label, claude.help) == (
        ("learning", "use_claude"), "switch", "Ask Claude to judge disagreements",
        "Sends text only, never your voice. A few cents a day at most.")
    assert (fixes.key, fixes.kind, fixes.label, fixes.help) == (
        None, "learned_auto", "Learned automatically", "Undo a fix and it is never learned again.")

    privacy = {s.label: s for s in group("Extras", "Privacy and files")}
    keep = privacy["Keep recordings for learning"]
    assert (keep.key, keep.kind, keep.help) == (
        ("learning", "keep_recordings"), "switch", "On this PC only, deleted after 14 days.")
    days = privacy["Keep recordings for (days)"]
    assert (days.key, days.kind, days.help, days.lo, days.hi) == (
        ("learning", "keep_days"), "int", "1-365.", 1, 365)
    files = privacy["Recordings"]
    assert (files.key, files.kind, files.help, files.choices) == (
        None, "action", "Saved dictations used for learning.",
        (("delete_recordings", "Delete all"), ("open_recordings", "Open folder")))


def test_learned_auto_is_a_full_width_control():
    assert "learned_auto" in settings_ui.BELOW and "learned_auto" in settings_ui.CONTROLS


def test_learning_keys_round_trip_through_store(paths):
    store = ss.Store(paths)
    assert store.value(("learning", "enabled")) is True            # the defaults show until changed
    store.save({("learning", "enabled"): False, ("learning", "use_claude"): False,
                ("learning", "keep_recordings"): False, ("learning", "keep_days"): 30})
    c = config.load(paths)
    assert (c.learning_enabled, c.use_claude, c.keep_recordings, c.keep_days) == (False, False, False, 30)
    store = ss.Store(paths)
    assert store.value(("learning", "enabled")) is False and store.value(("learning", "keep_days")) == 30


def test_learning_keys_are_added_to_a_config_from_before_the_feature(paths):
    paths.config_file.write_text("[output]\nsounds = false\n", "utf-8")             # no [learning] table yet
    ss.Store(paths).save({("learning", "enabled"): False, ("learning", "keep_days"): 7})
    c = config.load(paths)
    assert (c.learning_enabled, c.keep_days, c.sounds) == (False, 7, False)
    assert c.use_claude is True and c.keep_recordings is True                         # the rest keep their defaults


# --- the window ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def hidden_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = settings_ui.make_root("dark")
    except tk.TclError as e:
        pytest.skip(f"no Tk: {e}")
    yield root
    root.destroy()


@pytest.fixture
def window(paths, hidden_root):
    env = settings_ui.Env(found_models=[], mics=[], startup_on=False, set_startup=lambda on: None,
                          available=lambda spec: True, model_in_use="", folder_models=lambda f: [])
    opened = []
    win = settings_ui.SettingsWindow(hidden_root, ss.Store(paths), env, open_file=opened.append,
                                     close=lambda: None)
    win.opened = opened
    yield win
    win.outer.destroy()


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def texts(widget):
    return [str(w.cget("text")) for w in walk(widget) if w.winfo_class() in ("TLabel", "TButton")
            and str(w.cget("text"))]


def buttons(widget, text):
    return [w for w in walk(widget) if w.winfo_class() == "TButton" and str(w.cget("text")) == text]


def learned(window):
    window.show("Improvements")
    return window.learned_auto


def test_learning_switches_and_days_are_saved_like_any_setting(window, paths):
    window.show("Improvements")
    window.show("Extras")
    assert window.field(("learning", "enabled")).control.get() is True
    assert window.field(("learning", "keep_days")).control.get() == 14
    assert not window.dirty()
    window.field(("learning", "enabled")).control.set(False)
    window.field(("learning", "use_claude")).control.set(False)
    window.field(("learning", "keep_days")).control.var.set("30")
    window.field(("learning", "keep_recordings")).control.set(False)
    assert window.changes()[0] == {("learning", "enabled"): False, ("learning", "use_claude"): False,
                                   ("learning", "keep_days"): 30, ("learning", "keep_recordings"): False}
    assert window.save(close=False)
    c = config.load(paths)
    assert (c.learning_enabled, c.use_claude, c.keep_days, c.keep_recordings) == (False, False, 30, False)


def test_learned_auto_control_starts_empty(window):
    c = learned(window)
    assert texts(c.frame) == [
        "Not started yet · 0 dictations checked · 0 fixes learned · 0 waiting for a second sighting",
        "No fixes learned yet.", "Reset all"]
    assert buttons(c.frame, "Reset all")[0].instate(["disabled"])                 # nothing to reset


def test_learned_auto_control_lists_the_fixes_and_counts(window, auto):
    learn_fix(auto, MAILZZY)
    learn_fix(auto, KEYCLOAK, recs=("rec-2",))                        # one sighting: waiting
    learn_fix(auto, KEYCLOAK, recs=("rec-3", "rec-4"), when="2026-11-14 18:30")
    learn_fix(auto, ("Orca", "or car"), recs=("rec-5",))
    for i in range(1, 6):
        auto.append_checked({"id": f"rec-{i}"})
    auto.set_status("Waiting for the PC to be idle")
    c = learned(window)
    assert texts(c.frame) == [
        "Waiting for the PC to be idle · 5 dictations checked · 2 fixes learned · 1 waiting for a second sighting",
        "mail sey → Mailzzy", "2026-11-12", "seen ×2", "Undo",
        "key cloak → Keycloak", "2026-11-14", "seen ×3", "Undo",
        "Reset all"]
    assert buttons(c.frame, "Reset all")[0].instate(["!disabled"])


def test_status_line_says_one_not_ones(window, auto):
    learn_fix(auto, MAILZZY, recs=("rec-1",))
    auto.append_checked({"id": "rec-1"})
    c = learned(window)
    assert texts(c.frame)[0] == (
        "Not started yet · 1 dictation checked · 0 fixes learned · 1 waiting for a second sighting")
    learn_fix(auto, MAILZZY, recs=("rec-2",))
    c.refresh()
    assert texts(c.frame)[0] == (
        "Not started yet · 1 dictation checked · 1 fix learned · 0 waiting for a second sighting")


def test_learned_auto_control_undo(window, auto):
    learn_fix(auto, MAILZZY)
    learn_fix(auto, KEYCLOAK)
    c = learned(window)
    buttons(c.frame, "Undo")[0].invoke()
    assert [(f.term, f.heard_as) for f in auto.active()] == [KEYCLOAK]
    assert texts(c.frame)[1:-1] == ["key cloak → Keycloak", "2026-11-12", "seen ×2", "Undo"]
    assert "mail sey" in window.msg.cget("text")                            # says what was undone
    assert str(window.msg.cget("style")) == "TLabel"
    # "never learned again": two more sightings change nothing
    learn_fix(auto, MAILZZY, recs=("rec-8", "rec-9"))
    assert [(f.term, f.heard_as) for f in auto.active()] == [KEYCLOAK]
    buttons(c.frame, "Undo")[0].invoke()
    assert texts(c.frame)[1] == "No fixes learned yet."
    assert not buttons(c.frame, "Undo")


def test_learned_auto_control_reset_asks_first(window, auto):
    learn_fix(auto, MAILZZY)
    learn_fix(auto, KEYCLOAK, recs=("rec-1",))                         # pending
    c = learned(window)
    buttons(c.frame, "Reset all")[0].invoke()
    assert "Really reset all?" in texts(c.frame)                       # asked in the window, not a messagebox
    assert not buttons(c.frame, "Reset all")
    assert len(auto.active()) == 1 and auto.pending_count() == 1       # nothing happened yet
    buttons(c.frame, "Cancel")[0].invoke()
    assert "Really reset all?" not in texts(c.frame) and buttons(c.frame, "Reset all")
    assert len(auto.active()) == 1

    buttons(c.frame, "Reset all")[0].invoke()
    buttons(c.frame, "Yes")[0].invoke()
    assert auto.active() == [] and auto.pending_count() == 0
    assert texts(c.frame)[1:] == ["No fixes learned yet.", "Reset all"]
    assert "Really reset all?" not in texts(c.frame)
    learn_fix(auto, MAILZZY, recs=("rec-8", "rec-9"))                  # reset fixes are never learned again
    assert auto.active() == []


def test_learned_auto_control_never_makes_the_window_dirty(window, auto):
    learn_fix(auto, MAILZZY)
    learn_fix(auto, KEYCLOAK)
    c = learned(window)
    assert not window.dirty()
    buttons(c.frame, "Undo")[0].invoke()
    buttons(c.frame, "Reset all")[0].invoke()
    assert not window.dirty() and window.save_btn.instate(["disabled"])
    buttons(c.frame, "Yes")[0].invoke()
    assert not window.dirty() and window.changes() == ({}, [])
    assert all(f.control is not c for f in window.fields)               # not a setting: never saved


@pytest.mark.parametrize("error", [TimeoutError("locked"), PermissionError("denied"), OSError("disk")])
def test_undo_and_reset_that_cannot_write_say_so(window, auto, monkeypatch, error):
    learn_fix(auto, MAILZZY)
    c = learned(window)

    def fail(*_):
        raise error
    monkeypatch.setattr(c.auto, "undo", fail)
    monkeypatch.setattr(c.auto, "reset_all", fail)
    buttons(c.frame, "Undo")[0].invoke()
    assert window.msg.cget("text") == GAVE_UP and str(window.msg.cget("style")) == "Error.TLabel"
    assert "mail sey → Mailzzy" in texts(c.frame)                       # still listed: it is still active
    window.message("")
    buttons(c.frame, "Reset all")[0].invoke()
    buttons(c.frame, "Yes")[0].invoke()
    assert window.msg.cget("text") == GAVE_UP and str(window.msg.cget("style")) == "Error.TLabel"
    assert "mail sey → Mailzzy" in texts(c.frame) and "Really reset all?" not in texts(c.frame)
    assert not window.dirty()


def test_undo_while_the_checker_holds_the_lock_gives_up_politely(window, auto, monkeypatch):
    learn_fix(auto, MAILZZY)
    c = learned(window)
    monkeypatch.setattr(autolearn, "LOCK_WAIT_S", 0.1)
    other = autolearn.Store(auto.learning_dir, auto.learned_auto_file)       # the tray process
    with other.lock():
        buttons(c.frame, "Undo")[0].invoke()
    assert window.msg.cget("text") == GAVE_UP
    assert [(f.term, f.heard_as) for f in auto.active()] == [MAILZZY]
    buttons(c.frame, "Undo")[0].invoke()                                       # the lock is free again
    assert auto.active() == [] and "mail sey" in window.msg.cget("text")


def test_damaged_learning_files_show_as_empty(window, paths):
    paths.learning_dir.mkdir(parents=True)
    (paths.learning_dir / "state.json").write_text("{not json", "utf-8")
    (paths.learning_dir / "checked.jsonl").write_bytes(b"\xff\xfe garbage\n{\n")
    c = learned(window)
    assert texts(c.frame)[:2] == [
        "Not started yet · 0 dictations checked · 0 fixes learned · 0 waiting for a second sighting",
        "No fixes learned yet."]


# --- recordings buttons ------------------------------------------------------------------------------

def save_recordings(folder, count):
    for i in range(count):
        when = datetime(2026, 11, 12, 9, i, 0)
        recordings.save(folder, np.zeros(1600, dtype=np.float32),
                        recordings.Record(id=recordings.new_id(when), time=when.isoformat(), exe="notepad.exe",
                                          raw="a", phrases=["a"], typed="A", model="m", language="en"))


def test_delete_recordings_action_counts(window, paths):
    save_recordings(paths.recordings_dir, 3)
    (paths.recordings_dir / "notes.txt").write_text("not a recording", "utf-8")
    window.do_action("delete_recordings")
    assert window.msg.cget("text") == "Deleted 3 recordings."
    assert sorted(p.name for p in paths.recordings_dir.iterdir()) == ["notes.txt"]
    assert not (paths.data_dir / "settings_request.json").exists()           # no tray action: done right here
    window.do_action("delete_recordings")
    assert window.msg.cget("text") == "No recordings to delete."
    save_recordings(paths.recordings_dir, 1)
    window.do_action("delete_recordings")
    assert window.msg.cget("text") == "Deleted 1 recording."


def test_delete_recordings_without_a_folder(window, paths):
    assert not paths.recordings_dir.exists()
    window.do_action("delete_recordings")
    assert window.msg.cget("text") == "No recordings to delete."


def test_delete_recordings_that_fails_says_so(window, paths, monkeypatch):
    def fail(folder):
        raise PermissionError("denied")
    monkeypatch.setattr(recordings, "delete_all", fail)
    window.do_action("delete_recordings")
    assert "Couldn't delete the recordings" in window.msg.cget("text")
    assert str(window.msg.cget("style")) == "Error.TLabel"


def test_open_recordings_folder(window, paths):
    window.do_action("open_recordings")
    assert window.msg.cget("text") == "No recordings yet." and window.opened == []          # no folder
    paths.recordings_dir.mkdir(parents=True)
    window.message("")
    window.do_action("open_recordings")
    assert window.msg.cget("text") == "No recordings yet." and window.opened == []          # empty folder
    save_recordings(paths.recordings_dir, 1)
    window.message("")
    window.do_action("open_recordings")
    assert window.opened == [paths.recordings_dir] and window.msg.cget("text") == ""


def test_the_buttons_on_the_extras_page_run_the_actions(window, paths):
    window.show("Extras")
    save_recordings(paths.recordings_dir, 2)
    delete, open_folder = buttons(window.pages["Extras"], "Delete all")[0], buttons(
        window.pages["Extras"], "Open folder")[0]
    open_folder.invoke()
    assert window.opened == [paths.recordings_dir]
    delete.invoke()
    assert window.msg.cget("text") == "Deleted 2 recordings."
