"""Spoken numbers written the way a developer types them: "port six zero four zero" -> "port 6040".

Parakeet v2 already writes most numbers as digits ("25%", "version 2.5", "port 6040", "512MB"). This is
the safety net for the ones it leaves as words, and it only converts where the meaning is clearly a
number (measured with Windows text-to-speech through the real model, see the README):

- next to a unit or percent sign: "three replicas" -> "3 replicas", "five minutes" -> "5 minutes",
  "twenty five percent" -> "25%" (and "25 percent" / "25 %" -> "25%");
- after a keyword: "port sixty forty" -> "port 6040", "Java twenty one" -> "Java 21", "v two" -> "v2";
- after one of your ticket prefixes: "SEC twelve" / "SEC12" -> "SEC-12", "Net 03" -> "NET-03";
- spoken digit by digit, three digits or more: "five five five one two three four" -> "5551234";
- with "point": "two point five" -> "2.5", "two point zero point one" -> "2.0.1";
- "minus five" -> "-5" (not after another number: "ten minus five" is a sum and stays);
- times: "at ten thirty" / "at 10.30" -> "at 10:30", "3.15 p.m." -> "3:15 p.m.".

Prose stays as it is: "one of the", "a couple", "for the", "to the", "no one", "once", "one-to-one",
"Two things are broken", "twenty-first". Deterministic word lists only, English only.
"""
from __future__ import annotations

import re
from typing import Iterable

