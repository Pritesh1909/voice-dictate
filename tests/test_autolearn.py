import json
import os
import pathlib
import tomllib

import pytest

from parakeet_dictate import autolearn, learn
from parakeet_dictate.autolearn import AutoFix, Store, entry_key

MAILZZY = ("Mailzzy", "mail sey")
KEYCLOAK = ("Keycloak", "")


def prop(entry, ok=True):
    term, heard_as = entry
    return learn.Proposal(term, heard_as, ok, "")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "learning", tmp_path / "roaming" / "learned_auto.toml")


def second_store(store):
    """Another Store on the same folder, like the Settings process next to the tray process."""
    return Store(store.learning_dir, store.learned_auto_file)


def two_sightings(store, entry=MAILZZY, user=()):
    store.note("rec-1", [prop(entry)], list(user), "first", "2026-11-12 09:00")
    return store.note("rec-2", [prop(entry)], list(user), "second", "2026-11-12 10:00")


def test_entry_key():
    assert entry_key(MAILZZY) == "mail sey -> Mailzzy"
    assert entry_key(KEYCLOAK) == " -> Keycloak"


def test_one_sighting_stays_pending(store):
    became = store.note("rec-1", [prop(MAILZZY)], [], "send the Mailzzy campaign", "2026-11-12 09:00")
    assert became == []
    assert store.active() == []
    assert store.pending_count() == 1
    assert learn.load(store.learned_auto_file) == []


def test_two_recordings_activate(store):
    became = two_sightings(store)
    assert became == [AutoFix("Mailzzy", "mail sey", "2026-11-12 10:00", 2, "second")]
    assert store.active() == became
    assert store.pending_count() == 0


def test_activation_is_reported_once(store):
    two_sightings(store)
    assert store.note("rec-3", [prop(MAILZZY)], [], "third", "2026-11-13 09:00") == []


def test_same_recording_twice_counts_once(store):
    store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:00")
    assert store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:05") == []
    assert store.note("rec-1", [prop(MAILZZY), prop(MAILZZY)], [], "a", "2026-11-12 09:10") == []
    assert store.active() == []
    assert store.pending_count() == 1


def test_seen_grows_with_new_recordings_not_repeats(store):
    two_sightings(store)
    store.note("rec-3", [prop(MAILZZY)], [], "third", "2026-11-13 09:00")
    store.note("rec-3", [prop(MAILZZY)], [], "third", "2026-11-13 09:01")
    [fix] = store.active()
    assert fix.seen == 3
    assert fix.first_learned == "2026-11-12 10:00"
    assert fix.example == "third"


def test_not_ok_proposals_ignored(store):
    store.note("rec-1", [prop(MAILZZY, ok=False)], [], "a", "2026-11-12 09:00")
    store.note("rec-2", [prop(MAILZZY, ok=False)], [], "b", "2026-11-12 10:00")
    assert store.active() == []
    assert store.pending_count() == 0


def test_not_ok_sighting_does_not_count_towards_two(store):
    store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:00")
    assert store.note("rec-2", [prop(MAILZZY, ok=False)], [], "b", "2026-11-12 10:00") == []
    assert store.active() == []
    assert store.pending_count() == 1


def test_clash_with_user_entry_dropped(store):
    user = [("Mail Sea", "mail sey")]            # same heard spelling (word_key), different term
    store.note("rec-1", [prop(MAILZZY)], user, "a", "2026-11-12 09:00")
    assert store.pending_count() == 0
    assert store.note("rec-2", [prop(MAILZZY)], user, "b", "2026-11-12 10:00") == []
    assert store.active() == []
    assert store.pending_count() == 0


def test_clash_ignores_case_and_spacing_of_the_heard_spelling(store):
    two_sightings(store, user=[("Mail Sea", "Mail-Sey")])
    assert store.active() == []


def test_clash_with_user_term_only_entry(store):
    # A user "Keycloak" (no heard_as) is matched by its own key, so "KeyCloak" for it is a clash.
    two_sightings(store, ("KeyCloak", "key cloak"), user=[("Key Cloak", "")])
    assert store.active() == []


