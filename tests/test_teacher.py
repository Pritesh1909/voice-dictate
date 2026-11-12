"""Getting the Whisper teacher model: resumable download, reusing Orca's copy, finding a discrete GPU.
Nothing here touches the internet: a local HTTP server serves a small fake model file."""
import hashlib
import http.server
import json
import os
import random
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest
from test_models import write_gguf

from parakeet_dictate import __main__ as entry
from parakeet_dictate import models, teacher

NAME = "whisper-large-v3-turbo-Q8_0.gguf"
WHISPER = {"general.architecture": "whisper", "general.name": "Whisper Large v3 Turbo",
           "general.languages": ["en"], "stt.variant": "large-v3-turbo"}


def make_body(tmp_path: Path, mb: int = 3, seed: int = 1) -> bytes:
    """A valid GGUF header followed by `mb` MB of seeded random bytes (zeros would hide offset mistakes)."""
    head = write_gguf(tmp_path / f"head{seed}.gguf", WHISPER).read_bytes()
    return head + random.Random(seed).randbytes(mb << 20)


@pytest.fixture
def body(tmp_path):
    return make_body(tmp_path)


@pytest.fixture
def known(body, monkeypatch):
    """The Whisper catalog entry, pointed at the fake file."""
    monkeypatch.setitem(models.KNOWN, teacher.KEY, ("repo/x", "rev", NAME, len(body), hashlib.sha256(body).hexdigest()))
    return NAME, len(body), hashlib.sha256(body).hexdigest()