_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine".split())}
_TEENS = {w: i + 10 for i, w in enumerate(
    "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: (i + 2) * 10 for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
# Never a stutter: "twenty twenty four", "five five five" (the repeat remover leaves these alone).
NUMBER_WORDS = frozenset([*_ONES, *_TEENS, *_TENS, "oh", "hundred", "thousand"])

# After these the next number is a number ("port", "version") or a product version ("Java 21").
KEYWORDS = frozenset("""port version page line step issue ticket chapter section
    java jdk python node nodejs postgres postgresql mysql ubuntu debian windows macos ios android tls
    angular react vue typescript kotlin scala gradle maven tomcat jetty redis kafka""".split())
KEYWORDS_2 = frozenset({"spring boot"})
# Units that make the number before them a number. Abbreviations match exactly, words in any case.
# Time units only in the plural, so "one second" / "one minute" (as in "give me one minute") stay.
UNIT_ABBREVIATIONS = frozenset("""KB MB GB TB PB Kb Mb Gb kB ms ns µs px GHz MHz Hz kbps Mbps Gbps
    fps rps qps RPS QPS TPS CPU CPUs vCPU vCPUs GPU GPUs""".split())
UNIT_WORDS = frozenset("""byte bytes kilobyte kilobytes megabyte megabytes gigabyte gigabytes terabyte terabytes
    bits kilobits megabits gigabits gigs megs milliseconds microseconds nanoseconds seconds secs minutes mins
    hours hrs days degrees pixels cores threads replicas instances pods nodes workers shards partitions
    retries""".split())
_AM_PM = frozenset({"a.m", "p.m", "AM", "PM"})
# What may follow a time like "at 10.30" (besides the end of a clause and a.m./p.m.).
_AFTER_TIME = frozenset("today tomorrow tonight sharp on in and or then every each local utc gmt ist est pst cet"
                        .split())

# token = leading openers + core word + trailing punctuation (as in textclean)
_SPLIT = re.compile(r"^([\"'(\[{“‘]*)(.*?)([.,!?;:…\"')\]}”’\-]*)$", re.S)
_DIGITS = re.compile(r"\d+(?:\.\d+)*")
_TIME_DOT = re.compile(r"(\d{1,2})\.([0-5]\d)")
_JOINED_ID = re.compile(r"([A-Za-z]+)-?(\d+)")


def _split(tok: str) -> tuple[str, str, str]:
    m = _SPLIT.match(tok)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", tok, "")


def _small(w: str) -> int | None:
    """0-99 from one word: "seven", "twelve", "forty", "Twenty-One"."""
    if w in _ONES:
        return _ONES[w]
    if w in _TEENS:
        return _TEENS[w]
    if w in _TENS:
        return _TENS[w]
    a, _, b = w.partition("-")
    if a in _TENS and _ONES.get(b, 0) > 0:
        return _TENS[a] + _ONES[b]
    return None


def is_number_word(w: str) -> bool:
    w = w.lower()
    return _small(w) is not None or w in ("hundred", "thousand")


class _Text:
    """The tokens of one text and the parsers over them. A number never runs past punctuation."""

    def __init__(self, text: str, prefixes: frozenset[str]):
        self.toks = text.split()
        self.items = [_split(t) for t in self.toks]
        self.words = [core.lower() for _, core, _ in self.items]
        n = len(self.toks)
        # joined[k]: token k runs on into token k+1 (no punctuation between them)
        self.joined = [k + 1 < n and not self.items[k][2] and not self.items[k + 1][0] for k in range(n)]
        self.prefixes = prefixes

    def word(self, k: int) -> str:
        return self.words[k] if 0 <= k < len(self.words) else ""

    def nxt(self, k: int) -> bool:
        """Token k+1 exists and continues token k."""
        return 0 <= k < len(self.toks) and self.joined[k]

    # --- word parsers: each returns (text, end) with `end` the index after the last word ---------------

    def small(self, k: int) -> tuple[int, int] | None:
        w = self.word(k)
        if w in _TENS and self.nxt(k) and _ONES.get(self.word(k + 1), 0) > 0:
            return _TENS[w] + _ONES[self.word(k + 1)], k + 2
        v = _small(w)
        return (v, k + 1) if v is not None else None

    def below_thousand(self, k: int) -> tuple[int, int] | None:
        s = self.small(k)
        if s is None:
            return None
        v, e = s
        if 0 < v < 100 and self.nxt(e - 1) and self.word(e) == "hundred":     # "five hundred", "fifteen hundred"
            v, e = v * 100, e + 1
            rest = self._and_small(e)
            if rest:
                v, e = v + rest[0], rest[1]
        return v, e

    def _and_small(self, e: int) -> tuple[int, int] | None:
        if not self.nxt(e - 1):
            return None
        if self.word(e) == "and" and self.nxt(e):
            return self.small(e + 1)
        return self.small(e)

    def cardinal(self, k: int) -> tuple[int, int] | None:
        b = self.below_thousand(k)
        if b is None:
            return None
        v, e = b
        if 0 < v < 1000 and self.nxt(e - 1) and self.word(e) == "thousand":
            v, e = v * 1000, e + 1
            if self.nxt(e - 1):
                at = e + 1 if self.word(e) == "and" and self.nxt(e) else e
                rest = self.below_thousand(at)
                if rest:
                    v, e = v + rest[0], rest[1]
        return v, e

    def digit_run(self, k: int, least: int, oh_first: bool = False) -> tuple[str, int] | None:
        """Single digits said one by one: "six oh three zero" -> "6030"."""
        out, e = "", k
        while e < len(self.toks) and (e == k or self.nxt(e - 1)):
            w = self.word(e)
            if w in _ONES:
                out += str(_ONES[w])
            elif w == "oh" and (out or oh_first):
                out += "0"
            else:
                break
            e += 1
        return (out, e) if len(out) >= least else None

    def phrase(self, k: int, chunks: bool) -> tuple[str, int, str] | None:
        """(digits, end, kind) for the number words at k. kind: "digits" (said one by one), "cardinal" or
        "decimal". With `chunks` (after a keyword) "sixty forty" is 6040 and "nineteen ninety nine" 1999."""
        d, c = self.digit_run(k, 2), self.cardinal(k)
        if d is None and c is None:
            return None
        if d and (c is None or d[1] >= c[1]):
            text, e, kind = d[0], d[1], "digits"
        else:
            text, e, kind = str(c[0]), c[1], "cardinal"
        while chunks and self.nxt(e - 1):
            more = self.digit_run(e, 2) or self.cardinal(e)
            if more is None or (isinstance(more[0], int) and more[0] < 10):
                break
            text, e, kind = text + str(more[0]), more[1], "digits"
        while self.nxt(e - 1) and self.word(e) == "point" and self.nxt(e):
            frac = self.digit_run(e + 1, 1, oh_first=True) or self.small(e + 1)
            if frac is None:
                break
            text, e, kind = f"{text}.{frac[0]}", frac[1], "decimal"
        return text, e, kind

    def number_at(self, k: int, chunks: bool) -> tuple[str, int, str] | None:
        """A digit token or number words at k."""
        if 0 <= k < len(self.toks) and _DIGITS.fullmatch(self.items[k][1]):
            return self.items[k][1], k + 1, "token"
        return self.phrase(k, chunks)

    # --- context ------------------------------------------------------------------------------------

    def is_unit(self, k: int) -> bool:
        if not 0 <= k < len(self.toks):
            return False
        core = self.items[k][1]
        return core in UNIT_ABBREVIATIONS or core.lower() in UNIT_WORDS

    def percent(self, e: int) -> int | None:
        """Index after "percent" / "per cent" / "%" right after a number ending before e."""
        if not self.nxt(e - 1):
            return None
        if self.items[e][1] == "%" or self.word(e) == "percent":
            return e + 1
        if self.word(e) == "per" and self.nxt(e) and self.word(e + 1) == "cent":
            return e + 2
        return None

    def unit_after(self, e: int) -> bool:
        """A unit or % follows the number ending before e, also across a range ("two to three minutes")."""
        if not self.nxt(e - 1):
            return False
        if self.is_unit(e) or self.percent(e) or self.items[e][1] in _AM_PM:
            return True
        if self.word(e) in ("to", "or") and self.nxt(e):
            other = self.number_at(e + 1, False)
            if other and self.nxt(other[1] - 1) and (self.is_unit(other[1]) or self.percent(other[1])):
                return True
        return False

    def keyword_before(self, k: int) -> bool:
        if not self.nxt(k - 1):
            return False
        if self.word(k - 1) in KEYWORDS:
            return True
        return self.nxt(k - 2) and f"{self.word(k - 2)} {self.word(k - 1)}" in KEYWORDS_2

    def prefix(self, k: int) -> str | None:
        """Your spelling of the ticket prefix token k is (not when all lower case: "net 30" is prose)."""
        core = self.items[k][1] if 0 <= k < len(self.toks) else ""
        if core.islower() or core.upper() not in self.prefixes:
            return None
        return core.upper()

    def is_number(self, k: int) -> bool:
        return 0 <= k < len(self.toks) and (bool(_DIGITS.fullmatch(self.items[k][1])) or is_number_word(self.word(k)))

    def time_follows_ok(self, e: int) -> bool:
        """After "at 10:30": the end of a clause, a.m./p.m. or a word like "tomorrow" (not "at 1.25 per hour")."""
        if e >= len(self.toks) or not self.joined[e - 1]:
            return True
        return self.items[e][1] in _AM_PM or self.word(e) in _AFTER_TIME

    # --- the rules ------------------------------------------------------------------------------------

    def rewrite(self) -> str:
        out, k = [], 0
        while k < len(self.toks):
            r = (self.joined_id(k) or self.ticket(k) or self.version_v(k) or self.minus(k) or self.time(k)
                 or self.number(k))
            if r is None:
                out.append(self.toks[k])
                k += 1
            else:
                out.append(r[0])
                k = r[1]
        return " ".join(out)

    def _wrap(self, k: int, text: str, e: int) -> tuple[str, int]:
        """`text` in place of tokens k..e-1, keeping the first one's openers and a following "%"."""
        p = self.percent(e)
        if p:
            text, e = text + "%", p
        return self.items[k][0] + text + self.items[e - 1][2], e

    def joined_id(self, k: int) -> tuple[str, int] | None:
        m = _JOINED_ID.fullmatch(self.items[k][1])
        if not m or m.group(1).islower() or m.group(1).upper() not in self.prefixes:
            return None
        lead, _, trail = self.items[k]
        return f"{lead}{m.group(1).upper()}-{m.group(2)}{trail}", k + 1

    def ticket(self, k: int) -> tuple[str, int] | None:
        name = self.prefix(k)
        if name is None or not (self.nxt(k) or self.items[k][2] == "-"):
            return None
        at = k + 1
        if self.word(at) == "dash" and self.nxt(at):
            at += 1
        num = self.number_at(at, True)
        if num is None or num[2] == "decimal" or self.unit_after(num[1]):     # "NET 30 days" is no ticket
            return None
        return self.items[k][0] + f"{name}-{num[0]}" + self.items[num[1] - 1][2], num[1]

    def version_v(self, k: int) -> tuple[str, int] | None:
        if self.items[k][1] not in ("v", "V") or not self.nxt(k):
            return None
        num = self.number_at(k + 1, True)
        if num is None or num[2] == "token":
            return None
        return self.items[k][0] + self.items[k][1] + num[0] + self.items[num[1] - 1][2], num[1]

    def minus(self, k: int) -> tuple[str, int] | None:
        if self.word(k) not in ("minus", "negative") or not self.nxt(k) or self.is_number(k - 1):
            return None
        num = self.number_at(k + 1, False)
        if num is None:
            return None
        return self._wrap(k, "-" + num[0], num[1])

    def time(self, k: int) -> tuple[str, int] | None:
        """ "at 10.30" / "at ten thirty" -> "at 10:30"; also before a.m./p.m. without "at"."""
        at = self.nxt(k - 1) and self.word(k - 1) == "at"
        m = _TIME_DOT.fullmatch(self.items[k][1])
        if m and int(m.group(1)) <= 23:
            hh, mm, e = m.group(1), m.group(2), k + 1
        elif _small(self.word(k)) is not None and 0 < _small(self.word(k)) <= 12 and self.nxt(k):
            hh, e = str(_small(self.word(k))), k + 1
            if self.word(e) == "oh" and self.nxt(e) and _ONES.get(self.word(e + 1), 0) > 0:
                mm, e = f"0{_ONES[self.word(e + 1)]}", e + 2
            else:
                mins = self.small(e)
                if mins is None or not 10 <= mins[0] <= 59:
                    return None
                mm, e = str(mins[0]), mins[1]
        else:
            return None
        before_am_pm = self.nxt(e - 1) and self.items[e][1] in _AM_PM
        if not (before_am_pm or (at and self.time_follows_ok(e))):
            return None
        return self.items[k][0] + f"{hh}:{mm}" + self.items[e - 1][2], e

    def number(self, k: int) -> tuple[str, int] | None:
        kw = self.keyword_before(k)
        num = self.number_at(k, kw)
        if num is None:
            return None
        text, e, kind = num
        if kind == "token":                                    # already digits: only "25 percent" -> "25%"
            return self._wrap(k, text, e) if self.percent(e) else None
        if kind == "decimal" or (kind == "digits" and len(text) >= 3) or kw or self.unit_after(e):
            return self._wrap(k, text, e)
        return None


def convert(text: str, id_prefixes: Iterable[str] = ()) -> str:
    """`text` with the clearly numeric number words written as digits; anything else is left alone."""
    if not text.strip():
        return text
    out = _Text(text, frozenset(p.upper() for p in id_prefixes)).rewrite()
    return re.sub(r"(?<=\d) +%", "%", out)


def may_continue(text: str, id_prefixes: Iterable[str] = ()) -> bool:
    """True when the last word of `text` could still become part of a number with the next word: "twenty"
    before "five", "two" before "point", "25" before "percent", "minus", "v", a ticket prefix ("SEC").
    Live streaming holds such a word back instead of typing it and having to take it back."""
    toks = text.split()
    if not toks:
        return False
    _, core, trail = _split(toks[-1])
    if trail and trail != "-":
        return False
    w = core.lower()
    if is_number_word(w) or _DIGITS.fullmatch(core) or w in ("minus", "negative") or core in ("v", "V"):
        return True
    prefixes = {p.upper() for p in id_prefixes}
    if not core.islower() and core.upper() in prefixes:
        return True
    if len(toks) < 2:
        return False
    _, pcore, ptrail = _split(toks[-2])
    if ptrail:
        return False
    if w in ("point", "oh", "to", "or", "and", "per") and (is_number_word(pcore) or _DIGITS.fullmatch(pcore)):
        return True
    return w == "dash" and not pcore.islower() and pcore.upper() in prefixes