def test_clash_dropped_when_it_appears_between_sightings(store):
    store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:00")
    assert store.pending_count() == 1
    assert store.note("rec-2", [prop(MAILZZY)], [("Mail Sea", "mail sey")], "b", "2026-11-12 10:00") == []
    assert store.pending_count() == 0
    assert store.active() == []


def test_user_entry_with_same_term_is_not_a_clash(store):
    became = two_sightings(store, user=[MAILZZY, ("Other", "other thing"), ("Mailzzy", "")])
    assert [(f.term, f.heard_as) for f in became] == [MAILZZY]


def test_undo_removes_and_blocks_relearning(store):
    two_sightings(store)
    store.undo(MAILZZY)
    assert store.active() == []
    assert learn.load(store.learned_auto_file) == []
    for n in range(3, 6):
        assert store.note(f"rec-{n}", [prop(MAILZZY)], [], "again", f"2026-11-1{n} 09:00") == []
    assert store.active() == []
    assert store.pending_count() == 0
    assert learn.load(store.learned_auto_file) == []


def test_undo_of_a_pending_entry_blocks_it_too(store):
    store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:00")
    store.undo(MAILZZY)
    assert store.pending_count() == 0
    assert store.note("rec-2", [prop(MAILZZY)], [], "b", "2026-11-12 10:00") == []
    assert store.active() == []


def test_undo_leaves_other_fixes_alone(store):
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    store.undo(MAILZZY)
    assert [(f.term, f.heard_as) for f in store.active()] == [KEYCLOAK]
    assert learn.load(store.learned_auto_file) == [KEYCLOAK]


def test_undo_blocks_even_a_never_seen_entry(store):
    store.undo(MAILZZY)
    store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:00")
    assert store.pending_count() == 0


def test_reset_all_blocks_everything_it_cleared(store):
    two_sightings(store)                                              # active
    store.note("rec-9", [prop(("DataGrip", ""))], [], "a", "2026-11-12 09:00")   # pending
    store.reset_all()
    assert store.active() == []
    assert store.pending_count() == 0
    assert learn.load(store.learned_auto_file) == []
    for entry in (MAILZZY, ("DataGrip", "")):
        store.note("rec-10", [prop(entry)], [], "x", "2026-11-13 09:00")
        store.note("rec-11", [prop(entry)], [], "y", "2026-11-13 10:00")
    assert store.active() == []
    assert store.pending_count() == 0
    # Anything that was not on the list before is still learnable.
    two_sightings(store, ("Postgres", ""))
    assert [f.term for f in store.active()] == ["Postgres"]


def test_learned_auto_toml_loads_with_learn_load(store):
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    two_sightings(store, ('Say "hi"', 'say \\ "hi"'))
    assert learn.load(store.learned_auto_file) == [
        ("Keycloak", ""), ("Mailzzy", ""), ("Mailzzy", "mail sey"), ('Say "hi"', ""), ('Say "hi"', 'say \\ "hi"')]
    meta = tomllib.loads(store.learned_auto_file.read_text("utf-8"))["meta"]
    assert meta[entry_key(MAILZZY)] == {"first_learned": "2026-11-12 10:00", "seen": 2, "example": "second"}
    assert set(meta) == {entry_key(MAILZZY), entry_key(KEYCLOAK), entry_key(('Say "hi"', 'say \\ "hi"'))}


def test_empty_learned_auto_toml_loads(store):
    two_sightings(store)
    store.undo(MAILZZY)
    assert tomllib.loads(store.learned_auto_file.read_text("utf-8")).get("meta", {}) == {}
    assert learn.load(store.learned_auto_file) == []


def test_load_active_reads_learned_auto_toml(store):
    assert autolearn.load_active(store.learned_auto_file) == []          # not written yet
    two_sightings(store)
    assert autolearn.load_active(store.learned_auto_file) == [("Mailzzy", ""), MAILZZY]
    store.learned_auto_file.write_text("[dictionary\nhalf written", "utf-8")
    assert autolearn.load_active(store.learned_auto_file) == []          # damaged: nothing, never an error


