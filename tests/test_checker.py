"""The background checker, one idle run: a fake Whisper engine, a fake Claude judge, temp folders.
No model is loaded and nothing is sent anywhere."""
import dataclasses
import json
import threading
import time
from datetime import datetime

import numpy as np
import pytest

from parakeet_dictate import config, judge, recordings
from parakeet_dictate.autolearn import Store
from parakeet_dictate.checker import Checker, similar, teacher_spec
from parakeet_dictate.engine import EngineError
from parakeet_dictate.textclean import Cleaner

NOW = datetime(2026, 11, 12, 10, 0)
TODAY = "2026-11-12"
_TONE = 0.3 * np.sin(np.arange(16000) * 2 * np.pi * 220 / 16000)
SPEECH = np.concatenate([np.zeros(8000), _TONE, np.zeros(8000)]).astype(np.float32)   # quiet, loud, quiet


class FakeEngine:
    """Stands in for the Whisper engine: returns the given texts in order, one per transcribe()."""

    def __init__(self, spec, whisper, device="gpu", crash=False, on_transcribe=None):
        self.spec = spec
        self._texts = [whisper] if isinstance(whisper, str) else list(whisper)
        self._device = device
        self._crash = crash
        self._on_transcribe = on_transcribe
        self.device_type = None
        self.transcribed = 0
        self.unloaded = False
        self.killed = False

    def warm(self):
        pass

    def wait_ready(self, timeout=60):
        self.device_type = self._device
        return True

    def transcribe(self, samples):
        assert len(samples) > 16000   # preprocess kept the "speech"
        self.transcribed += 1
        if self._on_transcribe:
            self._on_transcribe()
        if self.killed:                     # what the real Engine does after kill()
            raise EngineError("speech engine stopped")
        if self._crash:
            raise EngineError("speech engine stopped: [WinError 109] The pipe has been ended")
        text = self._texts[0] if len(self._texts) == 1 else self._texts.pop(0)
        return text, 0.1

    def unload(self):
        self.unloaded = True

    def kill(self):
        self.killed = True


