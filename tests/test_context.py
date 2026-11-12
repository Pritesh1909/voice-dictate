import json
import os

import pytest

from parakeet_dictate import context
from parakeet_dictate.context import ContextScanner, dictionary_entries, harvest, rank, title_terms, tokens
from parakeet_dictate.dictionary import build


def test_tokens_keep_only_multi_word_shapes_and_acronyms():
    text = ("Docker and Spring run McpAuthFilter on ss-core-service with V2, TX-RAMP, ALB, EC2, OAuth, "
            "e.g. the 2026-11-12 build 54d1f82 at 100ms, Notes")
    assert list(tokens(text)) == ["McpAuthFilter", "ss-core-service", "V2", "TX-RAMP", "ALB", "EC2", "OAuth"]


def test_tokens_skip_urls_paths_and_emails_and_split_file_names():
    text = "see https://example.com/KeyCloak D:\\work\\SecretPath a/b/CamelCase x@corp.com McpAuthFilter.java"
    assert list(tokens(text)) == ["McpAuthFilter.java", "McpAuthFilter"]


def test_title_terms():
    assert title_terms("McpAuthFilter.java - ss-core-service - IntelliJ IDEA", "idea64.exe") == \
        ["McpAuthFilter.java", "McpAuthFilter", "ss-core-service", "IntelliJ", "idea64"]
    assert title_terms("Inbox - Outlook", "OUTLOOK.EXE") == []   # "OUTLOOK" reads as "out look"
    assert title_terms("Notes - Notepad", "notepad.exe") == []
    assert title_terms("", "") == []


NOTE_A = """---
tags: [security]
---
# SEC-12 resolve-auth hardening

The `McpAuthFilter` calls `resolve-auth` on ss-core-service. Deploy core before CRM behind the ALB.
This is a read-only change and a follow-up to the 24-hour UserName review.
Some ideas: an IDEA is not a term. We use docker and Docker.

```java
class McpAuthFilter { String reallyLongGeneratedIdentifierNameThatIsNoise = "x"; }
```
See [[TX-RAMP plan|the plan]] and ![[Pasted image 20261103.png]] and https://softsages.example/McpLink
"""

NOTE_B = """# Rollout
McpAuthFilter again, TX-RAMP again, CRM again. The read-only flag, read-only mode, follow-up items.
Something in a username field, the user name.
"""


def test_harvest_weights_and_code_evidence():
    n = harvest(NOTE_A, "SEC-12 notes")
    assert "McpAuthFilter" in n.terms and n.terms["McpAuthFilter"][1] >= 2    # code span + code block
    assert "resolve-auth" in n.terms and n.terms["resolve-auth"][1] == 1      # in backticks
    assert "reallyLongGeneratedIdentifierNameThatIsNoise" not in n.terms      # long token in a code block
    assert "McpLink" not in n.terms                                          # inside a URL
    assert "SEC-12" in n.terms and n.terms["SEC-12"][0] >= 2 * context.W_HEADING   # title + heading
    assert "TX-RAMP" in n.terms
    assert not any("Pasted" in t for t in n.terms)


def test_rank_safety_rule_never_recases_ordinary_speech():
    terms = rank([harvest(NOTE_A, "a"), harvest(NOTE_B, "b")], 50)
    for wanted in ("McpAuthFilter", "TX-RAMP", "ss-core-service", "CRM", "ALB", "SEC-12", "resolve-auth"):
        assert wanted in terms
    for unwanted in ("IDEA", "Docker", "read-only", "follow-up", "24-hour", "UserName"):
        assert unwanted not in terms
    assert terms[0] in ("McpAuthFilter", "TX-RAMP", "CRM")    # in both notes: ranked first
    spell = build(dictionary_entries(terms))
    assert spell("the mcp auth filter calls resolve auth behind the alb") == \
        "the McpAuthFilter calls resolve-auth behind the ALB"
    assert spell("a read only idea for the user name") == "a read only idea for the user name"


def test_rank_picks_the_most_used_spelling_and_caps():
    notes = [harvest("KeyCloakAdmin KeyCloakAdmin KeycloakAdmin"), harvest("KeyCloakAdmin")]
    assert rank(notes, 10) == ["KeyCloakAdmin"]
    assert rank([harvest("AlphaOne BetaTwo GammaThree")], 2) == ["AlphaOne", "BetaTwo"]


def test_dictionary_entries_first_source_wins():
    assert dictionary_entries(["McpAuthFilter"], ["MCPAuthFilter", "TX-RAMP"]) == \
        [("McpAuthFilter", ""), ("TX-RAMP", "")]


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / ".obsidian").mkdir(parents=True)
    (v / ".trash").mkdir()
    (v / "sub").mkdir()
    (v / "one.md").write_text(NOTE_A, "utf-8")
    (v / "sub" / "two.md").write_text(NOTE_B, "utf-8")
    (v / ".obsidian" / "skip.md").write_text("HiddenTerm HiddenTerm", "utf-8")
    (v / ".trash" / "old.md").write_text("TrashTerm", "utf-8")
    (v / "image.png").write_bytes(b"\x89PNG PngTerm")
    return v


def test_scanner_reads_md_only_caches_and_rescans_changed(vault, tmp_path):
    cache = tmp_path / "cache" / "context_terms.json"
    clock = [0.0]
    s = ContextScanner(cache, [str(vault)], max_terms=50, refresh_minutes=10, clock=lambda: clock[0])
    assert s.terms == [] and s.due()
    st = s.scan()
    assert (st.files, st.rescanned) == (2, 2)
    assert "McpAuthFilter" in s.terms
    assert not {"HiddenTerm", "TrashTerm", "PngTerm"} & set(s.terms)
    assert not s.due()
    clock[0] = 601
    assert s.due()

    raw = cache.read_text("utf-8")
    assert "Deploy core before" not in raw and "username field" not in raw   # no note text in the cache
    assert json.loads(raw)["terms"] == s.terms

    again = ContextScanner(cache, [str(vault)], max_terms=50)
    assert again.terms == s.terms                     # ready at once from the cache
    (vault / "sub" / "two.md").write_text(NOTE_B + "\nBrandNewThing here.\n", "utf-8")
    st2 = again.scan()
    assert (st2.files, st2.rescanned) == (2, 1)
    assert "BrandNewThing" in again.terms
    os.remove(vault / "one.md")
    again.scan()
    assert "resolve-auth" not in again.terms


def test_scanner_background_refresh_and_settings_change(vault, tmp_path):
    s = ContextScanner(tmp_path / "c.json", [str(vault)], max_terms=50)
    assert s.refresh()
    s.wait(10)
    assert "TX-RAMP" in s.terms
    assert not s.refresh()                            # not due again yet
    s.configure([str(vault)], 1, 10)
    assert s.due()
    assert s.refresh(force=False)
    s.wait(10)
    assert len(s.terms) == 1


def test_scanner_without_folders_does_nothing(tmp_path):
    s = ContextScanner(tmp_path / "c.json", [])
    assert not s.due() and not s.refresh()
    s2 = ContextScanner(tmp_path / "c2.json", [str(tmp_path / "missing")])
    assert s2.scan().files == 0 and s2.terms == []


def test_is_ordinary_uses_prose_words(vault, tmp_path):
    s = ContextScanner(tmp_path / "c.json", [str(vault)])
    s.scan()
    assert s.is_ordinary("username") and s.is_ordinary("idea") and not s.is_ordinary("mcpauthfilter")
    # "username" is written as a plain word in the notes, so the window's "UserName" must not respell it.
    assert title_terms("UserName.java - IDEA", is_ordinary=s.is_ordinary) == ["UserName.java"]
