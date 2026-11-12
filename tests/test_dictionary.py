import pytest

from parakeet_dictate.dictionary import build, normalize_terms, word_key

TERMS = [("Keycloak", ""), ("Inkfree", ""), ("SoftSages", ""), ("CodeCommit", ""), ("useAudioCapture", ""),
         ("Mailzzy", "mail sey"), ("Mailzzy", "mailc"), (".NET", ""), ("C#", ""), ("stt-worker.ts", "")]


@pytest.fixture
def spell():
    return build(TERMS)


@pytest.mark.parametrize("raw, expected", [
    ("The key cloak token.", "The Keycloak token."),
    ("Open KEYCLOAK and Key-Cloak.", "Open Keycloak and Keycloak."),
    ("Send the mail sey campaign.", "Send the Mailzzy campaign."),
    ("Send the mail-sey campaign, then MailC.", "Send the Mailzzy campaign, then Mailzzy."),
    ("Push it to code commit.", "Push it to CodeCommit."),
    ("Call use audio capture here.", "Call useAudioCapture here."),
    ("It runs on .net and C# too.", "It runs on .NET and C# too."),
    ("Written in c# today.", "Written in C# today."),
    ("Edit stt worker ts now.", "Edit stt-worker.ts now."),
    ("Soft sages, ink free.", "SoftSages, Inkfree."),
    ("No terms here at all.", "No terms here at all."),
    ("keycloaks are different", "keycloaks are different"),    # whole words only
    ("key, cloak", "key, cloak"),                              # a comma breaks a term apart
])
def test_respells(spell, raw, expected):
    assert spell(raw) == expected


def test_heard_as_beats_another_term_with_the_same_key():
    spell = build([("Male C", ""), ("Mailzzy", "male c")])
    assert spell("a male c thing") == "a Mailzzy thing"


def test_terms_ahead_win_over_all_others():
    personal = [("Mailzzy", "mail c"), ("Keycloak", "")]
    assert build(personal)("the mail c file") == "the Mailzzy file"
    spell = build(personal, ahead=[("Mail C", ""), ("KeyCloak", "")])
    assert spell("the mail c file in key cloak") == "the Mail C file in KeyCloak"
    assert build([], ahead=[("pnpm", "")])("run p n p m") == "run pnpm"


def test_longest_run_wins():
    spell = build([("Spring", ""), ("Spring Boot", "")])
    assert spell("a spring boot app in spring") == "a Spring Boot app in Spring"


def test_none_without_terms():
    assert build([]) is None
    assert build([("  ", "x")]) is None


def test_normalize_caps_and_dedupes():
    assert normalize_terms([(" A ", ""), ("A", ""), ("A", "a")]) == [("A", ""), ("A", "a")]
    assert len(normalize_terms((f"t{i}", "") for i in range(600))) == 500


def test_word_key():
    assert word_key("Use-Audio Capture") == word_key("useAudioCapture") == "useaudiocapture"
