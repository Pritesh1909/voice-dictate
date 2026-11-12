"""Turn raw Parakeet output into clean dictation text.

Everything here is deterministic word-list logic: it only removes fillers, collapses stutters and applies
the user's exact replacements and dictionary spellings. It never rewords anything else (no AI rewrite step).
Filler removal is ported from Orca 1.4.256 (shared/dictation-filler-words.js): each language has its own
thinking sounds, and the user's [cleanup] fillers are added on top.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from . import dictionary

# token = leading openers + core word + trailing punctuation
_SPLIT = re.compile(r"^([\"'(\[{“‘]*)(.*?)([.,!?;:…\"')\]}”’\-]*)$", re.S)
_WORD = re.compile(r"^[A-Za-z]+(?:'[A-Za-z]+)?$")


def _split(tok: str) -> tuple[str, str, str]:
    m = _SPLIT.match(tok)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", tok, "")


def _stretchable(words: Iterable[str]) -> str | None:
    """Regex alternatives for base filler spellings, each run of letters stretched but not shortened."""
    alts = set()
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        alts.add("".join(f"{re.escape(run[0])}{{{len(run)},}}" for run in
                         (m.group(0) for m in re.finditer(r"(.)\1*", w))))
    return "|".join(sorted(alts, key=len, reverse=True)) if alts else None


def filler_regex(words: Iterable[str]) -> re.Pattern | None:
    """Build one case-insensitive full-word pattern from base filler spellings.

    Each run of letters may be stretched but not shortened: "uh" matches uh/uhh/uuhh, "ah" matches AHH,
    "mm" matches mm/mmm but never a lone "M", "hm" matches hm/hmm.
    """
    alts = _stretchable(words)
    return re.compile(f"(?:{alts})", re.I) if alts else None


# Sounds people make while thinking, per language. Never words with meaning ("like", "so", "well", "那个", "그").
FILLERS_BY_LANGUAGE = {
    "en": r"u+m+|u+h+m*|er|e+r+m+|h+m+|m{3,}",
    "de": r"ä+h+m*|ö+h+m*|e+h+m+|h+m+|m{3,}",
    "es": r"e+h{2,}|e+h+m+|e+m+|h+m+|m{3,}",
    "fr": r"e+u+h+|h+e+u+|h+u+m+|h+m+|m{3,}",
    "it": r"e+h{2,}|e+h+m+|u+h+m+|h+m+|m{3,}",
    "pt": r"h+ã+|ã+h+|h+u+m+|h+m+|m{3,}",
    "nl": r"e+h+m*|u+h+m*|h+m+|m{3,}",
    "ru": r"э+м*|х+м+|м{3,}",
    "ja": r"え[ーえっ]*と|えー+|あのー+|うー+ん|んー+",
    "ko": r"으*음+|흠+",
    # 嗯 alone is also "yes", so only the one a pause follows goes.
    "zh": r"嗯+(?=\s*[，,、])|呃+|[额額]+",
}
FILLERS_BY_LANGUAGE["yue"] = FILLERS_BY_LANGUAGE["zh"]
# For an unknown language: English fillers that are no word elsewhere ("um" is German and Portuguese, "er" German).
LANGUAGE_NEUTRAL_FILLERS = r"u+h+m*|u+m{2,}|e+r+m+|h+m+|m{3,}"

# What joins a filler to a word. Orca's [\p{L}\p{N}\p{M}_/'’-]: Python's \w has the letters, digits and "_",
# and the common combining-mark blocks stand in for \p{M}.
_WORD_CHAR = r"[\w/'’\-̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-゙゚︯]"
# The comma or ellipsis after a filler goes with it.
_TRAILING_PAUSE = r"(?:\s*(?P<pause>[,，、…]|\.{3}))?\s*"
_SENTENCE_END_CHARS = ".!?。！？"
_SENTENCE_END_RE = re.compile(r"[.!?。！？]+\s*")
_QUESTION_OR_CALL_RE = re.compile(r"\s*[?？!！]")
# A comma left in front of a removed filler that ended the sentence or the text.
_TRAILING_SOFT_PUNCTUATION_RE = re.compile(r"[\s,，、]+$")
# The comma before a filler that a pause also follows, keeping the space after it.
_COMMA_BEFORE_PAUSED_FILLER_RE = re.compile(r"[,，、](\s*)$")
_LEADING_LETTERS_RE = re.compile(r"[^\W\d_]+")


def _standalone(alternatives: list[str]) -> re.Pattern:
    """A filler stands alone: nothing joins it to a word (a dot only does inside a name like "um.txt")."""
    return re.compile(rf"(?<!{_WORD_CHAR})(?<!{_WORD_CHAR}\.)({'|'.join(alternatives)})"
                      rf"(?!{_WORD_CHAR}|\.{_WORD_CHAR}){_TRAILING_PAUSE}", re.I)


@dataclass(frozen=True)
class Fillers:
    """Compiled filler rules for one set of dictation languages."""
    pattern: re.Pattern            # the language's sounds (or the language-neutral set) + the user's own
    plain: re.Pattern | None       # only the user's own: for non-Latin text when the language is unknown
    neutral: bool                  # no list for the language, so `plain` applies to non-Latin text


def filler_rules(languages: Iterable[str] = (), extra: Iterable[str] = (),
                 language_fillers: bool = True) -> Fillers | None:
    """What to remove from text in `languages` (ISO-639-1 codes; "zh-CN" counts as "zh"; none = unknown),
    plus the user's `extra` base spellings. None when there is nothing to remove."""
    user = _stretchable(extra)
    lists: list[str] = []
    if language_fillers:
        for code in languages:
            f = FILLERS_BY_LANGUAGE.get(code.lower().split("-")[0])
            if f and f not in lists:
                lists.append(f)
    neutral = language_fillers and not lists
    if neutral:
        lists.append(LANGUAGE_NEUTRAL_FILLERS)
    if user:
        lists.append(user)
    if not lists:
        return None
    return Fillers(_standalone(lists), _standalone([user]) if neutral and user else None, neutral)