class Serve:
    """HTTP server for one fake file. honour_range=False answers a Range request with the whole file (200)."""

    def __init__(self, body: bytes, honour_range: bool = True):
        self.body, self.honour_range = body, honour_range
        self.ranges: list[str | None] = []
        self.sent = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path.startswith(f"/repo/x/resolve/rev/{NAME}")
                rng = self.headers.get("Range")
                outer.ranges.append(rng)
                data, status, extra = outer.body, 200, {}
                if rng and outer.honour_range:
                    start = int(rng.split("=")[1].rstrip("-"))
                    data, status = outer.body[start:], 206
                    extra["Content-Range"] = f"bytes {start}-{len(outer.body) - 1}/{len(outer.body)}"
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
                outer.sent += len(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, args=(0.02,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def serve(body):
    made: list[Serve] = []

    def start(honour_range: bool = True) -> Serve:
        made.append(Serve(body, honour_range))
        return made[-1]

    yield start
    for s in made:
        s.close()


# --- resumable download ---------------------------------------------------------------------------

def test_download_resumes_from_part(tmp_path, body, known, serve):
    s = serve()
    half = len(body) // 2
    part = tmp_path / "dl" / teacher.KEY / (NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(body[:half])
    seen = []
    m = models.download(tmp_path / "dl", lambda d, t: seen.append((d, t)), base_url=s.url, key=teacher.KEY, resume=True)
    final = tmp_path / "dl" / teacher.KEY / NAME
    assert m.path == final and hashlib.sha256(final.read_bytes()).hexdigest() == known[2]
    assert s.ranges == [f"bytes={half}-"]                 # asked only for the rest
    assert s.sent == len(body) - half                     # and only that went over the wire
    assert seen[0][0] > half and seen[-1] == (len(body), len(body))   # progress counts the bytes already there
    assert not part.exists()


def test_resume_falls_back_when_server_ignores_range(tmp_path, body, known, serve):
    s = serve(honour_range=False)
    part = tmp_path / "dl" / teacher.KEY / (NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"\xff" * (len(body) // 2))          # bytes that are NOT the start of the file
    m = models.download(tmp_path / "dl", base_url=s.url, key=teacher.KEY, resume=True)
    assert s.ranges and s.ranges[0].startswith("bytes=")  # it did ask
    assert s.sent == len(body)                            # the server answered 200 with everything
    assert hashlib.sha256(m.path.read_bytes()).hexdigest() == known[2]


def test_download_without_resume_starts_over(tmp_path, body, known, serve):
    s = serve()
    part = tmp_path / "dl" / teacher.KEY / (NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(body[:1000])
    models.download(tmp_path / "dl", base_url=s.url, key=teacher.KEY)
    assert s.ranges == [None] and s.sent == len(body)


def test_resume_ignores_a_part_that_is_already_too_big(tmp_path, body, known, serve):
    s = serve()
    part = tmp_path / "dl" / teacher.KEY / (NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(body + b"junk")                      # cannot be a prefix of the real file
    m = models.download(tmp_path / "dl", base_url=s.url, key=teacher.KEY, resume=True)
    assert s.ranges == [None]
    assert hashlib.sha256(m.path.read_bytes()).hexdigest() == known[2]


def test_resumed_download_with_a_wrong_prefix_is_rejected(tmp_path, body, known, serve):
    s = serve()
    part = tmp_path / "dl" / teacher.KEY / (NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"\xff" * 1000)                      # wrong bytes, server resumes after them
    with pytest.raises(OSError, match="checksum"):
        models.download(tmp_path / "dl", base_url=s.url, key=teacher.KEY, resume=True)
    assert not part.exists() and not (tmp_path / "dl" / teacher.KEY / NAME).exists()   # next try starts clean


# --- finding the file ---------------------------------------------------------------------------

def orca_copy(tmp_path: Path, content: bytes) -> Path:
    p = tmp_path / "orca" / teacher.KEY / NAME
    p.parent.mkdir(parents=True)
    p.write_bytes(content)
    return p


def test_find_prefers_own_then_orca_with_matching_sha(tmp_path, body, known, monkeypatch):
    teacher_dir, orca_dir, learning = tmp_path / "teacher", tmp_path / "orca", tmp_path / "learning"
    assert teacher.find(teacher_dir, orca_dir, learning_dir=learning) is None
    theirs = orca_copy(tmp_path, body)
    hashed = []
    real = models._sha256
    monkeypatch.setattr(models, "_sha256", lambda p: hashed.append(p) or real(p))

    assert teacher.find(teacher_dir, orca_dir, learning_dir=learning) == theirs   # verified once ...
    assert len(hashed) == 1
    assert str(theirs) in json.loads((learning / "teacher_verified.json").read_text())
    assert teacher.find(teacher_dir, orca_dir, learning_dir=learning) == theirs   # ... and remembered
    assert len(hashed) == 1

    own = teacher_dir / teacher.KEY / NAME
    own.parent.mkdir(parents=True)
    own.write_bytes(body)
    assert teacher.find(teacher_dir, orca_dir, learning_dir=learning) == own      # our own copy wins
    assert len(hashed) == 1


def test_find_looks_for_the_cache_next_to_the_teacher_folder_by_default(tmp_path, body, known):
    orca_copy(tmp_path, body)
    teacher.find(tmp_path / "data" / "teacher", tmp_path / "orca")
    assert (tmp_path / "data" / "learning" / "teacher_verified.json").is_file()


def test_find_rejects_orca_file_with_wrong_size(tmp_path, body, known):
    orca_copy(tmp_path, body[:-1])
    assert teacher.find(tmp_path / "teacher", tmp_path / "orca", learning_dir=tmp_path / "learning") is None
    assert not (tmp_path / "learning" / "teacher_verified.json").exists()


def test_find_rejects_orca_file_with_wrong_sha(tmp_path, body, known):
    orca_copy(tmp_path, bytes([body[0] ^ 1]) + body[1:])          # same size, one bit off
    assert teacher.find(tmp_path / "teacher", tmp_path / "orca", learning_dir=tmp_path / "learning") is None
    assert not (tmp_path / "learning" / "teacher_verified.json").exists()


def test_find_checks_again_when_orca_replaces_its_file(tmp_path, body, known):
    learning = tmp_path / "learning"
    theirs = orca_copy(tmp_path, body)
    assert teacher.find(tmp_path / "teacher", tmp_path / "orca", learning_dir=learning) == theirs
    st = theirs.stat()
    theirs.write_bytes(bytes([body[0] ^ 1]) + body[1:])           # same size, different bytes ...
    os.utime(theirs, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # ... and a clearly newer file time
    assert teacher.find(tmp_path / "teacher", tmp_path / "orca", learning_dir=learning) is None


@pytest.mark.parametrize("junk", ["", "{not json", "[1, 2]", '{"x": 5}'])
def test_find_survives_a_damaged_cache_file(tmp_path, body, known, junk):
    learning = tmp_path / "learning"
    learning.mkdir()
    (learning / "teacher_verified.json").write_text(junk)
    theirs = orca_copy(tmp_path, body)
    assert teacher.find(tmp_path / "teacher", tmp_path / "orca", learning_dir=learning) == theirs
    assert str(theirs) in json.loads((learning / "teacher_verified.json").read_text())


def test_find_ignores_an_own_file_of_the_wrong_size(tmp_path, body, known):
    own = tmp_path / "teacher" / teacher.KEY / NAME
    own.parent.mkdir(parents=True)
    own.write_bytes(body[:100])
    assert teacher.find(tmp_path / "teacher", tmp_path / "orca", learning_dir=tmp_path / "learning") is None


def test_ensure_downloads_once_then_reuses(tmp_path, body, known, serve):
    s = serve()
    teacher_dir, orca_dir = tmp_path / "teacher", tmp_path / "orca"
    seen = []
    p = teacher.ensure(teacher_dir, orca_dir, lambda d, t: seen.append((d, t)), learning_dir=tmp_path / "learning",
                       base_url=s.url)
    assert p == teacher_dir / teacher.KEY / NAME and p.read_bytes() == body
    assert seen[-1] == (len(body), len(body)) and len(s.ranges) == 1
    assert teacher.ensure(teacher_dir, orca_dir, learning_dir=tmp_path / "learning", base_url=s.url) == p
    assert len(s.ranges) == 1                                       # no second request


def test_ensure_uses_orca_copy_without_downloading(tmp_path, body, known, serve):
    s = serve()
    theirs = orca_copy(tmp_path, body)
    p = teacher.ensure(tmp_path / "teacher", tmp_path / "orca", learning_dir=tmp_path / "learning", base_url=s.url)
    assert p == theirs and s.ranges == []


def test_ensure_resumes_an_interrupted_download(tmp_path, body, known, serve):
    s = serve()
    half = len(body) // 3
    part = tmp_path / "teacher" / teacher.KEY / (NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(body[:half])
    p = teacher.ensure(tmp_path / "teacher", tmp_path / "orca", learning_dir=tmp_path / "learning", base_url=s.url)
    assert p.read_bytes() == body and s.ranges == [f"bytes={half}-"]


def test_catalog_entry_matches_the_whisper_build():
    repo, rev, name, size, sha = models.KNOWN["whisper-large-v3-turbo"]
    assert (repo, rev, name, size, sha) == (
        "handy-computer/whisper-large-v3-turbo-gguf", "5eaf945c7978e564bae5b28a5b1639dd93c2bfb1",
        "whisper-large-v3-turbo-Q8_0.gguf", 886381760,
        "b2e30cc286bc9f3aba4db9099fc7403543497c05ce7100d0d83091ddfd25a183")
    assert teacher.KEY == "whisper-large-v3-turbo"


# --- graphics card check ----------------------------------------------------------------------------

def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_has_discrete_gpu_parses_output():
    assert teacher.has_discrete_gpu(py("print('[[\"gpu\",\"Vulkan1\"]]')")) is True
    assert teacher.has_discrete_gpu(py("print('[[\"igpu\",\"Vulkan0\"],[\"cpu\",\"CPU\"],[\"gpu\",\"Vulkan1\"]]')")) is True
    assert teacher.has_discrete_gpu(py("print('[[\"igpu\",\"Vulkan0\"],[\"cpu\",\"CPU\"]]')")) is False
    assert teacher.has_discrete_gpu(py("print('[]')")) is False


def test_has_discrete_gpu_passes_the_flag_to_the_command():
    code = "import sys, json; print(json.dumps([['gpu', 'x']] if sys.argv[1:] == ['--list-devices'] else []))"
    assert teacher.has_discrete_gpu(py(code)) is True


def test_has_discrete_gpu_is_false_on_a_timeout():
    assert teacher.has_discrete_gpu(py("import time; time.sleep(30)"), timeout=0.5) is False


@pytest.mark.parametrize("code", ["print('not json')", "", "import sys; sys.exit(3)",
                                  "print('{\"gpu\": 1}')", "print('[null]')",
                                  "import sys; print('[[\"gpu\",\"x\"]]'); sys.exit(1)"])
def test_has_discrete_gpu_is_false_when_the_output_is_unusable(code):
    assert teacher.has_discrete_gpu(py(code)) is False


def test_has_discrete_gpu_is_false_when_the_program_is_missing(tmp_path):
    assert teacher.has_discrete_gpu([str(tmp_path / "nope.exe")]) is False


def test_has_discrete_gpu_hides_the_console_window(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='[["gpu","a"]]', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert teacher.has_discrete_gpu(["app.exe", "--x"]) is True
    assert seen["cmd"] == ["app.exe", "--x", "--list-devices"] and seen["timeout"] == 30
    assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_teacher_module_does_not_load_the_engine():
    code = ("import sys; from parakeet_dictate import teacher; "
            "sys.exit(1 if 'transcribe_cpp' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 0


# --- --list-devices ----------------------------------------------------------------------------------

def test_list_devices_flag_prints_device_types_and_names(monkeypatch, capsys):
    dev = lambda t, n: types.SimpleNamespace(device_type=t, name=n, description="ignored")   # noqa: E731
    fake = types.SimpleNamespace(set_log_callback=lambda cb: None,
                                 backends=lambda: [dev("igpu", "Vulkan0"), dev("gpu", "Vulkan1"), dev("cpu", "CPU")])
    monkeypatch.setitem(sys.modules, "transcribe_cpp", fake)
    monkeypatch.setattr(sys, "argv", ["ParakeetDictate", "--list-devices"])
    assert entry.main() == 0
    assert json.loads(capsys.readouterr().out) == [["igpu", "Vulkan0"], ["gpu", "Vulkan1"], ["cpu", "CPU"]]
