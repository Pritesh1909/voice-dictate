"""End to end with the real model: Windows text-to-speech -> preprocess -> Parakeet v2 (GGUF) -> cleanup."""
import subprocess
import time
import wave

import numpy as np
import pytest

from parakeet_dictate import config
from parakeet_dictate.audio import preprocess
from parakeet_dictate.engine import Engine, EngineSpec
from parakeet_dictate.textclean import Cleaner

pytestmark = pytest.mark.slow

SENTENCES = {
    "fillers": "Uh, I think we should, um, deploy the core service first. Ahh, the Keycloak token must be validated.",
    "names": "Please open the Inkfree e-signature flow on CodeCommit.",
    "more_names": "Sign in with Keycloak, then send the Mailzzy campaign from the SoftSages account.",
}


def _tts(text: str, path, voice: str | None = None, shell: str = "powershell") -> np.ndarray:
    """Windows text-to-speech into `path`, read back as float32 at 16 kHz. `voice` names an installed voice
    ("Microsoft Zira"); the Indian-English ones ("Microsoft Ravi", "Microsoft Heera") only show up in
    PowerShell 7, so pass shell="pwsh" for them."""
    ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          "if ($env:PD_VOICE) { $s.SelectVoice($env:PD_VOICE) };"
          "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
          "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono);"
          f"$s.SetOutputToWaveFile('{path}',$f); $s.Speak($env:PD_TEXT); $s.Dispose()")
    import os
    subprocess.run([shell, "-NoProfile", "-Command", ps], check=True,
                   env=dict(os.environ, PD_TEXT=text, PD_VOICE=voice or ""))
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def _model():
    from parakeet_dictate import models
    found = models.find(include_wsl=False)
    found = [m for m in found if m.languages == ("en",)]
    if not found:
        pytest.skip("English Parakeet model not on this PC")
    return found[0]


@pytest.fixture(scope="module", params=["auto", "cpu"])
def engine(request):
    c = config.parse("")
    e = Engine(EngineSpec.for_model(_model(), c.threads, device=request.param, vocabulary=c.hotwords),
               unload_after=0)
    yield e
    e.unload()
    assert e.state == "unloaded"


@pytest.fixture(scope="module")
def cleaner():
    c = config.parse("")
    return Cleaner.from_config(c)


def test_fillers_removed_end_to_end(engine, cleaner, tmp_path):
    raw, secs = engine.transcribe(preprocess(_tts(SENTENCES["fillers"], tmp_path / "a.wav")))
    text = cleaner.clean(raw)
    print(f"\nraw:   {raw}\nclean: {text}\n({secs:.2f}s, {engine.note})")
    assert engine.note.startswith("on ")
    low = f" {text.lower()} "
    for f in (" uh", " um", " ahh", " ah,"):
        assert f not in low
    assert "deploy the core service first" in text
    assert "Keycloak" in text


def test_product_names_fixed(engine, cleaner, tmp_path):
    raw, _ = engine.transcribe(preprocess(_tts(SENTENCES["names"], tmp_path / "b.wav")))
    text = cleaner.clean(raw)
    print(f"\nraw:   {raw}\nclean: {text}")
    assert "Inkfree e-signature flow" in text and "CodeCommit" in text
    raw, _ = engine.transcribe(preprocess(_tts(SENTENCES["more_names"], tmp_path / "c.wav")))
    text = cleaner.clean(raw)
    print(f"raw:   {raw}\nclean: {text}")
    for name in ("Keycloak", "Mailzzy", "SoftSages"):
        assert name in text


def test_memory_freed_after_idle_period(tmp_path):
    """done() starts the countdown; a new dictation (warm) before it expires keeps the model."""
    c = config.parse("")
    e = Engine(EngineSpec.for_model(_model(), c.threads, device=c.device), unload_after=2)
    try:
        audio = preprocess(_tts("Testing memory release.", tmp_path / "m.wav"))
        e.transcribe(audio)
        proc = e._proc
        e.done()
        time.sleep(1.0)
        e.warm()                       # next dictation started within the window -> kept
        assert e.state == "ready" and e._proc is proc
        e.transcribe(audio)
        e.done()
        time.sleep(3.5)
        assert e.state == "unloaded"
        assert not proc.is_alive()     # the process is gone, so its memory is back with Windows
    finally:
        e.unload()


def test_split_at_pause_matches_one_shot(engine, cleaner, tmp_path):
    """Transcribing while speaking (cut at the pause) must give the same text as one shot."""
    from parakeet_dictate.segmenter import Segmenter
    a = _tts("Uh, I think we should, um, deploy the core service first.", tmp_path / "p1.wav")
    b = _tts("Then check the Keycloak token on CodeCommit.", tmp_path / "p2.wav")
    pause = np.random.default_rng(0).normal(0, 0.0005, int(1.2 * 16000)).astype(np.float32)
    x = np.concatenate([a, pause, b])

    seg = Segmenter(16000, pause_seconds=0.8)
    parts = []
    for i in range(0, x.size, 800):
        parts += seg.feed(x[i:i + 800])
    parts.append(seg.finish())
    assert len(parts) >= 2          # cut between the sentences (a trailing-silence part may follow)
    raws = [engine.transcribe(preprocess(p))[0] for p in parts]
    split_text = cleaner.clean(" ".join(r.strip() for r in raws if r.strip()))
    one_shot = cleaner.clean(engine.transcribe(preprocess(x))[0])
    print(f"\nsplit:    {split_text}\none shot: {one_shot}")
    assert split_text == one_shot
    assert split_text == ("I think we should deploy the core service first. "
                          "Then check the Keycloak token on CodeCommit.")