def _has_non_latin_letter(text: str) -> bool:
    return any(ch.isalpha() and "LATIN" not in unicodedata.name(ch, "") for ch in text if ord(ch) > 0x7F)


def _all_caps_word(word: str) -> bool:
    """In mixed-case text an all-caps "UM" or "ER" is more likely a name or acronym than a sound. A stretched
    one ("AHH", "HMM", "UMM": a letter doubled) is still a sound."""
    return (len(word) > 1 and word == word.upper() and word != word.lower()
            and not re.search(r"(.)\1", word.lower()))


def _capitalize_lowercase_word(text: str) -> str:
    m = _LEADING_LETTERS_RE.match(text)
    if not m or m.group(0) != m.group(0).lower():
        return text
    return text[:1].upper() + text[1:]


def _cap_first(tok: str) -> str:
    for i, ch in enumerate(tok):
        if ch.isalpha():
            return tok[:i] + ch.upper() + tok[i + 1:]
    return tok


def remove_fillers(text: str, rules: Fillers | None) -> str:
    """Drop standalone filler sounds and the pause punctuation after them; text with none comes back unchanged."""
    if rules is None:
        return text
    pattern = rules.plain if rules.neutral and _has_non_latin_letter(text) else rules.pattern
    if pattern is None:
        return text
    out, cursor, cap_next = "", 0, False
    # Some models write everything in capitals, where "UM" is still a sound.
    caseless = text == text.upper()

    def keep(segment: str) -> None:
        nonlocal out, cap_next
        if segment:
            out += _capitalize_lowercase_word(segment) if cap_next else segment
            cap_next = False

    for m in pattern.finditer(text):
        filler = m.group(1)
        if not caseless and _all_caps_word(filler):
            continue
        before = (out + text[cursor:m.start()]).rstrip()
        at_start = before == "" or before[-1] in _SENTENCE_END_CHARS
        # "Hm?" or "Eh!" on its own is a question or a call, so it stays; "Is it ready, uh?" loses the "uh".
        if at_start and _QUESTION_OR_CALL_RE.match(text, m.end(1)):
            continue
        keep(text[cursor:m.start()])
        cursor = m.end()
        end = _SENTENCE_END_RE.match(text, cursor)
        if end and at_start:
            cursor = end.end()                                   # "Um." on its own goes entirely
        elif end:
            out = _TRAILING_SOFT_PUNCTUATION_RE.sub("", out)     # "we should, um." keeps its full stop
        elif m.group("pause"):
            # "we should, uh, ship" set the filler off on both sides; neither comma belongs to the sentence.
            out = _COMMA_BEFORE_PAUSED_FILLER_RE.sub(r"\1", out)
        # The next word begins the sentence when a capitalised "Um" began it, or when the text began with the
        # filler (Orca only does the first; a lower-case "um, deploy it" also becomes "Deploy it" here).
        cap_next = cap_next or (at_start and (filler[:1] != filler[:1].lower() or before == ""))
    if cursor == 0:
        return text
    if cursor == len(text):
        out = _TRAILING_SOFT_PUNCTUATION_RE.sub("", out)     # a filler at the very end leaves the comma dangling
    else:
        keep(text[cursor:])
    return out.strip()


