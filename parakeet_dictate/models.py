"""Find speech-to-text models wherever they already are: Windows apps (Orca, Handy, ...), WSL distributions,
or Linux home folders. If there is none, download the exact Parakeet v2 build Orca uses (checksum verified).

Models are single GGUF files run by transcribe.cpp (the engine Orca uses). Nothing is hard-coded per
model: each .gguf file describes itself in its header (architecture, name, languages), so any speech
model Orca or another app downloads later shows up without a code change. Files that are not
speech-to-text (voice-activity detectors, speaker diarizers, language-ID models, LLMs) are skipped.
Models found inside WSL are copied to this PC once, because loading ~700 MB over the \\\\wsl.localhost
share on every dictation would be slow and would keep WSL running.
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# Orca's catalog entries (repo, revision, file, size, SHA-256), copied from Orca 2026-11-12.
KNOWN = {
    "parakeet-tdt-0.6b-v2-int8": ("handy-computer/parakeet-tdt-0.6b-v2-gguf", "07cee0616125a08ef619729bb47f40ef747e4bc4",
                                  "parakeet-tdt-0.6b-v2-Q8_0.gguf", 729574912,
                                  "f0d0e99cebb6d3b83f1f7069b82b5d3c2e39a54545b0da039cb4bafd9c4e5caa"),
    "parakeet-tdt-0.6b-v3-int8": ("handy-computer/parakeet-tdt-0.6b-v3-gguf", "85ac09ea12fc4b1112fa76810059364bc6adc9de",
                                  "parakeet-tdt-0.6b-v3-Q8_0.gguf", 739508576,
                                  "5859f77944efcd8eafa23a6350731960b2b55b2203df51f319665c807d802cc7"),
    # The "teacher": a bigger model that re-listens to dictations in the background (teacher.py). It is never
    # offered as a dictation model; it lives in its own folder, outside the folders searched for models.
    "whisper-large-v3-turbo": ("handy-computer/whisper-large-v3-turbo-gguf", "5eaf945c7978e564bae5b28a5b1639dd93c2bfb1",
                               "whisper-large-v3-turbo-Q8_0.gguf", 886381760,
                               "b2e30cc286bc9f3aba4db9099fc7403543497c05ce7100d0d83091ddfd25a183"),
}
DEFAULT_DOWNLOAD = "parakeet-tdt-0.6b-v2-int8"
HF_URL = "https://huggingface.co"
SKIP_DISTROS = ("docker-desktop", "rancher-desktop", "podman-machine")

# Not speech-to-text, even though transcribe.cpp can load them.
_NON_ASR_WORDS = ("vad", "diariz", "sortformer", "langid", "lang_id", "ecapa", "voxlingua")
_NON_ASR_TAGS = ("voice-activity-detection", "speaker-diarization", "diarization", "language-identification")
_NON_ASR_KEYS = ("stt.vad.", "stt.diarize.", "stt.langid.")


@dataclass(frozen=True)
class Model:
    path: Path                      # the .gguf file
    name: str                       # from the file, e.g. "Parakeet TDT 0.6B v2"
    arch: str                       # e.g. "parakeet", "whisper"
    languages: tuple[str, ...]
    source: str                     # where it was found, for the tray menu
    exact: bool                     # byte-identical size to one of Orca's downloads
    in_wsl: bool = False

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def languages_label(self) -> str:
        if not self.languages:
            return "languages unknown"
        if self.languages == ("en",):
            return "English"
        return f"{len(self.languages)} languages"

    def label(self) -> str:
        return f"{self.name} ({self.languages_label()}) · {self.source}"

    def describe(self) -> str:
        return f"{self.name} — {self.source} ({self.path})"


# --- dictation language -------------------------------------------------------------------------

def match_language(setting: str, supported) -> str | None:
    """The code to tell the engine for the [model] language setting, or None to let it detect the language.
    Like Orca: the model's exact code, else its first regional one ("zh" -> "zh-CN"); None when the setting
    is "auto" or the model doesn't list it (a model that lists no languages can't be told one)."""
    want = (setting or "").strip().lower()
    if want in ("", "auto"):
        return None
    supported = [str(x) for x in supported or ()]
    return next((x for x in supported if x.lower() == want), None) or next(
        (x for x in supported if x.lower().startswith(want + "-")), None)


def dictated_languages(model_languages, setting: str) -> tuple[str, ...]:
    """The languages the dictated text is in, for the filler words; () when unknown. A model with one
    language (or two, as in Chinese-English) uses its own; a multilingual one the setting, if it lists it;
    one whose header lists no languages the setting as it is."""
    langs = tuple(str(x) for x in model_languages or ())
    if 0 < len(langs) <= 2:
        return langs
    want = (setting or "").strip().lower()
    if want in ("", "auto"):
        return ()
    if not langs:
        return (want,)
    got = match_language(want, langs)
    return (got,) if got else ()


# --- reading a GGUF header ------------------------------------------------------------------------

_SCALAR = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
_WANT = {"general.architecture", "general.name", "general.basename", "general.version", "general.languages",
         "general.tags", "stt.variant"}
_MAX_STR = 1 << 20
_MAX_KV = 100_000


class _Bad(Exception):
    pass


def _read(f, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise _Bad("truncated")
    return b


def _str(f) -> str:
    (n,) = struct.unpack("<Q", _read(f, 8))
    if n > _MAX_STR:
        raise _Bad("string too long")
    return _read(f, n).decode("utf-8", "replace")


def _skip_or_read(f, t: int, keep: bool):
    if t == 8:
        if keep:
            return _str(f)
        (n,) = struct.unpack("<Q", _read(f, 8))
        if n > _MAX_STR:
            raise _Bad("string too long")
        f.seek(n, os.SEEK_CUR)
        return None
    if t == 9:
        (et,) = struct.unpack("<I", _read(f, 4))
        (n,) = struct.unpack("<Q", _read(f, 8))
        if et in _SCALAR:
            size = struct.calcsize("<" + _SCALAR[et]) * n
            if not keep:
                f.seek(size, os.SEEK_CUR)
                return None
            return list(struct.unpack(f"<{n}{_SCALAR[et]}", _read(f, size)))
        out = [_skip_or_read(f, et, keep) for _ in range(n)]
        return out if keep else None
    if t in _SCALAR:
        fmt = "<" + _SCALAR[t]
        v = struct.unpack(fmt, _read(f, struct.calcsize(fmt)))[0]
        return v if keep else None
    raise _Bad(f"unknown value type {t}")


def read_header(path: Path) -> dict | None:
    """The few metadata keys we need from a GGUF file (plus every key name), or None if it isn't GGUF.
    Reads only the header; big arrays (the token list) are skipped, not loaded."""
    try:
        with path.open("rb") as f:
            if f.read(4) != b"GGUF":
                return None
            (ver,) = struct.unpack("<I", _read(f, 4))
            if ver < 2:
                return None
            _n_tensors, n_kv = struct.unpack("<QQ", _read(f, 16))
            if n_kv > _MAX_KV:
                return None
            meta: dict = {"_keys": []}
            for _ in range(n_kv):
                k = _str(f)
                (t,) = struct.unpack("<I", _read(f, 4))
                v = _skip_or_read(f, t, k in _WANT)
                meta["_keys"].append(k)
                if k in _WANT:
                    meta[k] = v
            return meta
    except (OSError, _Bad, struct.error, MemoryError):
        return None


def is_speech_to_text(meta: dict) -> bool:
    keys = meta.get("_keys", [])
    if not any(k.startswith("stt.") for k in keys):
        return False  # transcribe.cpp models all carry stt.* keys; LLM GGUFs (llama, qwen, ...) don't
    arch = str(meta.get("general.architecture", "")).lower()
    variant = str(meta.get("stt.variant", "")).lower()
    tags = [str(x).lower() for x in (meta.get("general.tags") or [])]
    if any(w in arch or w in variant for w in _NON_ASR_WORDS):
        return False
    if any(t in _NON_ASR_TAGS for t in tags):
        return False
    return not any(k.startswith(_NON_ASR_KEYS) for k in keys)


def inspect(p: Path, source: str = "", in_wsl: bool = False) -> Model | None:
    """The speech-to-text model in file `p`, else None. Cheap: header only, no hashing."""
    try:
        if not (p.is_file() and p.suffix.lower() == ".gguf"):
            return None
        size = p.stat().st_size
    except OSError:
        return None
    meta = read_header(p)
    if not meta or not is_speech_to_text(meta):
        return None
    arch = str(meta.get("general.architecture") or "unknown")
    name = str(meta.get("general.name") or meta.get("stt.variant") or p.stem)
    langs = tuple(str(x) for x in (meta.get("general.languages") or []))
    exact = any(size == k[3] and p.name == k[2] for k in KNOWN.values())
    return Model(p, name, arch, langs, source or str(p.parent), exact, in_wsl)


def inspect_path(p: Path, source: str = "", in_wsl: bool = False) -> Model | None:
    """A .gguf file, or a folder holding one (the best model in it)."""
    if p.is_dir():
        found = [m for f in _gguf_files(p, 1) if (m := inspect(f, source, in_wsl))]
        return sorted(found, key=_rank)[0] if found else None
    return inspect(p, source, in_wsl)


# --- where to look --------------------------------------------------------------------------------

def _gguf_files(base: Path, depth: int) -> list[Path]:
    """*.gguf in `base` and up to `depth` levels of sub-folders (hidden folders skipped below the top)."""
    out: list[Path] = []
    try:
        with os.scandir(base) as it:
            entries = list(it)
    except OSError:
        return out
    for e in entries:
        try:
            if e.is_file() and e.name.lower().endswith(".gguf"):
                out.append(Path(e.path))
            elif depth > 0 and e.is_dir() and not e.name.startswith((".", "$")):
                out += _gguf_files(Path(e.path), depth - 1)
        except OSError:
            pass
    return out


def _subdirs(base: Path, name: str) -> list[tuple[Path, str]]:
    """base/*/<name> folders, labelled with the app folder's name."""
    out = []
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    if e.is_dir():
                        d = Path(e.path) / name
                        if d.is_dir():
                            out.append((d, e.name))
                except OSError:
                    pass
    except OSError:
        pass
    return out


# (folder, label, depth) to scan for *.gguf
Place = tuple[Path, str, int]


def windows_places(env: dict | None = None) -> list[Place]:
    env = env if env is not None else os.environ
    roaming = Path(env["APPDATA"]) if env.get("APPDATA") else None
    local = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else None
    home = Path(env["USERPROFILE"]) if env.get("USERPROFILE") else None
    c: list[Place] = []
    if local:
        c.append((local / "ParakeetDictate" / "models", "Parakeet Dictate", 2))
    if roaming:
        c.append((roaming / "orca" / "speech-models", "Orca", 2))
        c += [(d, f"{app} (Windows)", 2) for sub in ("speech-models", "models") for d, app in _subdirs(roaming, sub)]
    if local:
        c += [(d, f"{app} (Windows)", 2) for d, app in _subdirs(local, "models")]
    if home:
        c += [(home, "your user folder", 0), (home / "Downloads", "your user folder", 2),
              (home / "models", "your user folder", 2), (home / ".cache", "your user folder", 2)]
    return _unique(c)


def _unique(c: list[Place]) -> list[Place]:
    seen, out = set(), []
    for p, s, d in c:
        k = str(p).lower()  # Windows paths (and \\wsl.localhost) compare case-insensitively
        if k not in seen:
            seen.add(k)
            out.append((p, s, d))
    return out


def _is_wsl_path(p: Path) -> bool:
    return str(p).lower().startswith(("\\\\wsl.localhost\\", "\\\\wsl$\\"))


def user_source(p: Path) -> str:
    """Source label of a model from [model] folders: "your folder (<name>)", the entry's own folder name
    (for a .gguf file entry, the folder it is in)."""
    p = Path(p)
    folder = p.parent if p.suffix.lower() == ".gguf" else p
    return f"your folder ({folder.name or str(folder)})"


def user_places(entries: Iterable[str | Path]) -> list[Place]:
    """[model] folders: .gguf files or folders (searched two folder levels deep, like the app folders)."""
    out: list[Place] = []
    for e in entries:
        p = Path(os.path.expandvars(str(e))).expanduser()
        out.append((p, user_source(p), 2))
    return _unique(out)


def _files_at(p: Path, depth: int) -> list[Path]:
    """A place's *.gguf files: the place itself when it is a .gguf file, else the files in the folder.
    A file or folder that isn't there (any more) just gives nothing."""
    try:
        if p.is_file():
            return [p] if p.suffix.lower() == ".gguf" else []
    except OSError:
        return []
    return _gguf_files(p, depth)


def linux_home_places(home: Path, label: str) -> list[Place]:
    c: list[Place] = [(home / ".config" / n / "speech-models", f"Orca ({label})", 2) for n in ("orca", "Orca")]
    c += [(d, f"{app} ({label})", 2) for d, app in _subdirs(home / ".config", "speech-models")]
    c += [(d, f"{app} ({label})", 2) for d, app in _subdirs(home / ".local" / "share", "models")]
    c += [(home / ".cache", label, 2), (home, label, 0), (home / "Downloads", label, 2), (home / "models", label, 2)]
    return _unique(c)


def wsl_distros() -> list[str]:
    """Installed WSL distributions, read from the registry (does not start WSL)."""
    if sys.platform != "win32":
        return []
    import winreg
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss") as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    with winreg.OpenKey(k, winreg.EnumKey(k, i)) as d:
                        names.append(winreg.QueryValueEx(d, "DistributionName")[0])
                except OSError:
                    pass
    except OSError:
        return []
    return [n for n in names if not n.lower().startswith(SKIP_DISTROS)]


def wsl_places(distros: Iterable[str], share_root: str = r"\\wsl.localhost") -> list[Place]:
    c: list[Place] = []
    for name in distros:
        root = Path(f"{share_root}\\{name}\\")
        homes = []
        try:
            homes += [p for p in (root / "home").iterdir() if p.is_dir()]
        except OSError:
            pass
        homes.append(root / "root")
        for h in homes:
            c += linux_home_places(h, f"WSL {name}")
    return c


def linux_places() -> list[Place]:
    """Running on Linux / inside WSL: own home, plus Windows user folders mounted under /mnt/c."""
    c = linux_home_places(Path.home(), "Linux")
    try:
        for u in Path("/mnt/c/Users").iterdir():
            c += windows_places({"APPDATA": str(u / "AppData" / "Roaming"),
                                 "LOCALAPPDATA": str(u / "AppData" / "Local"), "USERPROFILE": str(u)})
    except OSError:
        pass
    return c


def _rank(m: Model):
    """Best first: on this PC, English-specialist models (Parakeet v2 was measured best for English
    dictation), then fewer languages, then Orca's exact builds, then by name."""
    return (m.in_wsl, "en" not in m.languages, len(m.languages) or 999, not m.exact, m.name.lower(), str(m.path))


def find(include_wsl: bool = True, distros: Iterable[str] | None = None,
         share_root: str = r"\\wsl.localhost", env: dict | None = None,
         extra: Iterable[str | Path] = ()) -> list[Model]:
    """Every speech-to-text model found, best first. The same model found in two places (e.g. Orca's
    folder and a copy) is listed once, keeping the copy on this PC. `extra`: the [model] folders
    (files or folders), searched first; a model there is listed under "your folder" even when an
    identical copy is somewhere else too."""
    places = [(p, s, d, _is_wsl_path(p)) for p, s, d in user_places(extra)]
    n_user = len(places)
    if sys.platform == "win32" or env is not None:
        places += [(p, s, d, False) for p, s, d in windows_places(env)]
        if include_wsl:
            ds = wsl_distros() if distros is None else list(distros)
            places += [(p, s, d, True) for p, s, d in wsl_places(ds, share_root)]
    else:
        places += [(p, s, d, False) for p, s, d in linux_places()]
    seen, found, mine = set(), [], set()
    for i, (base, src, depth, wsl) in enumerate(places):
        for f in _files_at(base, depth):
            key = str(f).lower()
            if key in seen:
                continue
            seen.add(key)
            m = inspect(f, src, wsl)
            if m:
                found.append(m)
                if i < n_user:
                    mine.add(key)
    return _dedupe(found, mine)


def _dedupe(found: list[Model], mine: set[str] = frozenset()) -> list[Model]:
    """Best first, each model once (same name, file name and size = the same model). Of identical
    copies, one from the user's own folders wins, then the best-ranked one."""
    found = sorted(found, key=_rank)
    same, keep = set(), set()
    for m in sorted(found, key=lambda m: (str(m.path).lower() not in mine, m.in_wsl)):
        ident = (m.name, m.path.name.lower(), m.size)
        if ident not in same:
            same.add(ident)
            keep.add(id(m))
    return [m for m in found if id(m) in keep]


def find_in(entries: Iterable[str | Path]) -> list[Model]:
    """Only the models in [model] folders (files or folders), best first."""
    seen, found = set(), []
    for base, src, depth in user_places(entries):
        for f in _files_at(base, depth):
            key = str(f).lower()
            if key not in seen:
                seen.add(key)
                m = inspect(f, src, _is_wsl_path(base))
                if m:
                    found.append(m)
    return _dedupe(found, seen)


def merge(*lists: Iterable[Model]) -> list[Model]:
    """The models of several lists in order, each file once."""
    seen, out = set(), []
    for lst in lists:
        for m in lst:
            key = str(m.path).lower()
            if key not in seen:
                seen.add(key)
                out.append(m)
    return out


def why_not_speech(p: Path) -> str:
    """Why `p` can't be used as the speech model, as the end of a sentence starting with its name."""
    p = Path(p)
    try:
        if not p.is_file():
            return "isn't there"
    except OSError:
        return "can't be read"
    if p.suffix.lower() != ".gguf":
        return "isn't a .gguf model file"
    meta = read_header(p)
    if meta is None:
        return "isn't a GGUF model file (or is damaged)"
    if is_vad(meta):
        return "is a voice detector (VAD), not a speech-to-text model"
    if not any(k.startswith("stt.") for k in meta.get("_keys", [])):
        return "isn't a speech-to-text model (it looks like a language model, LLM)"
    return "isn't a speech-to-text model (a diarization or language-ID model)"


def is_vad(meta: dict) -> bool:
    arch = str(meta.get("general.architecture", "")).lower()
    tags = [str(x).lower() for x in (meta.get("general.tags") or [])]
    return "vad" in arch or "voice-activity-detection" in tags or "stt.vad.frame_samples" in meta.get("_keys", [])


def find_vad(setting: str = "auto", env: dict | None = None) -> Path | None:
    """The voice-activity detector (Silero VAD, ~1 MB, which Orca downloads next to its speech models).
    `setting` is "auto" (search this PC), "off", or the path of a .gguf file or a folder holding one.
    Only Windows folders are searched: a 1 MB file isn't worth waking WSL for."""
    if setting == "off":
        return None
    if setting != "auto":
        p = Path(setting)
        files = _gguf_files(p, 1) if p.is_dir() else [p]
    else:
        if sys.platform != "win32" and env is None:
            return None
        files = [f for base, _, depth in windows_places(env) for f in _gguf_files(base, depth)]
    found = []
    for f in files:
        meta = read_header(f) if f.suffix.lower() == ".gguf" and f.is_file() else None
        if meta and is_vad(meta):
            found.append((str(meta.get("general.version", "")), f))
    return max(found, key=lambda t: t[0])[1] if found else None   # the newest version


# --- getting one onto this PC ---------------------------------------------------------------------

Progress = Callable[[int, int], None]


def _copy(src: Path, dest: Path, progress: Progress) -> None:
    total = src.stat().st_size
    done = 0
    tmp = dest.with_name(dest.name + ".part")
    with src.open("rb") as i, tmp.open("wb") as o:
        while chunk := i.read(4 << 20):
            o.write(chunk)
            done += len(chunk)
            progress(done, total)
    tmp.replace(dest)  # never rewrite a model file in place: it may be memory-mapped


def import_copy(m: Model, dest_root: Path, progress: Progress = lambda done, total: None) -> Model:
    """Copy a model (e.g. from WSL) to dest_root/<folder>/<file> so it loads fast and WSL can stay off."""
    dest = dest_root / m.dir.name / m.path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy(m.path, dest, progress)
    got = inspect(dest, "Parakeet Dictate (copied from " + m.source + ")")
    if not got:
        raise OSError(f"copied model {dest} is incomplete")
    return got


def download(dest_root: Path, progress: Progress = lambda done, total: None,
             base_url: str = HF_URL, key: str = DEFAULT_DOWNLOAD, resume: bool = False) -> Model:
    """Download one of Orca's models (default Parakeet v2, ~730 MB) and verify its SHA-256.
    resume: carry on from a `.part` file left by an interrupted download. The server is asked for the rest
    (`Range`); if it sends only that (206) the bytes already there are hashed again and the rest appended,
    if it sends the whole file (200) the download starts over. A download that ends early (OSError "incomplete")
    leaves its .part for the next resume; a full-size file with the wrong SHA-256 is thrown away."""
    repo, rev, name, size, sha = KNOWN[key]
    dest = dest_root / key
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / name
    if not (final.is_file() and final.stat().st_size == size and _sha256(final) == sha):
        url = f"{base_url}/{repo}/resolve/{rev}/{name}?download=true"
        tmp = dest / (name + ".part")
        headers = {"User-Agent": "parakeet-dictate"}
        have = tmp.stat().st_size if resume and tmp.is_file() else 0
        if 0 < have < size:                     # a .part that is already as big as the file cannot be a prefix
            headers["Range"] = f"bytes={have}-"
        else:
            have = 0
        h = hashlib.sha256()
        done = 0
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            if have and r.status == 206:
                with tmp.open("rb") as old:
                    while chunk := old.read(4 << 20):
                        h.update(chunk)
                done, mode = have, "ab"
            else:
                mode = "wb"
            with tmp.open(mode) as out:
                while chunk := r.read(1 << 20):
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    progress(done, size)
        if done < size:  # the server or a proxy closed the connection early: keep the .part so resume can finish it
            raise OSError(f"{name}: incomplete download ({done} of {size} bytes), the partial file is kept")
        if h.hexdigest() != sha:
            tmp.unlink(missing_ok=True)
            raise OSError(f"{name}: checksum mismatch, download discarded")
        tmp.replace(final)
    progress(size, size)
    m = inspect(final, "Parakeet Dictate (downloaded)")
    if not m:
        raise OSError(f"downloaded model {final} is not a usable speech model")
    return m


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()
