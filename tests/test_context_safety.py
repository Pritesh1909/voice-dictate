"""The knowledge-base safety rule: harvested terms must never rewrite ordinary speech.

The dictionary respells a run of spoken words whose joined key equals a term's key, so a term whose key
reads as ordinary words ("IAM" = "i am", "userId" = "user id") would rewrite them. Those are dropped
(acronyms) or used only in code programs ([context] code_apps)."""
from types import SimpleNamespace as NS

import pytest

from parakeet_dictate import config, winutil
from parakeet_dictate.app import App
from parakeet_dictate.context import (CODE_ONLY, DROP, EVERYWHERE, ContextScanner, classify, harvest,
                                      rank, splits_into_words, title_terms)
from parakeet_dictate.textclean import Cleaner

from parakeet_dictate.context import _common as common  # noqa: E402


@pytest.mark.parametrize("key, splits", [
    ("iam", True), ("userid", True), ("displayname", True), ("defaultroleid", True), ("outlook", True),
    ("sscoreservice", False), ("mcpauthfilter", False), ("useast1", False), ("sha256", False),
    ("user", False),          # one word on its own is not a split (plain words are handled elsewhere)
    ("", False),
])
def test_splits_into_words(key, splits):
    assert splits_into_words(key, common) is splits


@pytest.mark.parametrize("term, cls", [
    ("IAM", DROP), ("OUTLOOK", DROP),
    ("userId", CODE_ONLY), ("displayName", CODE_ONLY), ("defaultRoleId", CODE_ONLY), ("readOnly", CODE_ONLY),
    ("ss-core-service", EVERYWHERE), ("Saml2LoginSuccessHandler", EVERYWHERE), ("McpAuthFilter", EVERYWHERE),
    ("us-east-1", EVERYWHERE), ("SHA-256", EVERYWHERE), ("MFA", EVERYWHERE), ("ALB", EVERYWHERE),
    ("cross-platform", CODE_ONLY), ("customer-facing", CODE_ONLY), ("per-tenant", CODE_ONLY),   # English
    ("erp-uat", EVERYWHERE), ("ss-esign-web", EVERYWHERE),                                     # abbreviations
])
def test_classify(term, cls):
    assert classify(term, common) == cls


NOTES = """# Access
IAM roles: IAM IAM IAM, and the `userId` and `displayName` fields of `ss-core-service`.
`userId` again, ss-core-service again. I am going to check it.
"""


@pytest.fixture
def scanner(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "access.md").write_text(NOTES, "utf-8")
    (vault / "more.md").write_text("ss-core-service and `userId` and IAM here too.\n", "utf-8")
    s = ContextScanner(tmp_path / "cache.json", [str(vault)], max_terms=50)
    s.scan()
    return s


def test_iam_is_harvested_but_dropped(scanner):
    assert "IAM" in harvest(NOTES).terms                 # it is in the notes ...
    assert "IAM" not in scanner.terms                     # ... but never used
    assert "IAM" not in rank([harvest(NOTES)], 50)
    assert {"ss-core-service", "userId", "displayName"} <= set(scanner.terms)
    assert scanner.code_only == {"userId", "displayName"}
    assert set(scanner.everywhere) == set(scanner.terms) - {"userId", "displayName"}


def _ns(scanner, cfg_text=""):
    return NS(cfg=config.parse(cfg_text), context=scanner)


def _clean(scanner, exe, text, title="", monkeypatch=None):
    if monkeypatch:
        monkeypatch.setattr(winutil, "window_title", lambda hwnd: title)
    entries = App._context_entries(_ns(scanner), exe, 1 if title else 0)
    return Cleaner.from_config(config.parse("")).with_extra(entries).clean(text)


def test_i_am_going_is_never_rewritten(scanner):
    for exe in ("outlook.exe", "idea64.exe", ""):
        assert _clean(scanner, exe, "I am going to check the IAM policy.") == "I am going to check the IAM policy."


def test_code_only_terms_only_in_code_apps(scanner):
    assert _clean(scanner, "OUTLOOK.EXE", "Fill in the user id field.") == "Fill in the user id field."
    assert _clean(scanner, "idea64.exe", "Fill in the user id field.") == "Fill in the userId field."
    assert _clean(scanner, r"C:\Program Files\JetBrains\bin\IDEA64.EXE", "the display name") == "the displayName"


def test_names_with_an_unusual_part_apply_everywhere(scanner):
    for exe in ("outlook.exe", "idea64.exe", "notepad.exe", ""):
        assert _clean(scanner, exe, "Deploy ss core service now.") == "Deploy ss-core-service now."


def test_window_title_terms_follow_the_same_rule(scanner, monkeypatch):
    title = "UserRoleId.java - McpAuthFilter - IAM Console"
    # "java" is no ordinary word, so the whole file name is safe everywhere; "UserRoleId" alone is code-only.
    assert title_terms(title, "outlook.exe", scanner.is_ordinary, code_app=False) == \
        ["UserRoleId.java", "McpAuthFilter"]
    assert title_terms(title, "idea64.exe", scanner.is_ordinary, code_app=True) == \
        ["UserRoleId.java", "UserRoleId", "McpAuthFilter", "idea64"]
    assert _clean(scanner, "outlook.exe", "the user role id and the mcp auth filter", title, monkeypatch) == \
        "the user role id and the McpAuthFilter"
    assert _clean(scanner, "idea64.exe", "the user role id and the mcp auth filter", title, monkeypatch) == \
        "the UserRoleId and the McpAuthFilter"


def test_context_off_gives_nothing(scanner):
    ns = _ns(scanner, "[context]\nenabled = false")
    assert App._context_entries(ns, "idea64.exe", 0) == []


def test_code_apps_setting():
    c = config.parse('[context]\ncode_apps = ["Notepad", "C:\\\\Tools\\\\Vim.EXE"]')
    assert c.context_code_apps == ("notepad.exe", "vim.exe")
    assert "idea64.exe" in config.parse("").context_code_apps


def test_cache_keeps_the_classes(scanner, tmp_path):
    again = ContextScanner(tmp_path / "cache.json", scanner.folders, max_terms=50)
    assert again.terms == scanner.terms and again.code_only == scanner.code_only