def remove_repeats(text: str, allowed: Iterable[str] = ()) -> str:
    """Collapse stutters like "the the" / "I, I" / "to- to". Words in `allowed` may legitimately repeat."""
    keep = {w.lower() for w in allowed}
    out: list[str] = []
    for tok in text.split():
        if out:
            pl, pc, pt = _split(out[-1])
            _, c, t = _split(tok)
            if (pc and _WORD.match(pc) and pc.lower() == c.lower() and pc.lower() not in keep
                    and pt in ("", ",", "-")):
                out[-1] = pl + pc + t
                continue
        out.append(tok)
    return " ".join(out)


def compile_replacements(pairs: Iterable[tuple[str, str]]) -> list[tuple[re.Pattern, str]]:
    compiled = []
    for src, dst in pairs:
        words = src.split()
        if not words:
            continue
        rx = r"(?<![\w'])" + r"[\s\-]+".join(re.escape(w) for w in words) + r"(?![\w'])"
        compiled.append((re.compile(rx, re.I), dst))
    return compiled


def apply_replacements(text: str, compiled: list[tuple[re.Pattern, str]]) -> str:
    for rx, dst in compiled:
        text = rx.sub(lambda _m, d=dst: d, text)
    return text


def tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?;:…])", r"\1", text)      # no space before punctuation
    text = re.sub(r",+(?=[.!?;:…])", "", text)          # ",." -> "."
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r"^[,;:\-\s]+", "", text)             # nothing dangling at the start
    return text


@dataclass
class Cleaner:
    fillers: list[str] = field(default_factory=list)                  # the user's own, on top of the language's
    remove_repeated_words: bool = True
    repeated_words_allowed: list[str] = field(default_factory=list)
    replacements: list[tuple[str, str]] = field(default_factory=list)
    dictionary: list[tuple[str, str]] = field(default_factory=list)   # (term, heard_as or "")
    app_dictionary: list[tuple[str, str]] = field(default_factory=list)  # one program's terms: win over yours
    languages: tuple[str, ...] = ()                                   # what is dictated; () = unknown
    language_fillers: bool = True                                     # remove that language's filler sounds

    def __post_init__(self) -> None:
        self._fillers = filler_rules(self.languages, self.fillers, self.language_fillers)
        self._repl = compile_replacements(self.replacements)
        self._spell = dictionary.build(self.dictionary, ahead=self.app_dictionary)

    @classmethod
    def from_config(cls, c, languages: Iterable[str] = (),
                    app_terms: Iterable[tuple[str, str]] = (),
                    learned: Iterable[tuple[str, str]] = ()) -> "Cleaner":
        """`app_terms` (one program's [dictionary.apps] entry) come first, so they win over your own.
        `learned` (from "Correct last dictation…") come right after your own [dictionary]."""
        return cls(fillers=list(c.fillers), remove_repeated_words=c.remove_repeated_words,
                   repeated_words_allowed=list(c.repeated_words_allowed), replacements=list(c.replacements),
                   dictionary=list(c.dictionary) + list(learned), app_dictionary=list(app_terms),
                   languages=tuple(languages), language_fillers=c.language_fillers)

    def with_extra(self, entries: Iterable[tuple[str, str]]) -> "Cleaner":
        """This clean-up plus lower-ranked terms for one dictation (context words: the window title, your
        notes). Only the respelling step is rebuilt (a few hundred terms: well under a millisecond), and
        this Cleaner is left as it is, so a cached per-program Cleaner stays valid."""
        entries = list(entries)
        if not entries:
            return self
        c = copy.copy(self)
        c._spell = dictionary.build(list(self.dictionary) + entries, ahead=self.app_dictionary)
        c._prefixes = None
        return c

    def may_continue(self, text: str) -> bool:
        """True when the last words of `text` could be the start of a longer term or replacement, so the
        next word might still change how they are spelled ("key" before "cloak" -> "Keycloak"). Live
        streaming holds such words back instead of typing them and having to take them back."""
        if getattr(self, "_prefixes", None) is None:
            keys = set(getattr(self._spell, "keys", ()))
            keys.update(dictionary.word_key(src) for src, _ in self.replacements)
            self._prefixes = dictionary.key_prefixes(k for k in keys if k)
        words = text.split()
        if not words or words[-1][-1:] in ",;:!?":
            return False          # a comma etc. ends any term
        for n in range(1, min(dictionary.MAX_WORDS_PER_TERM, len(words)) + 1):
            if dictionary.word_key(" ".join(words[-n:])) in self._prefixes:
                return True
        return False

    def clean(self, raw: str) -> str:
        text = remove_fillers(raw.replace("\n", " "), self._fillers)
        if self.remove_repeated_words:
            text = remove_repeats(text, self.repeated_words_allowed)
        text = apply_replacements(text, self._repl)
        text = tidy(text)
        if raw[:1].isupper() and text:
            text = _cap_first(text)
        # Terms last, so a lower-case term at the start ("pnpm") keeps its spelling.
        return self._spell(text) if self._spell else text