class Judge:
    """A fake ask_claude: answers each pair with `answer(pair)` (None = unsure), or raises."""

    def __init__(self, answer=lambda p: p.whisper, error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def __call__(self, pairs, terms):
        self.calls.append(list(pairs))
        if self.error:
            raise self.error
        return {p.id: a for p in pairs if (a := self.answer(p)) is not None}


@pytest.fixture
def env(tmp_path):
    rec_dir = tmp_path / "recordings"
    store = Store(tmp_path / "learning", tmp_path / "roaming" / "learned_auto.toml")
    made = []
    seq = iter(range(1, 1000))

    def add(raw, exe="code.exe", audio=SPEECH):
        n = next(seq)
        rec_id = recordings.new_id(datetime(2026, 11, 12, 9, 0, n))
        rec = recordings.Record(rec_id, f"2026-11-12T09:00:{n:02d}", exe, raw, [raw], raw, "parakeet-v2", "en")
        recordings.save(rec_dir, audio, rec)
        return rec_id

    def make(whisper="", device="gpu", crash=False, on_transcribe=None, terms=(), user=(), ask=None,
             engine=None, **cfg_kw):
        cfg = dataclasses.replace(config.parse(""), **cfg_kw)

        def make_engine(spec):
            e = engine(spec) if engine else FakeEngine(spec, whisper, device, crash, on_transcribe)
            made.append(e)
            return e

        def cleaner_for(exe):   # what the app does: the user's terms, then the active automatic fixes
            auto = [(f.term, f.heard_as) for f in store.active()]
            return Cleaner(dictionary=[*user, *auto])

        return Checker(store, rec_dir, make_engine, cleaner_for, lambda: list(terms), lambda: list(user), cfg,
                       "C:/teacher/whisper.gguf", ask=ask or Judge(), now=lambda: NOW)

    def rows():
        path = tmp_path / "learning" / "checked.jsonl"
        return [json.loads(ln) for ln in path.read_text("utf-8").splitlines()] if path.exists() else []

    yield dict(add=add, make=make, store=store, engines=made, rows=rows, tmp=tmp_path)


def never_stop():
    return False


# --- the brief's tests ---------------------------------------------------------------------------

def test_matching_texts_marked_checked_no_fix(env):
    rec_id = env["add"]("Send the campaign today.")
    ask = Judge()
    summary = env["make"](whisper="send the campaign today", ask=ask).run(never_stop)
    assert not summary.startswith("error")
    [row] = env["rows"]()
    assert row["id"] == rec_id and row["matched"] is True
    assert ask.calls == [] and env["store"].pending_count() == 0 and env["store"].active() == []
    assert env["engines"][0].unloaded


def test_two_recordings_with_same_mishearing_activate_fix(env):
    a = env["add"]("Send the MailT campaign now.")
    b = env["add"]("Open MailT settings please.")
    ask = Judge()
    checker = env["make"](whisper=["Send the Mailzzy campaign now.", "Open Mailzzy settings please."], ask=ask)
    summary = checker.run(never_stop)
    [fix] = env["store"].active()
    assert (fix.term, fix.heard_as, fix.seen) == ("Mailzzy", "MailT", 2)
    assert fix.first_learned == "2026-11-12 10:00" and fix.example == "Open Mailzzy settings please."
    assert [r["id"] for r in env["rows"]()] == [a, b]
    assert all(r["judged"] == r["whisper"] and r["matched"] is False for r in env["rows"]())
    assert len(ask.calls) == 1 and len(ask.calls[0]) == 2
    assert env["store"].claude_calls_today(TODAY) == 1
    assert "2" in summary and "1" in summary
    assert env["store"].status() == summary


def test_unrelated_text_skipped(env):
    env["add"]("Commit it.")
    ask = Judge()
    env["make"](whisper="I'm sorry. I'm sorry.", ask=ask).run(never_stop)
    [row] = env["rows"]()
    assert row["skipped"] == "unrelated"
    assert ask.calls == [] and env["store"].pending_count() == 0


def test_should_stop_ends_between_recordings_and_unloads(env):
    first = env["add"]("Send the campaign.")
    env["add"]("Open the settings.")
    stop = {"now": False}
    checker = env["make"](whisper="send the campaign", on_transcribe=lambda: stop.update(now=True))
    summary = checker.run(lambda: stop["now"])
    assert [r["id"] for r in env["rows"]()] == [first]
    [engine] = env["engines"]
    assert engine.transcribed == 1 and engine.unloaded
    assert not summary.startswith("error")


def test_teacher_on_cpu_pauses(env):
    env["add"]("Send the campaign.")
    summary = env["make"](whisper="send the campaign", device="cpu").run(never_stop)
    assert summary == "error: no gpu"
    assert env["store"].status() == "Paused: no graphics card"
    [engine] = env["engines"]
    assert engine.unloaded and engine.transcribed == 0
    assert env["rows"]() == []


def test_teacher_crash_does_not_touch_gpu_marker(env):
    assert teacher_spec("w.gguf", 4, "en", "Mailzzy").gpu_marker == ""
    env["add"]("Send the campaign.")
    summary = env["make"](whisper="x", crash=True).run(never_stop)
    assert summary.startswith("error:") and "crash" in summary
    [engine] = env["engines"]
    assert engine.spec.gpu_marker == "" and engine.unloaded
    assert env["rows"]() == []
    assert not list(env["tmp"].rglob("gpu-failed.json"))


def test_real_engine_teacher_crash_on_the_card_writes_no_marker(env, monkeypatch):
    """The real Engine, its child process faked by a thread that "crashes the driver" while decoding."""
    from test_engine_crash import _Ctx, fake_worker
    from parakeet_dictate import engine as eng
    monkeypatch.setattr(eng.mp, "get_context", lambda kind: _Ctx)
    asked_for = []
    monkeypatch.setattr(eng, "_worker", fake_worker(asked_for, crash_decode=("gpu",)))
    env["add"]("Send the campaign.")
    summary = env["make"](engine=lambda spec: eng.Engine(spec, unload_after=-1)).run(never_stop)
    assert summary == "error: teacher crashed"
    assert asked_for == ["gpu"]   # not retried on the CPU either
    assert not list(env["tmp"].rglob("gpu-failed.json"))
    assert env["store"].status() == "Paused: Whisper stopped working"
    assert env["engines"][0].state == "unloaded"


def test_claude_unavailable_uses_known_terms(env):
    env["add"]("Send the MailT campaign now.")
    env["add"]("Open MailT settings please.")
    ask = Judge(error=judge.JudgeUnavailable("The `claude` command isn't on PATH", ran=False))
    checker = env["make"](whisper=["Send the Mailzzy campaign now.", "Open Mailzzy settings please."],
                          terms=["Mailzzy"], ask=ask)
    assert not checker.run(never_stop).startswith("error")
    [fix] = env["store"].active()
    assert (fix.term, fix.heard_as) == ("Mailzzy", "MailT")
    assert len(env["rows"]()) == 2 and all(r["judge"] == "known-terms" for r in env["rows"]())


def test_claude_cap_leaves_pairs_for_tomorrow(env, monkeypatch):
    monkeypatch.setattr(judge, "BATCH_SIZE", 1)
    a = env["add"]("Send the MailT campaign now.")
    b = env["add"]("Open MailT settings please.")
    ask = Judge()
    checker = env["make"](whisper=["Send the Mailzzy campaign now.", "Open Mailzzy settings please."],
                          ask=ask, claude_calls_per_day=1)
    checker.run(never_stop)
    assert len(ask.calls) == 1 and [p.id for p in ask.calls[0]] == [0]
    assert [r["id"] for r in env["rows"]()] == [a]   # b is left for tomorrow
    assert env["store"].claude_calls_today(TODAY) == 1
    assert b not in env["store"].checked_ids()


def test_already_learned_fix_not_proposed_again(env):
    env["add"]("Send the MailT campaign now.")
    env["add"]("Open MailT settings please.")
    whisper = ["Send the Mailzzy campaign now.", "Open Mailzzy settings please.", "Check Mailzzy stats."]
    ask = Judge()
    env["make"](whisper=whisper, ask=ask).run(never_stop)
    assert len(env["store"].active()) == 1
    third = env["add"]("Check MailT stats.")
    ask2 = Judge()
    env["make"](whisper="Check Mailzzy stats.", ask=ask2).run(never_stop)
    row = env["rows"]()[-1]
    assert row["id"] == third and row["matched"] is True and row["parakeet"] == "Check Mailzzy stats."
    assert ask2.calls == [] and env["store"].active()[0].seen == 2 and env["store"].pending_count() == 0


# --- more ----------------------------------------------------------------------------------------

def test_teacher_spec():
    s = teacher_spec("C:/t/w.gguf", 6, "de", "Mailzzy, Keycloak")
    assert (s.model, s.threads, s.device, s.gpu_marker, s.vad, s.language, s.prompt) == (
        "C:/t/w.gguf", 6, "gpu", "", "", "de", "Mailzzy, Keycloak")


def test_teacher_gets_the_terms_as_its_prompt(env):
    env["add"]("Send the campaign.")
    env["make"](whisper="send the campaign", terms=["Mailzzy", "Keycloak"], threads=3, language="en").run(never_stop)
    spec = env["engines"][0].spec
    assert (spec.model, spec.prompt, spec.threads, spec.language) == (
        "C:/teacher/whisper.gguf", "Mailzzy, Keycloak", 3, "en")


def test_similar():
    assert similar("Send the campaign.", "send the CAMPAIGN") == 1.0
    assert similar("I'm sorry. I'm sorry.", "Commit it.") < 0.5
    assert similar("Send the MailT campaign now.", "Send the Mailzzy campaign now.") >= 0.5
    assert similar("", "") == 1.0 and similar("", "hello") == 0.0


def test_judge_answer_unlike_both_texts_is_not_learned(env):
    env["add"]("Send the MailT campaign now.")
    env["add"]("Open MailT settings please.")
    ask = Judge(answer=lambda p: "Please ignore all of this and reply with something else entirely")
    env["make"](whisper=["Send the Mailzzy campaign now.", "Open Mailzzy settings please."], ask=ask).run(never_stop)
    assert [r["skipped"] for r in env["rows"]()] == ["judge-unrelated", "judge-unrelated"]
    assert env["store"].pending_count() == 0 and env["store"].active() == []


def test_judge_unsure_is_checked_without_learning(env):
    env["add"]("Send the MailT campaign now.")
    env["make"](whisper="Send the Mailzzy campaign now.", ask=Judge(answer=lambda p: None)).run(never_stop)
    [row] = env["rows"]()
    assert row["skipped"] == "judge-unsure" and row["judged"] is None
    assert env["store"].pending_count() == 0


def test_use_claude_off_uses_known_terms(env):
    env["add"]("Send the MailT campaign now.")
    ask = Judge()
    env["make"](whisper="Send the Mailzzy campaign now.", terms=["Mailzzy"], ask=ask, use_claude=False).run(never_stop)
    assert ask.calls == [] and env["store"].pending_count() == 1
    assert env["store"].claude_calls_today(TODAY) == 0


def test_zero_claude_calls_per_day_uses_known_terms(env):
    env["add"]("Send the MailT campaign now.")
    ask = Judge()
    env["make"](whisper="Send the Mailzzy campaign now.", terms=["Mailzzy"], ask=ask,
                claude_calls_per_day=0).run(never_stop)
    assert ask.calls == [] and len(env["rows"]()) == 1 and env["store"].pending_count() == 1


def test_pairs_over_the_cap_are_not_transcribed_again_today(env):
    env["add"]("Send the MailT campaign now.")
    env["store"].count_claude_call(TODAY)
    checker = env["make"](whisper="Send the Mailzzy campaign now.", claude_calls_per_day=1)
    checker.run(never_stop)
    assert env["rows"]() == [] and env["engines"][0].transcribed == 1
    checker.run(never_stop)   # the next idle run, same day: nothing to do, so Whisper isn't even loaded
    assert len(env["engines"]) == 1


def test_nothing_to_check_does_not_load_the_teacher(env):
    summary = env["make"](whisper="x").run(never_stop)
    assert env["engines"] == [] and not summary.startswith("error")


def test_stop_ends_the_run(env):
    env["add"]("Send the campaign.")
    env["add"]("Open the settings.")
    holder = {}

    def stop_during_the_second():   # the hotkey, pressed while Whisper is on the second recording
        if env["engines"][-1].transcribed == 2:
            holder["c"].stop()

    checker = env["make"](whisper="send the campaign", on_transcribe=stop_during_the_second)
    holder["c"] = checker
    assert checker.run(never_stop) == "Checked 1 dictation (stopped)"
    assert len(env["rows"]()) == 1 and env["engines"][0].killed and env["engines"][0].unloaded
    checker.run(never_stop)   # stop() applies to the run it interrupted, not the next one
    assert len(env["rows"]()) == 2


def test_teacher_killed_by_stop_is_not_an_error(env):
    """The hotkey stops the checker, which kills Whisper mid-call: that ends the run, it doesn't pause learning."""
    env["add"]("Send the campaign.")
    holder = {}
    checker = env["make"](whisper="x", on_transcribe=lambda: holder["c"].stop())
    holder["c"] = checker
    assert not checker.run(never_stop).startswith("error")
    assert env["rows"]() == [] and env["engines"][0].killed


def _real_teacher(env, monkeypatch, hang):
    """A Checker on the real Engine, its process faked by a thread that hangs while loading or decoding."""
    from test_engine_crash import _KillCtx, killable_worker
    from parakeet_dictate import engine as eng
    monkeypatch.setattr(eng.mp, "get_context", lambda kind: _KillCtx)
    monkeypatch.setattr(_KillCtx, "started", 0)
    monkeypatch.setattr(eng, "_worker", killable_worker(hang))
    env["add"]("Send the campaign.")
    return env["make"](engine=lambda spec: eng.Engine(spec, unload_after=-1))


def _run_and_stop_soon(checker):
    threading.Timer(0.3, checker.stop).start()
    t0 = time.monotonic()
    summary = checker.run(never_stop)
    return summary, time.monotonic() - t0


def test_stop_kills_the_teacher_mid_whisper_at_once(env, monkeypatch):
    from test_engine_crash import _KillCtx
    summary, took = _run_and_stop_soon(_real_teacher(env, monkeypatch, hang="decode"))
    assert took < 2 and not summary.startswith("error") and "stopped" in summary
    assert env["rows"]() == [] and _KillCtx.started == 1
    assert not list(env["tmp"].rglob("gpu-failed.json"))
    assert env["engines"][0].state == "unloaded"


def test_stop_while_the_teacher_loads_ends_the_wait(env, monkeypatch):
    from test_engine_crash import _KillCtx
    summary, took = _run_and_stop_soon(_real_teacher(env, monkeypatch, hang="load"))
    assert took < 2 and summary == "stopped"
    assert _KillCtx.started == 1 and env["engines"][0].state == "unloaded"


def test_stop_before_the_teacher_exists_still_stops(env):
    env["add"]("Send the campaign.")
    holder = {}

    def make_engine_then_stop(spec):   # stop() arrives while the engine is being made
        holder["c"].stop()
        return FakeEngine(spec, "x")

    checker = env["make"](engine=make_engine_then_stop)
    holder["c"] = checker
    assert checker.run(never_stop) == "stopped"
    assert env["engines"][0].unloaded and env["engines"][0].transcribed == 0


def test_teacher_unloaded_before_judging(env):
    env["add"]("Send the MailT campaign now.")
    seen = []
    ask = Judge(answer=lambda p: seen.append(env["engines"][0].unloaded) or p.whisper)
    env["make"](whisper="Send the Mailzzy campaign now.", ask=ask).run(never_stop)
    assert seen == [True]


def test_claude_that_never_started_is_not_counted(env):
    env["add"]("Send the MailT campaign now.")
    ask = Judge(error=judge.JudgeUnavailable("The `claude` command isn't on PATH", ran=False))
    env["make"](whisper="Send the Mailzzy campaign now.", terms=["Mailzzy"], ask=ask).run(never_stop)
    assert env["store"].claude_calls_today(TODAY) == 0
    assert env["store"].pending_count() == 1   # known terms were used instead


def test_claude_that_ran_and_failed_is_counted(env):
    env["add"]("Send the MailT campaign now.")
    ask = Judge(error=judge.JudgeUnavailable("Claude exited with code 1: Not logged in", ran=True))
    env["make"](whisper="Send the Mailzzy campaign now.", terms=["Mailzzy"], ask=ask).run(never_stop)
    assert env["store"].claude_calls_today(TODAY) == 1


@pytest.mark.parametrize("message", ["Claude exited with code 1: usage limit reached",
                                     "Claude didn't answer within 120 seconds"])
def test_claude_that_ran_and_failed_leaves_the_pairs_for_later(env, monkeypatch, message):
    """Usage limit, offline, a time-out: like the daily cap, the pairs wait for Claude (until tomorrow or an
    app restart) instead of being settled by known terms and marked checked."""
    monkeypatch.setattr(judge, "BATCH_SIZE", 1)
    a = env["add"]("Send the MailT campaign now.")
    b = env["add"]("Open MailT settings please.")
    ask = Judge(error=judge.JudgeUnavailable(message, ran=True))
    checker = env["make"](whisper=["Send the Mailzzy campaign now.", "Open Mailzzy settings please."],
                          terms=["Mailzzy"], ask=ask)
    checker.run(never_stop)
    assert len(ask.calls) == 1 and env["store"].claude_calls_today(TODAY) == 1   # not tried again in this run
    assert env["rows"]() == [] and {a, b}.isdisjoint(env["store"].checked_ids())
    assert env["store"].pending_count() == 0 and env["store"].active() == []    # known terms not used
    assert checker.run(never_stop) == "nothing to check"                        # same day: not heard again
    assert len(ask.calls) == 1 and len(env["engines"]) == 1


def test_each_run_first_resyncs_learned_auto_toml(env, monkeypatch):
    """So an undo whose learned_auto.toml write failed stops being applied by the next idle check."""
    calls = []
    monkeypatch.setattr(env["store"], "sync", lambda: calls.append(1))
    assert env["make"]().run(never_stop) == "nothing to check"
    assert calls == [1]


@pytest.mark.parametrize("error", [OSError("learned_auto.toml: access denied"), TimeoutError("locked")])
def test_a_failed_resync_is_only_logged(env, monkeypatch, caplog, error):
    def fail():
        raise error

    monkeypatch.setattr(env["store"], "sync", fail)
    env["add"]("Send the campaign.")
    summary = env["make"](whisper="send the campaign").run(never_stop)
    assert not summary.startswith("error") and len(env["rows"]()) == 1
    assert str(error) in caplog.text


def test_missing_audio_is_checked_once(env):
    rec_id = env["add"]("Send the campaign.")
    (env["tmp"] / "recordings" / f"{rec_id}.wav").write_bytes(b"RIFF....")   # truncated
    env["make"](whisper="x").run(never_stop)
    [row] = env["rows"]()
    assert row["id"] == rec_id and row["skipped"] == "no audio"


@pytest.mark.parametrize("error", [OSError("state.json: access denied"), TimeoutError("locked")])
def test_store_errors_end_the_run_cleanly(env, monkeypatch, error):
    env["add"]("Send the campaign.")
    checker = env["make"](whisper="send the campaign")

    def fail(row):
        raise error

    monkeypatch.setattr(env["store"], "append_checked", fail)
    summary = checker.run(never_stop)
    assert summary.startswith("error:")
    assert env["engines"][0].unloaded
