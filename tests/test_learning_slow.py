"""Learning in the background, end to end with the real models.

Windows text-to-speech says "Send the Mailzzy campaign." in two voices; the real Parakeet v2 writes what
it heard (a product name it gets wrong); two kept recordings are saved with that text; the real Checker
lets the real Whisper Large v3 Turbo listen again on the graphics card (a stub stands in for Claude and
agrees with Whisper); and the fix must then be active and make the app's clean-up spell "Mailzzy".

Skipped when Parakeet already spells the word right for the voices tried, when there is no discrete
graphics card, or when the Whisper file is not on this PC. Point PD_TEACHER_GGUF at the .gguf to use a
copy anywhere; otherwise the app's own download or Orca's copy is used (teacher.find).
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from parakeet_dictate import autolearn, checker, config, models, recordings, teacher
from parakeet_dictate.audio import preprocess
from parakeet_dictate.engine import Engine, EngineSpec
from parakeet_dictate.textclean import Cleaner

from test_engine_slow import _model, _tts

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parent.parent
TEACHER_ENV = "PD_TEACHER_GGUF"
SAID = "Send the Mailzzy campaign."
TERM = "Mailzzy"
# (voice, shell). The Indian-English voices only exist in PowerShell 7. Parakeet v2 heard the name as
# "MailT", "Mails Z" or "Mails D" with these; a voice it spells right is skipped, a missing one too.
VOICES = (("Microsoft Zira Desktop", "powershell"), ("Microsoft David Desktop", "powershell"),
          ("Microsoft Heera", "pwsh"), ("Microsoft Ravi", "pwsh"), ("Microsoft Mark", "pwsh"))


def _teacher_file(tmp_path: Path) -> Path:
    env = os.environ.get(TEACHER_ENV)
    if env:
        if not Path(env).is_file():
            pytest.skip(f"{TEACHER_ENV} is set but {env} is not a file")
        return Path(env)
    paths = config.Paths.default()
    orca = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "orca" / "speech-models"
    found = teacher.find(paths.teacher_dir, orca, learning_dir=tmp_path / "verified")   # nothing written to the app's folders
    if found is None:
        pytest.skip(f"Whisper (whisper-large-v3-turbo-Q8_0.gguf) is not on this PC: set {TEACHER_ENV} to the file")
    return found


def _heard_by_parakeet(cfg, cleaner: Cleaner, tmp_path: Path):
    """[(audio, raw text)] for the first two voices whose Parakeet text does not already come out right."""
    spec = EngineSpec.for_model(_model(), cfg.threads, device="auto", vocabulary=cfg.hotwords)
    engine = Engine(spec, unload_after=0)
    got = []
    try:
        for voice, shell in VOICES:
            try:
                audio = _tts(SAID, tmp_path / "said.wav", voice, shell)
            except (subprocess.CalledProcessError, OSError):
                print(f"voice {voice}: not available")
                continue
            raw = engine.transcribe(preprocess(audio))[0]
            clean = cleaner.clean(raw)
            print(f"voice {voice}: Parakeet raw {raw!r} -> app text {clean!r}")
            if TERM.lower() in clean.lower():
                continue   # the default dictionary (or Parakeet) already gets it right: nothing to learn
            got.append((audio, raw))
            if len(got) == 2:
                break
    finally:
        engine.unload()   # off the graphics card before Whisper loads
    return got


def test_mishearing_learned_from_two_recordings(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])))
    if not teacher.has_discrete_gpu([sys.executable, "-m", "parakeet_dictate"]):
        pytest.skip("no discrete graphics card: the teacher only runs on one")
    whisper = _teacher_file(tmp_path)

    cfg = config.parse("")   # the default dictionary, not the user's own
    model = _model()
    langs = models.dictated_languages(model.languages, cfg.language)
    plain = Cleaner.from_config(cfg, languages=langs)   # today's fixes: none learned yet
    heard = _heard_by_parakeet(cfg, plain, tmp_path)
    if len(heard) < 2:
        pytest.skip(f"Parakeet already spells {TERM} right in all but {len(heard)} of the voices tried")

    rec_dir, learning_dir, auto_file = tmp_path / "recordings", tmp_path / "learning", tmp_path / "learned_auto.toml"
    for n, (audio, raw) in enumerate(heard):
        when = datetime(2026, 11, 12, 9, 5 * n, 0)   # two different recordings
        recordings.save(rec_dir, audio, recordings.Record(
            id=recordings.new_id(when), time=when.isoformat(timespec="seconds"), exe="", raw=raw, phrases=[raw],
            typed=plain.clean(raw), model=model.name, language="en"))
    assert len(recordings.list_records(rec_dir)) == 2

    asked = []

    def judge_agrees_with_whisper(pairs, terms):   # stands in for `claude -p`: text in, the right text out
        asked.extend(pairs)
        return {p.id: p.whisper for p in pairs}

    store = autolearn.Store(learning_dir, auto_file)
    run = checker.Checker(
        store, rec_dir, lambda spec: Engine(spec, -1), lambda exe: plain,
        lambda: list(dict.fromkeys(term for term, _ in cfg.dictionary)),   # what the app tells Whisper
        lambda: list(cfg.dictionary), cfg, str(whisper), ask=judge_agrees_with_whisper)
    result = run.run(lambda: False)

    rows = [json.loads(line) for line in (learning_dir / "checked.jsonl").read_text("utf-8").splitlines()]
    fixes = store.active()
    for row in rows:
        print(f"checked {row['id']}: Parakeet {row['parakeet']!r} | Whisper {row['whisper']!r} | judged {row['judged']!r}"
              f" | matched={row['matched']} skipped={row['skipped']}")
    print(f"checker: {result!r}; Claude was asked about {len(asked)} pair(s)")
    print("active fixes: " + "; ".join(f"{f.heard_as!r} -> {f.term!r} (seen {f.seen}x)" for f in fixes))
    print(f"learned_auto.toml:\n{auto_file.read_text('utf-8') if auto_file.exists() else '(missing)'}")

    assert not result.startswith("error:"), result
    assert len(asked) == 2, f"the stub judge should have been asked about both recordings: {asked}"
    assert [f.term for f in fixes] == [TERM], f"expected one fix for {TERM}: {fixes}"

    # The clean-up built the way the app builds it: [dictionary], then learned.toml (none here), then the
    # automatic fixes read back from learned_auto.toml.
    app_cleaner = Cleaner.from_config(cfg, languages=langs, learned=[] + autolearn.load_active(auto_file))
    for _, raw in heard:
        text = app_cleaner.clean(raw)
        print(f"Parakeet raw {raw!r} -> app text {text!r}")
        assert TERM in text
