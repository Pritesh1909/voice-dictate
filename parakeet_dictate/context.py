"""Context words: code names and acronyms from the active window and the user's notes, as dictionary hints.

They only fix the spelling, spacing and capitals of words the user actually said: the dictionary
respells a run of spoken words whose joined key equals a term's key ("mcp auth filter" ->
"McpAuthFilter", "tx ramp" -> "TX-RAMP"). Two sources:

1. The foreground window at dictation start: its title ("McpAuthFilter.java - ss-core-service -
   IntelliJ IDEA") and its exe name. See title_terms().
2. Knowledge-base folders ([context] folders, e.g. an Obsidian vault): every .md note is scanned in
   the background, never on the hotkey path. See ContextScanner.

Safety rule: a harvested term must never recase ordinary speech. So only terms whose key takes two or
more spoken words to form are kept (CamelCase with 2+ parts, hyphen/dot/underscore-joined names, letters
mixed with digits such as "V2"), plus ALL-CAPS acronyms. A single plain word ("Docker", "Notes") is
never harvested, and neither is anything whose key is itself an ordinary word ("IDEA" -> "idea",
"WorkFlow" -> "workflow"): ordinary means in the built-in list of common words or written in lower case
in the notes' prose. Compounds made only of ordinary words ("read-only", "follow-up") are kept only
when the notes write them as code (in `backticks`) at least as often as in prose.

Privacy: notes are only read. The cache (%LOCALAPPDATA%\\ParakeetDictate\\context_terms.json) holds the
extracted terms, file times and sizes, and short one-way hashes of the lower-case words seen in prose
(to tell ordinary words apart on the next, incremental, scan); never note text. Nothing about notes is
logged except counts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .dictionary import word_key

log = logging.getLogger(__name__)

CACHE_VERSION = 1
MAX_NOTE_BYTES = 2_000_000      # a giant pasted log is not worth scanning past this
MAX_TOKEN = 40
MAX_CODE_TOKEN = 32             # fenced code blocks: long tokens there are data, not names
MAX_DIGITS = 3                  # "SEC-12", "V2" yes; dates, build numbers and versions no
SKIP_DIRS = {"node_modules", "__pycache__"}   # plus every folder starting with "." (.obsidian, .trash, .git)

W_PROSE, W_CODE_SPAN, W_HEADING, W_CODE_BLOCK = 1.0, 2.0, 3.0, 0.5

# Common English words (and a few chat words) that ALL-CAPS or CamelCase spellings could collide with.
# The notes' own lower-case prose words are added to this at scan time.
COMMON = frozenset("""
a i ad am an as at aw ax be by co do ex go ha re un mid non pre sub anti semi multi he hi if in is it me my no of oh ok on or so to up us we ya yes
all and any are ask big bit but can day did end few for get got had has her him his how its let lot
may new nor not now off old one our out own pay put ran run saw say see set she sit six ten the too
top try two use via was way who why win yet you
able also back been best both call came come does done down each easy even ever fact fine find five
four free from full give goes good half hand have help here high hold home hope idea info into item
just keep kind knew know last late left less like line list live long look made main make many mark
mean meet mind more most move much must name near need next nice none note once only open over page
part pass past plan play read real rest role room rule safe said same save seen self send show shut
side sign site some soon sort stay step stop such sure take talk task team tell test text than that
them then they this time todo told tool town tree true turn type unit upon used user very view wait
want week well went were what when whom wide will with word work year your zero
about above after again agent along alter among apply array basic begin being below block board
build built cache catch cause check class clean clear click close count cover cross daily debug
delay doing draft drive early email empty entry error event every field final first fixed flag
flow focus force found frame front given going great group guard guide happy heard hello input
issue items label large later learn level light limit local login logic lower major match maybe
media merge might model money month never night notes other owner panel paper parse party patch
phase phone place plain point power press print proxy query queue quick quiet quite range ready
reply reset right route rules scope score setup shape share shell short since small sound space
spell split stack stage start state still store story study style table taken teams thank their
theme there these thing think those three today token total touch track trust under until upper
usage valid value video watch water where which while white whole world would write wrong yours
access accept action active actual always amount answer before better called cannot change chosen
client column config course create custom delete design detail device direct during enable engine
enough entire except export filter folder follow format future global handle health import inside
latest launch layout listen manage manual market member memory method mobile modern moment number
object office online option output people period please policy prefer pretty public random reason
record remote remove report result return review sample screen script search second secret select
server should signal simple single source status stream string strong submit switch system target
tenant thanks ticket toggle travel update upload useful wallet window within worker
account address another because between booking browser careful certain channel comment company
connect content context control correct current default details display example feature general
history however install instead journal keyword message minimum missing network nothing private
problem process product project publish quickly running service session setting similar started
storage support testing through tracker usually version warning website without working
""".split())

_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.M | re.S)
_CODE_SPAN = re.compile(r"`+([^`\n]+?)`+")
_EMBED = re.compile(r"!\[\[[^\]\n]*\]\]")
_WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_MD_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")
_URL = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+", re.I)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+)$", re.M)
_HARD_SEP = re.compile(r"[^A-Za-z0-9._\-]+")
_JOINERS = re.compile(r"[._\-]+")
_CAMEL_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")
_ACRONYM = re.compile(r"^[A-Z]{2,}\d*$|^[A-Z]{2,}s$")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HEXISH = re.compile(r"^(?=.*\d)[0-9a-f]{7,}$|^[0-9a-fA-F]{8}-[0-9a-fA-F-]{13,}$")
_NUMBER_UNIT = re.compile(r"^\d+[a-z]{1,3}$")
_PLAIN_LOWER = re.compile(r"^[a-z]{2,}$")
_PUNCT = "\"'()[]{}<>*~,;:!?|`"
_EXTENSIONS = frozenset("java py ts tsx js jsx md toml yml yaml json xml sql sh ps1 txt kt go rs cs html css "
                        "exe dll jar iss bat cmd ini cfg log csv vue".split())


def _hash(word: str) -> str:
    return hashlib.blake2b(word.encode("utf-8"), digest_size=4).hexdigest()


def _parts(token: str) -> list[str]:
    """Spoken parts of a name, lower case: "McpAuthFilter" -> mcp/auth/filter, "TX-RAMP" -> tx/ramp."""
    return [p.lower() for seg in _JOINERS.split(token) for p in _CAMEL_PART.findall(seg)]


def _shape_ok(token: str) -> bool:
    """True for shapes that take two or more spoken words to say, and for acronyms."""
    if _ACRONYM.match(token):
        return True
    segments = [s for s in _JOINERS.split(token) if s]
    if len(segments) >= 2:
        return any(len(s) > 1 for s in segments)        # "ss-core-service" yes, "e.g" / "U.S" no
    s = segments[0] if segments else ""
    has_digit, has_alpha = any(c.isdigit() for c in s), any(c.isalpha() for c in s)
    if has_digit and has_alpha:
        return True                                      # "V2", "EC2", "Route53"
    return len(_CAMEL_PART.findall(s)) >= 2              # "McpAuthFilter", "OAuth", "IntelliJ"


def _junk(token: str, max_len: int) -> bool:
    if len(token) < 2 or len(token) > max_len or not _HAS_LETTER.search(token):
        return True
    if sum(c.isdigit() for c in token) > MAX_DIGITS or _NUMBER_UNIT.match(token) or _HEXISH.match(token):
        return True
    return False


def tokens(text: str, max_len: int = MAX_TOKEN) -> Iterable[str]:
    """Name-shaped tokens in a piece of text. URLs and file paths are skipped whole."""
    for chunk in text.split():
        chunk = chunk.strip(_PUNCT)
        if not chunk or "/" in chunk or "\\" in chunk or "@" in chunk or "://" in chunk:
            continue
        for tok in _HARD_SEP.split(chunk):
            tok = tok.strip("._-")
            if not tok or _junk(tok, max_len) or not _shape_ok(tok):
                continue
            yield tok
            stem, dot, ext = tok.rpartition(".")
            if dot and ext.lower() in _EXTENSIONS and not _junk(stem, max_len) and _shape_ok(stem):
                yield stem                                # "McpAuthFilter.java" -> also "McpAuthFilter"


@dataclass
class NoteTerms:
    terms: dict[str, list[float]]   # spelling -> [weight, code hits, prose hits]
    words: set[str]                 # hashes of lower-case prose words

    def add(self, text: str, weight: float, code: bool, max_len: int = MAX_TOKEN) -> None:
        for tok in tokens(text, max_len):
            rec = self.terms.setdefault(tok, [0.0, 0, 0])
            rec[0] += weight
            rec[1 if code else 2] += 1


def harvest(text: str, title: str = "") -> NoteTerms:
    """Candidate terms of one note, weighted by where they appear, plus its ordinary-word hashes."""
    out = NoteTerms({}, set())
    out.add(title, W_HEADING, code=False)
    text = _URL.sub(" ", _MD_LINK_TARGET.sub("] ", _EMBED.sub(" ", text)))
    blocks = []

    def take_block(m):
        blocks.append(m.group(0))
        return "\n"
    text = _FENCE.sub(take_block, text)
    for b in blocks:
        out.add(b, W_CODE_BLOCK, code=True, max_len=MAX_CODE_TOKEN)
    spans = []

    def take_span(m):
        spans.append(m.group(1))
        return " "
    text = _CODE_SPAN.sub(take_span, text)
    for s in spans:
        out.add(s, W_CODE_SPAN, code=True)
    links = []

    def take_link(m):
        target, _, alias = m.group(1).partition("|")
        links.append(target.split("#")[0])
        return " " + (alias or "") + " "
    text = _WIKILINK.sub(take_link, text)
    for t in links:
        out.add(t, W_HEADING, code=False)
    headings = []

    def take_heading(m):
        headings.append(m.group(1))
        return ""
    text = _HEADING.sub(take_heading, text)
    for h in headings:
        out.add(h, W_HEADING, code=False)
    out.add(text, W_PROSE, code=False)
    out.words = {_hash(w) for w in _prose_words(" ".join([text] + headings))}
    return out


def _prose_words(text: str) -> Iterable[str]:
    """Stand-alone lower-case words ("deploy", "workflow"), not the pieces of "ss-core-service"."""
    for chunk in text.split():
        w = chunk.strip(_PUNCT + ".")
        if _PLAIN_LOWER.match(w):
            yield w


def _ordinary(word: str, word_hashes: set[str]) -> bool:
    return word in COMMON or _hash(word) in word_hashes


def _ordinary_compound(token: str, words: set[str]) -> bool:
    """Made only of ordinary words and plain numbers: "read-only", "24-hour", "UserName".
    Fused letters and digits ("Route53", "V2") or an unusual part ("Mcp", "ss") make it a name."""
    if len(_parts(token)) < 2:
        return False
    segments = [s for s in _JOINERS.split(token) if s]
    if any(s.isdigit() for s in segments) and all(s.isdigit() or s.islower() for s in segments):
        return True          # "24-hour", "3-way", "64-bit"; names with numbers have capitals: "SEC-12"
    for seg in segments:
        if seg.isdigit():
            continue
        if any(c.isdigit() for c in seg):
            return False
        if not all(_ordinary(p, words) for p in _parts(seg)):
            return False
    return True


def rank(notes: Iterable[NoteTerms], max_terms: int) -> list[str]:
    """The best terms across notes: frequency (weighted by where they appear) times spread across notes."""
    notes = list(notes)
    words: set[str] = set().union(*(n.words for n in notes)) if notes else set()
    by_key: dict[str, dict] = {}
    for n in notes:
        seen_here = set()
        for spelling, (weight, code, prose) in n.terms.items():
            k = word_key(spelling)
            g = by_key.setdefault(k, {"weight": 0.0, "code": 0, "prose": 0, "notes": 0, "spell": {}})
            g["weight"] += weight
            g["code"] += code
            g["prose"] += prose
            g["spell"][spelling] = g["spell"].get(spelling, 0.0) + weight
            if k not in seen_here:
                seen_here.add(k)
                g["notes"] += 1
    scored = []
    for k, g in by_key.items():
        best = max(g["spell"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        if not k or _ordinary(k, words):
            continue
        if _ordinary_compound(best, words) and g["code"] < max(1, g["prose"]):
            continue                                     # "read-only", "follow-up": English, not code
        scored.append((math.log2(1 + g["weight"]) * g["notes"], best))
    scored.sort(key=lambda s: (-s[0], s[1].lower()))
    return [t for _, t in scored[:max_terms]]


def title_terms(title: str, exe: str = "", is_ordinary: Callable[[str], bool] | None = None) -> list[str]:
    """Terms from the foreground window's title and exe name, e.g.
    "McpAuthFilter.java - ss-core-service - IntelliJ IDEA" -> McpAuthFilter.java, McpAuthFilter,
    ss-core-service, IntelliJ ("IDEA" is dropped: its key is the ordinary word "idea")."""
    ordinary = is_ordinary or (lambda w: w in COMMON)
    stem = exe[:-4] if exe.lower().endswith(".exe") else exe
    out, seen = [], set()
    for tok in list(tokens(title or "")) + list(tokens(stem or "")):
        k = word_key(tok)
        if k and k not in seen and not ordinary(k):
            seen.add(k)
            out.append(tok)
    return out


# --- background scanner with a cache --------------------------------------------------------------

@dataclass
class ScanStats:
    files: int = 0
    rescanned: int = 0
    terms: int = 0
    seconds: float = 0.0


def _note_files(folders: Iterable[str]) -> Iterable[Path]:
    for folder in folders:
        root = Path(os.path.expandvars(folder))
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            for f in filenames:
                if f.lower().endswith(".md"):
                    yield Path(dirpath) / f


def _read_note(p: Path) -> str:
    with open(p, "rb") as f:
        return f.read(MAX_NOTE_BYTES).decode("utf-8", errors="replace")


class ContextScanner:
    """Knowledge-base terms, kept fresh in the background.

    terms is ready at once from the cache of the last run; refresh() rescans on a background thread
    (only notes whose time or size changed are read again) when the last scan is older than
    refresh_minutes. Nothing here ever blocks the hotkey."""

    def __init__(self, cache_file: Path, folders: Iterable[str] = (), max_terms: int = 300,
                 refresh_minutes: int = 10, clock: Callable[[], float] = time.monotonic):
        self.cache_file = cache_file
        self.folders = tuple(folders)
        self.max_terms = max_terms
        self.refresh_minutes = refresh_minutes
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_scan: float | None = None
        self._files: dict[str, dict] = {}
        self._words: set[str] = set()
        self.terms: list[str] = []
        self.last_stats: ScanStats | None = None
        self._load_cache()

    def configure(self, folders: Iterable[str], max_terms: int, refresh_minutes: int) -> None:
        """New settings: a changed folder list or size rescans on the next refresh()."""
        folders = tuple(folders)
        if folders != self.folders or max_terms != self.max_terms:
            self._last_scan = None
        self.folders, self.max_terms, self.refresh_minutes = folders, max_terms, refresh_minutes

    def is_ordinary(self, word: str) -> bool:
        return _ordinary(word, self._words)

    def _load_cache(self) -> None:
        try:
            d = json.loads(self.cache_file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if d.get("version") != CACHE_VERSION or not isinstance(d.get("files"), dict):
            return
        self._files = d["files"]
        self._words = {h for rec in self._files.values() for h in rec.get("words", [])}
        if tuple(d.get("folders", ())) == self.folders and d.get("max_terms") == self.max_terms:
            self.terms = [t for t in d.get("terms", []) if isinstance(t, str)]

    def _save_cache(self) -> None:
        d = {"version": CACHE_VERSION, "folders": list(self.folders), "max_terms": self.max_terms,
             "terms": self.terms, "files": self._files}
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_name(self.cache_file.name + ".tmp")
            tmp.write_text(json.dumps(d, separators=(",", ":")), "utf-8")
            os.replace(tmp, self.cache_file)
        except OSError as e:
            log.warning("context cache not saved: %s", e)

    def due(self) -> bool:
        if not self.folders:
            return False
        return self._last_scan is None or self._clock() - self._last_scan >= self.refresh_minutes * 60

    def refresh(self, force: bool = False) -> bool:
        """Start a background rescan if one is due. Returns True if a scan was started."""
        with self._lock:
            if (self._thread and self._thread.is_alive()) or not (force or self.due()):
                return False
            self._thread = threading.Thread(target=self._scan_safely, daemon=True, name="context-scan")
            self._thread.start()
            return True

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t:
            t.join(timeout)

    def _scan_safely(self) -> None:
        try:
            s = self.scan()
            log.info("context: %d notes (%d read), %d terms in %.2fs", s.files, s.rescanned, s.terms, s.seconds)
        except Exception:
            log.exception("context scan")

    def scan(self) -> ScanStats:
        """Rescan now, on this thread: read changed notes, rank, save the cache."""
        t0 = time.perf_counter()
        stats = ScanStats()
        files: dict[str, dict] = {}
        for p in _note_files(self.folders):
            try:
                st = p.stat()
            except OSError:
                continue
            key = str(p)
            old = self._files.get(key)
            stats.files += 1
            if old and old.get("mtime_ns") == st.st_mtime_ns and old.get("size") == st.st_size:
                files[key] = old
                continue
            try:
                n = harvest(_read_note(p), p.stem)
            except OSError:
                continue
            stats.rescanned += 1
            files[key] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size,
                          "terms": n.terms, "words": sorted(n.words)}
        notes = [NoteTerms({k: list(v) for k, v in rec["terms"].items()}, set(rec.get("words", [])))
                 for rec in files.values()]
        terms = rank(notes, self.max_terms)
        self._files, self._words = files, set().union(*(n.words for n in notes)) if notes else set()
        self.terms = terms
        self._last_scan = self._clock()
        self._save_cache()
        stats.terms, stats.seconds = len(terms), time.perf_counter() - t0
        self.last_stats = stats
        return stats


def dictionary_entries(*sources: Iterable[str]) -> list[tuple[str, str]]:
    """(term, "") dictionary entries from term lists given in precedence order (first wins per key)."""
    out, seen = [], set()
    for src in sources:
        for t in src:
            k = word_key(t)
            if k and k not in seen:
                seen.add(k)
                out.append((t, ""))
    return out