def test_state_persists_across_store_objects(store):
    store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:00")
    other = second_store(store)
    assert other.pending_count() == 1
    other.note("rec-2", [prop(MAILZZY)], [], "b", "2026-11-12 10:00")
    assert [(f.term, f.heard_as) for f in store.active()] == [MAILZZY]


def test_corrupt_state_and_toml_start_empty(store, caplog):
    store.learning_dir.mkdir(parents=True)
    store.learned_auto_file.parent.mkdir(parents=True)
    (store.learning_dir / "state.json").write_bytes(b"\xff\xfe{not json at all")
    store.learned_auto_file.write_text("[dictionary\nterms = garbage", "utf-8")
    (store.learning_dir / "checked.jsonl").write_bytes(b'{"id": "a"}\n\xff\xfe broken\n{"id": ')
    with caplog.at_level("WARNING", logger="parakeet_dictate.autolearn"):
        assert store.active() == []
        assert store.pending_count() == 0
        assert store.status() == ""
        assert store.claude_calls_today("2026-11-12") == 0
        assert store.checked_ids() == {"a"}
    assert "state.json" in caplog.text
    # It keeps working, and the next write replaces the damaged files with good ones.
    assert two_sightings(store)
    assert [(f.term, f.heard_as) for f in store.active()] == [MAILZZY]
    assert learn.load(store.learned_auto_file) == [("Mailzzy", ""), MAILZZY]
    store.append_checked({"id": "b"})
    assert store.checked_ids() == {"a", "b"}


@pytest.mark.parametrize("text", ["[]", "null", "42", '"x"', '{"pending": 5, "active": [1], "never": {"a": 1}, '
                                  '"claude": "x", "status": 7}', "[" * 100000])
def test_wrong_shaped_state_starts_empty(store, text):
    store.learning_dir.mkdir(parents=True)
    (store.learning_dir / "state.json").write_text(text, "utf-8")
    assert store.active() == []
    assert store.pending_count() == 0
    assert store.status() == ""
    assert store.claude_calls_today("2026-11-12") == 0
    store.count_claude_call("2026-11-12")
    assert store.claude_calls_today("2026-11-12") == 1


def test_damaged_entries_are_skipped_but_good_ones_kept(store):
    two_sightings(store)
    path = store.learning_dir / "state.json"
    state = json.loads(path.read_text("utf-8"))
    state["active"]["junk"] = {"term": 5}
    state["pending"]["junk2"] = "x"
    path.write_text(json.dumps(state), "utf-8")
    assert [(f.term, f.heard_as) for f in store.active()] == [MAILZZY]
    assert store.pending_count() == 0


def test_undo_survives_concurrent_note(store):
    settings = second_store(store)
    store.note("rec-1", [prop(MAILZZY)], [], "a", "2026-11-12 09:00")
    settings.undo(MAILZZY)                                  # the Settings window, between two checker writes
    assert store.note("rec-2", [prop(MAILZZY)], [], "b", "2026-11-12 10:00") == []
    assert store.active() == []
    assert learn.load(store.learned_auto_file) == []


def test_undo_of_an_active_fix_survives_concurrent_note(store):
    settings = second_store(store)
    two_sightings(store)
    settings.undo(MAILZZY)
    assert store.note("rec-3", [prop(MAILZZY), prop(KEYCLOAK)], [], "c", "2026-11-13 09:00") == []
    assert store.active() == []
    assert learn.load(store.learned_auto_file) == []
    assert store.pending_count() == 1                       # only Keycloak is still on its way


def test_reset_survives_concurrent_note(store):
    settings = second_store(store)
    two_sightings(store)
    settings.reset_all()
    store.note("rec-3", [prop(MAILZZY)], [], "c", "2026-11-13 09:00")
    assert store.active() == []


