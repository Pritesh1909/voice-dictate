import json
import sys

import pytest

from parakeet_dictate import config, correct, learn
from parakeet_dictate.correct import CorrectionModel
from parakeet_dictate.dictionary import build


def entries(original, corrected):
    return [p.entry for p in learn.propose(original, corrected)]


def test_heard_as_when_the_key_differs():
    assert entries("Send the mail sey campaign.", "Send the Mailzzy campaign.") == [("Mailzzy", "mail sey")]


def test_term_only_when_the_dictionary_key_already_matches():
    assert entries("Open the key cloak console.", "Open the Keycloak console.") == [("Keycloak", "")]
    assert entries("Push to code commit.", "Push to CodeCommit.") == [("CodeCommit", "")]
    assert entries("the postgres db", "the Postgres db") == [("Postgres", "")]


def test_several_fixes_in_one_correction():
    got = entries("Log into key cloak, then open mail sey and data grip.",
                  "Log into Keycloak, then open Mailzzy and DataGrip.")
    assert got == [("Keycloak", ""), ("Mailzzy", "mail sey"), ("DataGrip", "")]


@pytest.mark.parametrize("original, corrected", [
    ("Deploy core first. then CRM.", "Deploy core first. Then CRM."),     # capital at a sentence start
    ("Deploy core first, then CRM", "Deploy core first; then CRM."),      # punctuation only
    ("So their build is fine.", "So there build is fine."),                # ordinary word swap
    ("deploy it now", "deploy it right now"),                               # inserted word
    ("deploy it right now", "deploy it now"),                               # deleted word
    ("we should deploy the core service first today", "the rollout waits until Monday morning, says Kim"),
    ("same text", "same text"),
])
def test_ignored_changes(original, corrected):
    assert entries(original, corrected) == []


def test_long_rewrite_is_not_a_spelling():
    assert entries("a b c d e f", "a Xx Yy Zz Ww Vv f") == []          # 4 words -> 5 words
    assert entries("a b c d e f", "a Bb Cc Dd Ee f") != []              # 4 -> 4 is still a fix


def test_lower_case_term_is_offered_but_not_ticked():
    [p] = learn.propose("We use PNPM here.", "We use pnpm here.")
    assert p.entry == ("pnpm", "") and not p.checked
    [q] = learn.propose("mail sey", "Mailzzy")
    assert q.checked


def test_word_swap_at_a_sentence_start_is_offered_unticked():
    [p] = learn.propose("Their build is fine.", "There build is fine.")
    assert p.entry == ("There", "Their") and not p.checked


def test_learned_file_round_trip_and_merge(tmp_path):
    f = tmp_path / "learned.toml"
    assert learn.load(f) == []
    learn.add(f, [("Mailzzy", "mail sey"), ("Keycloak", ""), ('Odd "quote"\\', "")])
    learn.add(f, [("Mailzzy", "mail say"), ("Keycloak", "")])
    got = learn.load(f)
    assert ("Keycloak", "") in got and ('Odd "quote"\\', "") in got
    assert ("Mailzzy", "mail sey") in got and ("Mailzzy", "mail say") in got
    assert got.count(("Keycloak", "")) == 1
    text = f.read_text("utf-8")
    assert text.startswith("# Words learned") and "[dictionary.heard_as]" in text
    assert build(got)("open key cloak and mail say") == "open Keycloak and Mailzzy"


def test_broken_learned_file_is_ignored(tmp_path):
    f = tmp_path / "learned.toml"
    f.write_text("[dictionary\nterms = 3", "utf-8")
    assert learn.load(f) == []


def test_model_keeps_ticks_and_skips_known_entries():
    m = CorrectionModel("open key cloak and mail sey", known=[("Keycloak", "")])
    m.update("open Keycloak and Mailzzy")
    assert [p.entry for p in m.proposals] == [("Mailzzy", "mail sey")]
    m.toggle(0)
    assert m.selected() == []
    m.update("open Keycloak and Mailzzy now")         # still there: keeps the untick
    assert m.selected() == []
    m.toggle(0, True)
    assert m.selected() == [("Mailzzy", "mail sey")]


def test_request_file_is_deleted_once_read(tmp_path):
    p = correct.write_request(tmp_path, "secret dictation")
    assert correct.read_request(p) == "secret dictation"
    assert not p.exists()


def test_launch_starts_a_process_without_waiting(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\ParakeetDictate.exe")
    correct.launch("text", tmp_path, popen=lambda argv, **kw: calls.append((argv, kw)))
    [(argv, kw)] = calls
    assert argv[:2] == [r"C:\Apps\ParakeetDictate.exe", "--correct"]
    assert json.loads(open(argv[2], encoding="utf-8").read()) == {"text": "text"}
    monkeypatch.setattr(sys, "frozen", False)
    argv, cwd = correct.command(tmp_path / "r.json")
    assert argv[1:4] == ["-m", "parakeet_dictate", "--correct"] and cwd


def test_failed_launch_removes_the_request(tmp_path):
    def boom(argv, **kw):
        raise OSError("no")
    with pytest.raises(OSError):
        correct.launch("text", tmp_path, popen=boom)
    assert list(tmp_path.iterdir()) == []


def test_run_saves_ticked_entries(tmp_path, monkeypatch):
    paths = config.Paths(tmp_path / "roaming", tmp_path / "local")
    paths.ensure()
    req = correct.write_request(paths.data_dir, "open the san jo app and key cloak")

    def fake_show(model):        # stands in for the Tk window: the user fixes the text and saves
        model.update("open the Zanjo app and Keycloak")
        return True
    monkeypatch.setattr(correct, "_show", fake_show)
    assert correct.run(paths, req) == 0
    # Keycloak is already in the default [dictionary], so only the new word is learned.
    assert learn.load(paths.learned_file) == [("Zanjo", ""), ("Zanjo", "san jo")]
    assert not req.exists()
