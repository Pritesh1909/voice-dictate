"""Model discovery on Windows folders, WSL shares and Linux homes, using small fake GGUF files."""
import hashlib
import http.server
import struct
import threading
from pathlib import Path

import pytest

from parakeet_dictate import models


def _s(x: str) -> bytes:
    b = x.encode()
    return struct.pack("<Q", len(b)) + b


def _kv(k: str, v) -> bytes:
    if isinstance(v, bool):
        return _s(k) + struct.pack("<I?", 7, v)
    if isinstance(v, int):
        return _s(k) + struct.pack("<II", 4, v)
    if isinstance(v, str):
        return _s(k) + struct.pack("<I", 8) + _s(v)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return _s(k) + struct.pack("<IIQ", 9, 8, len(v)) + b"".join(_s(x) for x in v)
    if isinstance(v, list):
        return _s(k) + struct.pack("<IIQ", 9, 6, len(v)) + struct.pack(f"<{len(v)}f", *v)
    raise TypeError(v)


def write_gguf(p: Path, meta: dict, pad: int = 0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(_kv(k, v) for k, v in meta.items())
    p.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(meta)) + body + b"\0" * pad)
    return p


def parakeet(name="Parakeet TDT 0.6B v2", langs=("en",), variant="tdt-0.6b-v2", **extra) -> dict:
    return {"general.architecture": "parakeet", "general.name": name, "general.languages": list(langs),
            "stt.variant": variant, "stt.parakeet.encoder.n_layers": 24,
            "tokenizer.ggml.tokens": [f"t{i}" for i in range(50)], "tokenizer.ggml.scores": [0.0] * 50, **extra}


def fake_model(d: Path, name="Parakeet TDT 0.6B v2", file="parakeet-tdt-0.6b-v2-Q8_0.gguf", **kw) -> Path:
    return write_gguf(d / file, parakeet(name=name, **kw))