def test_lock_excludes_other_stores_and_is_released(store, monkeypatch):
    monkeypatch.setattr(autolearn, "LOCK_WAIT_S", 0.2)
    other = second_store(store)
    with store.lock():
        with pytest.raises(TimeoutError):
            with other.lock():
                pytest.fail("two stores held the lock at once")
        with pytest.raises(TimeoutError):
            other.set_status("blocked")
    with other.lock():                                      # free again after the first one left
        pass
    with pytest.raises(RuntimeError):
        with store.lock():
            raise RuntimeError("boom")
    with other.lock():                                      # and after an error inside it
        pass


def test_lock_creates_the_folder(tmp_path):
    s = Store(tmp_path / "a" / "b", tmp_path / "c" / "learned_auto.toml")
    with s.lock():
        assert (tmp_path / "a" / "b" / "state.lock").exists()


def test_write_retries_while_another_process_reads_the_file(store, monkeypatch):
    store.set_status("one")
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError("held open by a reader")
        real(src, dst)

    monkeypatch.setattr(autolearn.os, "replace", flaky)
    store.set_status("two")
    assert len(calls) == 3
    assert store.status() == "two"


@pytest.fixture
def block_reads(monkeypatch):
    """Reading the file names added to the returned set fails, like a file the other process holds open."""
    blocked = set()
    real_text, real_bytes = pathlib.Path.read_text, pathlib.Path.read_bytes

    def read_text(self, *args, **kwargs):
        if self.name in blocked:
            raise PermissionError(13, "blocked", str(self))
        return real_text(self, *args, **kwargs)

    def read_bytes(self):
        if self.name in blocked:
            raise PermissionError(13, "blocked", str(self))
        return real_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
    monkeypatch.setattr(autolearn.time, "sleep", lambda seconds: None)     # no waiting between retries
    return blocked


WRITE_PATHS = {
    "note": lambda s: s.note("rec-9", [prop(KEYCLOAK)], [], "x", "2026-11-14 09:00"),
    "undo": lambda s: s.undo(KEYCLOAK),
    "reset_all": lambda s: s.reset_all(),
    "count_claude_call": lambda s: s.count_claude_call("2026-11-14"),
    "set_status": lambda s: s.set_status("changed"),
}


@pytest.mark.parametrize("op", WRITE_PATHS)
def test_write_that_cannot_read_state_raises_and_changes_nothing(store, block_reads, op):
    two_sightings(store)                                                      # Mailzzy: active
    store.note("rec-5", [prop(KEYCLOAK)], [], "k", "2026-11-12 11:00")        # Keycloak: pending
    store.undo(("Postgres", ""))                                              # Postgres: never learn
    state_file = store.learning_dir / "state.json"
    before = (state_file.read_bytes(), store.learned_auto_file.read_bytes())
    block_reads.add("state.json")
    with pytest.raises(PermissionError):
        WRITE_PATHS[op](store)
    block_reads.clear()
    assert (state_file.read_bytes(), store.learned_auto_file.read_bytes()) == before
    # The lock was released, the fixes are intact and the never-learn list still works.
    assert [(f.term, f.heard_as) for f in store.active()] == [MAILZZY]
    assert store.pending_count() == 1
    two_sightings(store, ("Postgres", ""))
    assert [f.term for f in store.active()] == ["Mailzzy"]


def test_read_only_methods_show_empty_when_state_cannot_be_read(store, block_reads):
    two_sightings(store)
    store.set_status("hi")
    store.count_claude_call("2026-11-12")
    block_reads.add("state.json")
    assert store.active() == []
    assert store.pending_count() == 0
    assert store.status() == ""
    assert store.claude_calls_today("2026-11-12") == 0
    block_reads.clear()
    assert len(store.active()) == 1 and store.status() == "hi"                # nothing was lost


def test_append_checked_that_cannot_read_raises_and_changes_nothing(store, block_reads):
    store.append_checked({"id": "a"})
    store.append_checked({"id": "b"})
    path = store.learning_dir / "checked.jsonl"
    before = path.read_bytes()
    block_reads.add("checked.jsonl")
    with pytest.raises(PermissionError):
        store.append_checked({"id": "c"})
    assert store.checked_ids() == set()                                       # read-only: empty, no error
    block_reads.clear()
    assert path.read_bytes() == before
    assert store.checked_ids() == {"a", "b"}

