import json
import os
import subprocess
from pathlib import Path

import pytest

from parakeet_dictate import judge, learn
from parakeet_dictate.judge import JudgeUnavailable, Pair

FAKE_CLAUDE = r"C:\fake\claude.exe"


def reply(items, **extra):
    """What `claude -p --output-format json` prints when the schema was followed."""
    body = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"items": items}}
    body.update(extra)
    return json.dumps(body)


def done(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["claude"], returncode, stdout=stdout, stderr=stderr)


class FakeRun:
    """Stands in for subprocess.run: records every call and never starts a process."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []        # (args, kwargs)
        self.cwd_listing = []  # what was inside the working folder at call time

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        cwd = kwargs.get("cwd")
        self.cwd_listing.append(os.listdir(cwd) if cwd and Path(cwd).is_dir() else None)
        return self.answer(args, kwargs) if callable(self.answer) else self.answer


def sent_ids(kwargs):
    return [p["id"] for p in json.loads(kwargs["input"])["pairs"]]


def ask(pairs, run, terms=(), **kw):
    return judge.ask_claude(pairs, list(terms), run=run, claude=FAKE_CLAUDE, **kw)


def test_ask_claude_builds_safe_command():
    run = FakeRun(done(reply([{"id": 1, "correct": "Open Mailzzy."}])))
    ask([Pair(1, "Open mail sey.", "Open Mailzzy.")], run, terms=["Mailzzy"])

    (args, kwargs), = run.calls
    assert args[0] == FAKE_CLAUDE
    assert "-p" in args
    assert "--strict-mcp-config" in args
    assert "--no-session-persistence" in args
    assert "--bare" not in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--setting-sources") + 1] == "local"
    assert args[args.index("--system-prompt") + 1].strip()
    # Pinned, so a judging call doesn't run on the user's default (possibly more expensive) model.
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--effort") + 1] == "low"
    assert json.loads(args[args.index("--json-schema") + 1]) == judge.SCHEMA
    assert kwargs["timeout"] == 120
    # An empty folder of its own, so no project CLAUDE.md or settings are picked up; gone again afterwards.
    assert run.cwd_listing == [[]]
    assert not Path(kwargs["cwd"]).exists()


def test_only_text_is_sent_and_on_stdin():
    run = FakeRun(done(reply([])))
    ask([Pair(7, "alpha beta", "alpha gamma")], run, terms=["Keycloak", "Mailzzy"])

    args, kwargs = run.calls[0]
    assert not any("alpha" in a for a in args)
    sent = json.loads(kwargs["input"])
    assert sent["pairs"] == [{"id": 7, "a": "alpha beta", "b": "alpha gamma"}]
    assert sent["terms"] == judge.prompt_terms(["Keycloak", "Mailzzy"]) == "Keycloak, Mailzzy"


def test_ask_claude_returns_only_sent_ids():
    run = FakeRun(done(reply([{"id": 1, "correct": "one"}, {"id": 99, "correct": "ghost"}])))
    assert ask([Pair(1, "a", "b"), Pair(2, "c", "d")], run) == {1: "one"}


def test_null_means_unsure():
    run = FakeRun(done(reply([{"id": 1, "correct": None}, {"id": 2, "correct": "two"}, {"id": 3, "correct": "  "}])))
    got = ask([Pair(1, "a", "b"), Pair(2, "c", "d"), Pair(3, "e", "f")], run)
    assert got == {2: "two"}


def test_a_pair_left_out_of_the_reply_is_unsure():
    run = FakeRun(done(reply([{"id": 2, "correct": "two"}])))
    assert ask([Pair(1, "a", "b"), Pair(2, "c", "d")], run) == {2: "two"}


def test_no_pairs_asks_nothing():
    run = FakeRun(done(reply([])))
    assert judge.ask_claude([], ["Mailzzy"], run=run, claude=None) == {}
    assert run.calls == []


@pytest.mark.parametrize("stdout", [
    "not json at all",
    "",
    "[1, 2, 3]",
    json.dumps({"is_error": False, "result": "plain text, no structured output"}),
    json.dumps({"is_error": False, "structured_output": {"nope": []}}),
    json.dumps({"is_error": False, "structured_output": {"items": "oops"}}),
    json.dumps({"is_error": False, "structured_output": {"items": [{"id": "1", "correct": "x"}]}}),
    json.dumps({"is_error": False, "structured_output": {"items": [{"id": 1, "correct": 5}]}}),
    json.dumps({"is_error": False, "structured_output": {"items": [{"id": 1}]}}),
    json.dumps({"is_error": False, "structured_output": {"items": [{"id": True, "correct": "x"}]}}),
    json.dumps({"is_error": False, "structured_output": {"items": ["x"]}}),
    json.dumps({"is_error": True, "result": "Not logged in", "structured_output": {"items": []}}),
])
def test_bad_json_raises_unavailable(stdout):
    with pytest.raises(JudgeUnavailable):
        ask([Pair(1, "a", "b")], FakeRun(done(stdout)))


def test_failed_call_raises_unavailable():
    with pytest.raises(JudgeUnavailable):
        ask([Pair(1, "a", "b")], FakeRun(done(reply([]), returncode=1, stderr="boom")))


def test_timeout_raises_unavailable():
    def hang(args, kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    with pytest.raises(JudgeUnavailable):
        ask([Pair(1, "a", "b")], FakeRun(hang))


def test_cannot_start_raises_unavailable():
    def cannot(args, kwargs):
        raise FileNotFoundError(args[0])

    with pytest.raises(JudgeUnavailable):
        ask([Pair(1, "a", "b")], FakeRun(cannot))


def test_missing_claude_raises_unavailable(monkeypatch):
    monkeypatch.setattr(judge.shutil, "which", lambda name: None)
    run = FakeRun(done(reply([])))
    with pytest.raises(JudgeUnavailable):
        judge.ask_claude([Pair(1, "a", "b")], [], run=run)   # claude=None -> looked up on PATH
    assert run.calls == []


def test_claude_found_on_path(monkeypatch):
    monkeypatch.setattr(judge.shutil, "which", lambda name: FAKE_CLAUDE if name == "claude" else None)
    run = FakeRun(done(reply([{"id": 1, "correct": "x"}])))
    assert judge.ask_claude([Pair(1, "a", "b")], [], run=run) == {1: "x"}
    assert run.calls[0][0][0] == FAKE_CLAUDE


def test_batches_of_30():
    def answer(args, kwargs):
        return done(reply([{"id": i, "correct": f"text {i}"} for i in sent_ids(kwargs)]))

    run = FakeRun(answer)
    pairs = [Pair(i, f"heard {i}", f"said {i}") for i in range(65)]
    got = ask(pairs, run)

    assert [len(sent_ids(kw)) for _, kw in run.calls] == [30, 30, 5]
    assert [i for _, kw in run.calls for i in sent_ids(kw)] == list(range(65))
    assert got == {i: f"text {i}" for i in range(65)}
    assert run.cwd_listing == [[], [], []]


def test_exactly_30_pairs_is_one_call():
    run = FakeRun(done(reply([])))
    ask([Pair(i, "a", "b") for i in range(30)], run)
    assert len(run.calls) == 1


def test_an_id_from_another_batch_is_not_accepted():
    # The second call answers for id 0, which only went out in the first call.
    replies = iter([done(reply([{"id": 0, "correct": "zero"}])), done(reply([{"id": 0, "correct": "again"}]))])
    run = FakeRun(lambda args, kwargs: next(replies))
    got = ask([Pair(i, "a", "b") for i in range(31)], run)
    assert got == {0: "zero"}


def test_a_failing_batch_raises_unavailable():
    replies = iter([done(reply([])), done("garbage")])
    with pytest.raises(JudgeUnavailable):
        ask([Pair(i, "a", "b") for i in range(31)], FakeRun(lambda args, kwargs: next(replies)))


def test_prompt_terms_cut_at_whole_term():
    terms = ["Mailzzy", "Keycloak", "DataGrip"]
    assert judge.prompt_terms(terms) == "Mailzzy, Keycloak, DataGrip"
    assert judge.prompt_terms(terms, max_chars=17) == "Mailzzy, Keycloak"
    assert judge.prompt_terms(terms, max_chars=26) == "Mailzzy, Keycloak"   # "DataGrip" would end mid-term
    assert judge.prompt_terms(terms, max_chars=27) == "Mailzzy, Keycloak, DataGrip"
    assert judge.prompt_terms(terms, max_chars=6) == ""                     # not even the first one fits
    assert judge.prompt_terms([]) == ""


def test_prompt_terms_keep_order_skip_blanks_and_default_to_600():
    assert judge.prompt_terms(["Zed", " ", "", "Alpha"]) == "Zed, Alpha"
    many = [f"Term{i:03d}" for i in range(200)]
    text = judge.prompt_terms(many)
    assert len(text) <= 600
    assert text.split(", ") == many[: len(text.split(", "))]
    assert len(text.split(", ")) > 50


def test_known_term_fixes_only_known_terms():
    fixes = judge.known_term_fixes("Open the MailT campaign.", "Open the Mailzzy campaign.", {"Mailzzy"})
    assert [(p.term, p.heard_as) for p in fixes] == [("Mailzzy", "MailT")]

    # Same difference, but the user doesn't know this term: not accepted.
    assert judge.known_term_fixes("Open the MailT campaign.", "Open the Mailzzy campaign.", {"Keycloak"}) == []
    assert judge.known_term_fixes("Open the MailT campaign.", "Open the Mailzzy campaign.", set()) == []
    # The term must match exactly, not just in lower case.
    assert judge.known_term_fixes("Open the MailT campaign.", "Open the Mailzzy campaign.", {"mailzzy"}) == []


def test_known_term_fixes_never_swaps_ordinary_words():
    assert judge.known_term_fixes("Please poll the branch.", "Please pull the branch.", {"pull", "poll"}) == []


def test_known_term_fixes_skips_unticked_proposals():
    # learn proposes "The" -> "the" mid-sentence but leaves it unticked: it would lower-case sentence starts.
    proposed = learn.propose("Open The console.", "Open the console.")
    assert proposed and not proposed[0].checked
    assert judge.known_term_fixes("Open The console.", "Open the console.", {"the"}) == []


def test_known_term_fixes_term_only_spelling():
    fixes = judge.known_term_fixes("Log into key cloak now.", "Log into Keycloak now.", {"Keycloak", "Mailzzy"})
    assert [p.entry for p in fixes] == [("Keycloak", "")]