def env_for(home: Path) -> dict:
    return {"APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "USERPROFILE": str(home)}


ORCA = Path("AppData") / "Roaming" / "orca" / "speech-models"


def test_reads_header_and_skips_non_speech_models(tmp_path):
    m = models.inspect(fake_model(tmp_path / "v2"))
    assert m and m.name == "Parakeet TDT 0.6B v2" and m.arch == "parakeet" and m.languages == ("en",)
    assert m.languages_label() == "English" and not m.exact
    v3 = models.inspect(fake_model(tmp_path / "v3", name="Parakeet TDT 0.6B v3", langs=("de", "en", "fr")))
    assert v3 and v3.languages_label() == "3 languages"
    # a future model family works the same way: it only has to be a transcribe.cpp speech model
    w = write_gguf(tmp_path / "w" / "whisper-tiny-Q8_0.gguf",
                   {"general.architecture": "whisper", "general.name": "Whisper Tiny", "stt.variant": "tiny"})
    assert models.inspect(w).name == "Whisper Tiny"
    vad = write_gguf(tmp_path / "vad" / "silero-vad-v6.2-F32.gguf",
                     {"general.architecture": "silero_vad", "general.name": "Silero VAD v6.2",
                      "general.tags": ["voice-activity-detection"], "stt.variant": "silero-vad-v6.2",
                      "stt.vad.frame_samples": 512})
    llm = write_gguf(tmp_path / "llm" / "llama-3-8b.gguf", {"general.architecture": "llama", "general.name": "Llama"})
    diar = write_gguf(tmp_path / "d" / "sortformer.gguf",
                      {"general.architecture": "sortformer", "general.name": "Sortformer", "stt.variant": "x"})
    assert models.inspect(vad) is None and models.inspect(llm) is None and models.inspect(diar) is None


def test_rejects_non_gguf_and_truncated_files(tmp_path):
    (tmp_path / "a.gguf").write_bytes(b"onnx")
    good = fake_model(tmp_path / "t").read_bytes()
    (tmp_path / "b.gguf").write_bytes(good[:40])
    old = tmp_path / "old"
    old.mkdir()
    (old / "encoder.int8.onnx").write_bytes(b"x")
    assert models.inspect(tmp_path / "a.gguf") is None
    assert models.inspect(tmp_path / "b.gguf") is None
    assert models.inspect_path(old) is None


def test_exact_matches_orcas_build_by_size(tmp_path):
    _, _, name, size, _ = models.KNOWN["parakeet-tdt-0.6b-v2-int8"]
    meta = parakeet()
    head = len(write_gguf(tmp_path / "probe" / name, meta).read_bytes())
    m = models.inspect(write_gguf(tmp_path / "x" / name, meta, pad=size - head))
    assert m.exact


def test_finds_orca_v2_and_v3_and_prefers_english_v2(tmp_path):
    home = tmp_path / "user"
    fake_model(home / ORCA / "parakeet-tdt-0.6b-v3-int8", name="Parakeet TDT 0.6B v3",
               file="parakeet-tdt-0.6b-v3-Q8_0.gguf", langs=("bg", "en", "fr", "de"))
    fake_model(home / ORCA / "parakeet-tdt-0.6b-v2-int8")
    write_gguf(home / ORCA / "silero-vad-v6.2" / "silero-vad-v6.2-F32.gguf",
               {"general.architecture": "silero_vad", "general.name": "Silero VAD", "stt.vad.frame_samples": 512})
    found = models.find(include_wsl=False, env=env_for(home))
    assert [m.name for m in found] == ["Parakeet TDT 0.6B v2", "Parakeet TDT 0.6B v3"]
    assert {m.source for m in found} == {"Orca"}


def test_inspect_path_takes_a_folder_or_a_file(tmp_path):
    f = fake_model(tmp_path / "m")
    assert models.inspect_path(tmp_path / "m").path == f
    assert models.inspect_path(f).path == f


def test_finds_other_apps_and_downloads(tmp_path):
    home = tmp_path / "user"
    fake_model(home / "AppData" / "Roaming" / "SomeApp" / "models" / "parakeet", name="A")
    fake_model(home / "AppData" / "Local" / "OtherApp" / "models", name="B")
    fake_model(home / "Downloads" / "stt", name="C")
    srcs = sorted(m.source for m in models.find(include_wsl=False, env=env_for(home)))
    assert srcs == ["OtherApp (Windows)", "SomeApp (Windows)", "your user folder"]


def test_same_model_in_two_places_listed_once(tmp_path):
    home = tmp_path / "user"
    fake_model(home / ORCA / "parakeet-tdt-0.6b-v2-int8")
    fake_model(home / "Downloads" / "copy")
    found = models.find(include_wsl=False, env=env_for(home))
    assert len(found) == 1


def test_finds_models_inside_wsl_distros(tmp_path):
    share = tmp_path / "wsl"
    fake_model(share / "Ubuntu" / "home" / "alice" / ".config" / "orca" / "speech-models" / "p", name="A")
    fake_model(share / "Debian" / "root" / "models" / "parakeet", name="B")
    fake_model(share / "Debian" / "home" / "bob" / ".cache" / "parakeet", name="C")
    found = models.find(include_wsl=True, distros=["Ubuntu", "Debian"], share_root=str(share),
                        env=env_for(tmp_path / "nobody"))
    assert {m.source for m in found} == {"Orca (WSL Ubuntu)", "WSL Debian"}
    assert len(found) == 3 and all(m.in_wsl for m in found)


def test_windows_copy_preferred_over_wsl(tmp_path):
    home, share = tmp_path / "user", tmp_path / "wsl"
    fake_model(share / "Ubuntu" / "home" / "a" / ".config" / "orca" / "speech-models" / "p", name="Parakeet W")
    fake_model(home / ORCA / "p")
    found = models.find(include_wsl=True, distros=["Ubuntu"], share_root=str(share), env=env_for(home))
    assert not found[0].in_wsl and found[1].in_wsl


def test_linux_home_places(tmp_path):
    fake_model(tmp_path / ".config" / "orca" / "speech-models" / "p", name="A")
    fake_model(tmp_path / ".local" / "share" / "com.example" / "models" / "p", name="B")
    hits = [models.inspect(f, s) for p, s, d in models.linux_home_places(tmp_path, "Linux")
            for f in models._gguf_files(p, d)]
    assert {m.source for m in hits if m} == {"Orca (Linux)", "com.example (Linux)"}


def test_import_copy_from_wsl(tmp_path):
    src = models.inspect(fake_model(tmp_path / "wsl" / "parakeet-tdt-0.6b-v2-int8"), "WSL Ubuntu", in_wsl=True)
    seen = []
    m = models.import_copy(src, tmp_path / "local", lambda d, t: seen.append((d, t)))
    assert m.path == tmp_path / "local" / "parakeet-tdt-0.6b-v2-int8" / src.path.name and not m.in_wsl
    assert m.name == src.name
    assert seen and seen[-1][0] == seen[-1][1]


def test_download_verifies_checksum(tmp_path, monkeypatch):
    body = fake_model(tmp_path / "src").read_bytes()
    name = "parakeet-tdt-0.6b-v2-Q8_0.gguf"
    monkeypatch.setitem(models.KNOWN, "test", ("repo/x", "rev", name, len(body), hashlib.sha256(body).hexdigest()))
    served = {"body": body}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path.startswith(f"/repo/x/resolve/rev/{name}")
            b = served["body"]
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        m = models.download(tmp_path / "dl", base_url=base, key="test")
        assert m.source == "Parakeet Dictate (downloaded)" and m.name == "Parakeet TDT 0.6B v2"
        served["body"] = body[:-3] + b"XXX"            # tampered file -> rejected
        with pytest.raises(OSError, match="checksum"):
            models.download(tmp_path / "dl2", base_url=base, key="test")
        assert not (tmp_path / "dl2" / "test" / name).exists()
    finally:
        srv.shutdown()


def test_real_orca_models_detected_on_this_pc():
    found = models.find(include_wsl=False)
    if not found:
        pytest.skip("no speech model on this PC")
    assert found[0].exact and found[0].languages == ("en",)


def silero(version="v6.2") -> dict:
    return {"general.architecture": "silero_vad", "general.name": f"Silero VAD {version}",
            "general.version": version, "general.tags": ["voice-activity-detection"], "stt.variant": "silero-vad",
            "stt.vad.frame_samples": 512}


def test_finds_the_voice_detector_next_to_orca_models(tmp_path):
    fake_model(tmp_path / ORCA / "parakeet-tdt-0.6b-v2-int8")
    vad = write_gguf(tmp_path / ORCA / "silero-vad-v6.2" / "silero-vad-v6.2-F32.gguf", silero())
    env = env_for(tmp_path)
    assert models.find_vad("auto", env=env) == vad
    assert [m.arch for m in models.find(include_wsl=False, env=env)] == ["parakeet"]   # not offered as a model
    assert models.find_vad("off", env=env) is None
    assert models.find_vad(str(vad.parent)) == vad


def test_no_voice_detector(tmp_path):
    fake_model(tmp_path / ORCA / "parakeet-tdt-0.6b-v2-int8")
    assert models.find_vad("auto", env=env_for(tmp_path)) is None
    assert models.find_vad(str(tmp_path / "missing.gguf")) is None