def test_checked_jsonl_capped(store, monkeypatch):
    assert autolearn.CHECKED_KEEP == 5000
    monkeypatch.setattr(autolearn, "CHECKED_KEEP", 5)      # 5000 real rewrites take minutes on a busy disk
    for n in range(8):
        store.append_checked({"id": f"rec-{n}", "matched": n % 2 == 0})
    raw = (store.learning_dir / "checked.jsonl").read_bytes()
    assert b"\r" not in raw
    lines = raw.decode("utf-8").split("\n")
    assert lines.pop() == ""                                # ends with a newline, no blank lines in between
    assert len(lines) == 5
    assert json.loads(lines[0])["id"] == "rec-3"
    assert json.loads(lines[-1])["id"] == "rec-7"
    assert store.checked_ids() == {f"rec-{n}" for n in range(3, 8)}


def test_append_checked_goes_through_tmp_and_replace(store, monkeypatch):
    store.append_checked({"id": "a"})
    real, calls = os.replace, []

    def spy(src, dst):
        calls.append((pathlib.Path(src).name, pathlib.Path(dst).name))
        real(src, dst)

    monkeypatch.setattr(autolearn.os, "replace", spy)
    store.append_checked({"id": "b"})
    assert calls == [("checked.jsonl.tmp", "checked.jsonl")]
    assert store.checked_ids() == {"a", "b"}


def test_append_checked_failing_before_the_replace_keeps_the_old_file(store, monkeypatch):
    store.append_checked({"id": "a"})
    path = store.learning_dir / "checked.jsonl"
    before = path.read_bytes()

    def disk_full(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(autolearn.os, "replace", disk_full)
    with pytest.raises(OSError):
        store.append_checked({"id": "b"})
    assert path.read_bytes() == before                      # never a half-written line for checked_ids()


@pytest.mark.parametrize("row", [{}, {"matched": True}, {"id": 7}, {"id": None}])
def test_append_checked_needs_an_id(store, row):
    with pytest.raises(ValueError):
        store.append_checked(row)
    assert not (store.learning_dir / "checked.jsonl").exists()

def test_checked_jsonl_keeps_unicode_and_every_field(store):
    row = {"id": "rec-1", "parakeet": "naïve café\u2028x\u0085y\r\nz", "whisper": "naive cafe", "matched": False,
           "skipped": None}
    store.append_checked(row)
    store.append_checked({"id": "rec-2"})
    first = (store.learning_dir / "checked.jsonl").read_text("utf-8").split("\n")[0]
    assert json.loads(first) == row
    assert store.checked_ids() == {"rec-1", "rec-2"}


def test_checked_ids_ignore_damaged_and_idless_lines(store):
    store.learning_dir.mkdir(parents=True)
    (store.learning_dir / "checked.jsonl").write_text(
        '{"id": "a"}\nnot json\n\n[1, 2]\n{"matched": true}\n{"id": 7}\n{"id": "b"}', "utf-8")
    assert store.checked_ids() == {"a", "b"}
    store.append_checked({"id": "c"})                       # a last line without a newline must not merge
    assert store.checked_ids() == {"a", "b", "c"}


def test_checked_ids_empty_without_a_file(store):
    assert store.checked_ids() == set()


def test_claude_calls_counted_per_day(store):
    assert store.claude_calls_today("2026-11-12") == 0
    store.count_claude_call("2026-11-12")
    store.count_claude_call("2026-11-12")
    assert store.claude_calls_today("2026-11-12") == 2
    assert second_store(store).claude_calls_today("2026-11-12") == 2
    assert store.claude_calls_today("2026-11-13") == 0
    store.count_claude_call("2026-11-13")
    assert store.claude_calls_today("2026-11-13") == 1
    assert store.claude_calls_today("2026-11-12") == 0      # a new day starts again


def test_status_round_trips_and_survives_other_writes(store):
    assert store.status() == ""
    store.set_status("Downloading Whisper 41%")
    assert second_store(store).status() == "Downloading Whisper 41%"
    two_sightings(store)
    store.count_claude_call("2026-11-12")
    assert store.status() == "Downloading Whisper 41%"
    store.set_status("Whisper ready")
    assert store.status() == "Whisper ready"
    assert len(store.active()) == 1 and store.claude_calls_today("2026-11-12") == 1


def test_no_temp_files_left_behind(store):
    two_sightings(store)
    store.set_status("x")
    store.append_checked({"id": "a"})
    leftovers = [p.name for d in (store.learning_dir, store.learned_auto_file.parent)
                 for p in d.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# --- state.json lost (an uninstall deletes it, learned_auto.toml stays) or damaged -------------------------

def lose_state(store, how):
    path = store.learning_dir / "state.json"
    if how == "deleted":
        path.unlink()
    else:
        path.write_bytes(b"\xff{half written")


def pairs(fixes):
    return [(f.term, f.heard_as) for f in fixes]


def sight_again(store, entry, n=2):
    for i in range(n):
        store.note(f"rec-again-{i}", [prop(entry)], [], "again", "2026-11-20 09:00")


@pytest.mark.parametrize("how", ["deleted", "damaged"])
def test_lost_state_lists_the_fixes_the_toml_still_applies(store, how):
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    before = store.active()
    lose_state(store, how)
    assert store.active() == before                       # what, when learned, how often seen, the example
    assert second_store(store).active() == before          # the Settings window sees them too


@pytest.mark.parametrize("how", ["deleted", "damaged"])
def test_lost_state_undo_works_and_stays_undone(store, how):
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    lose_state(store, how)
    store.undo(MAILZZY)
    assert pairs(store.active()) == [KEYCLOAK]
    assert learn.load(store.learned_auto_file) == [KEYCLOAK]   # no longer applied
    sight_again(store, MAILZZY)
    assert pairs(store.active()) == [KEYCLOAK] and store.pending_count() == 0


@pytest.mark.parametrize("how", ["deleted", "damaged"])
def test_lost_state_after_an_undo_keeps_it_blocked(store, how):
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    store.undo(MAILZZY)
    lose_state(store, how)
    assert pairs(store.active()) == [KEYCLOAK]
    sight_again(store, MAILZZY)
    assert pairs(store.active()) == [KEYCLOAK] and store.pending_count() == 0


@pytest.mark.parametrize("how", ["deleted", "damaged"])
def test_lost_state_after_undoing_every_fix_keeps_them_blocked(store, how):
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    store.reset_all()
    lose_state(store, how)
    assert store.active() == []
    sight_again(store, MAILZZY)
    sight_again(store, KEYCLOAK)
    assert store.active() == [] and store.pending_count() == 0


def test_lost_state_reset_all_works(store):
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    lose_state(store, "deleted")
    store.reset_all()
    assert store.active() == [] and learn.load(store.learned_auto_file) == []
    sight_again(store, MAILZZY)
    assert store.active() == []


def test_lost_state_is_not_wiped_by_the_next_write(store):
    two_sightings(store)
    lose_state(store, "deleted")
    store.note("rec-3", [prop(KEYCLOAK)], [], "k", "2026-11-13 09:00")   # one sighting of something else
    assert pairs(store.active()) == [MAILZZY] and learn.load(store.learned_auto_file) == [("Mailzzy", ""), MAILZZY]
    store.note("rec-4", [prop(MAILZZY)], [], "fourth", "2026-11-13 10:00")
    [fix] = store.active()
    assert (fix.seen, fix.first_learned, fix.example) == (3, "2026-11-12 10:00", "fourth")


def test_lost_state_keeps_a_term_only_fix_next_to_a_heard_as_one(store):
    two_sightings(store, ("Mailzzy", ""))                   # learned.toml's shape has no line of its own for it
    two_sightings(store, MAILZZY)
    before = store.active()
    lose_state(store, "deleted")
    assert store.active() == before


def test_never_list_is_kept_in_learned_auto_toml_and_learn_load_ignores_it(store):
    two_sightings(store)
    store.undo(MAILZZY)
    store.undo(("Postgres", ""))
    d = tomllib.loads(store.learned_auto_file.read_text("utf-8"))
    assert d["never"] == {"keys": [entry_key(MAILZZY), entry_key(("Postgres", ""))]}
    assert learn.load(store.learned_auto_file) == []
    two_sightings(store, KEYCLOAK)
    assert learn.load(store.learned_auto_file) == [KEYCLOAK]
    assert autolearn.load_active(store.learned_auto_file) == [KEYCLOAK]


def test_damaged_toml_with_lost_state_starts_empty(store):
    two_sightings(store)
    lose_state(store, "deleted")
    store.learned_auto_file.write_text("[dictionary\nhalf", "utf-8")
    assert store.active() == []
    assert two_sightings(store, KEYCLOAK)                   # and the next write replaces it
    assert learn.load(store.learned_auto_file) == [KEYCLOAK]


# --- learned_auto.toml out of date with state.json (its write failed after state.json's) --------------------

@pytest.fixture
def toml_write_fails(store, monkeypatch):
    """Writing learned_auto.toml raises OSError while the returned list holds True."""
    failing = [True]
    real = autolearn._write

    def write(path, text):
        if failing[0] and path == store.learned_auto_file:
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(autolearn, "_write", write)
    return failing


def test_sync_rewrites_a_toml_left_behind_by_a_failed_undo(store, toml_write_fails):
    toml_write_fails[0] = False
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    toml_write_fails[0] = True
    with pytest.raises(OSError):
        store.undo(MAILZZY)                                  # state.json written, the toml not
    toml_write_fails[0] = False
    assert pairs(store.active()) == [KEYCLOAK]
    assert MAILZZY in learn.load(store.learned_auto_file)   # still applied by the tray...
    store.sync()                                             # ...until the next check starts
    assert learn.load(store.learned_auto_file) == [KEYCLOAK]
    assert pairs(store.active()) == [KEYCLOAK]


def test_a_failed_undo_of_the_last_fix_is_not_rebuilt_from_the_stale_toml(store, toml_write_fails):
    toml_write_fails[0] = False
    two_sightings(store)
    toml_write_fails[0] = True
    with pytest.raises(OSError):
        store.undo(MAILZZY)
    toml_write_fails[0] = False
    assert store.active() == []                              # on the never-learn list in state.json
    store.sync()
    assert learn.load(store.learned_auto_file) == []
    sight_again(store, MAILZZY)
    assert store.active() == []


def test_every_fix_write_resyncs_a_stale_toml(store, toml_write_fails):
    toml_write_fails[0] = False
    two_sightings(store)
    two_sightings(store, KEYCLOAK)
    toml_write_fails[0] = True
    with pytest.raises(OSError):
        store.undo(MAILZZY)
    toml_write_fails[0] = False
    store.note("rec-2", [prop(KEYCLOAK)], [], "k", "2026-11-13 09:00")   # counted already: changes nothing
    assert learn.load(store.learned_auto_file) == [KEYCLOAK]


def test_sync_leaves_an_up_to_date_toml_alone(store, monkeypatch):
    two_sightings(store)
    writes = []
    real = autolearn._write
    monkeypatch.setattr(autolearn, "_write", lambda path, text: (writes.append(path.name), real(path, text)))
    store.sync()
    store.note("rec-2", [prop(MAILZZY)], [], "x", "2026-11-13 09:00")    # counted already
    assert writes == []


def test_sync_saves_a_rebuilt_state(store):
    two_sightings(store)
    store.undo(("Postgres", ""))
    lose_state(store, "deleted")
    store.sync()
    state = json.loads((store.learning_dir / "state.json").read_text("utf-8"))
    assert list(state["active"]) == [entry_key(MAILZZY)] and state["never"] == [entry_key(("Postgres", ""))]


def test_sync_without_any_files_writes_nothing(store):
    store.sync()
    assert not store.learned_auto_file.exists() and not (store.learning_dir / "state.json").exists()
