import pytest

from parakeet_dictate.textclean import Cleaner, filler_regex, remove_fillers, remove_repeats

FILLERS = ["uh", "um", "uhm", "ah", "er", "erm", "eh", "hm", "mm"]


@pytest.fixture
def cleaner():
    return Cleaner(
        fillers=FILLERS,
        repeated_words_allowed=["that", "had"],
        replacements=[("key cloak", "Keycloak"), ("mail c", "Mailzzy"), ("code commit", "CodeCommit")],
    )


@pytest.mark.parametrize("raw, expected", [
    # the exact Parakeet v2 output from the TTS probe
    ("Uh, I think we should, um, deploy the core service first, and then the CRM service. "
     "AHH, the key cloak token must be validated.",
     "I think we should deploy the core service first, and then the CRM service. "
     "The Keycloak token must be validated."),
    ("um, deploy it", "Deploy it"),
    ("and then, uh.", "and then."),
    ("Is it ready, uh?", "Is it ready?"),
    ("We need, uhh, the logs. Um, and the metrics.", "We need the logs. And the metrics."),
    ("Uh", ""),
    ("uh um ah", ""),
    ("Hmm, let me check the the build.", "Let me check the build."),
    ("Errr, okay.", "Okay."),
    ("I said uh-huh to that.", "I said uh-huh to that."),       # not a filler
    ("Size M is fine.", "Size M is fine."),                        # "mm" never matches a lone M
    ("He had had enough.", "He had had enough."),                  # allowed repeat
    ("I, I think so.", "I think so."),
    ("Open the mail C campaign on code-commit.", "Open the Mailzzy campaign on CodeCommit."),
    ("An umbrella and a hummingbird.", "An umbrella and a hummingbird."),  # fillers only as whole words
    ("The error was, er, transient.", "The error was transient."),
])
def test_clean(cleaner, raw, expected):
    assert cleaner.clean(raw) == expected


def test_filler_regex_stretching():
    rx = filler_regex(["uh", "mm", "hm"])
    for ok in ("uh", "UHH", "uuhh", "mm", "mmmm", "hm", "hmmm"):
        assert rx.fullmatch(ok), ok
    for bad in ("u", "h", "m", "M", "huh", "uhm"):
        assert not rx.fullmatch(bad), bad


def test_empty_filler_list_is_noop():
    assert remove_fillers("uh hello", filler_regex([])) == "uh hello"


def test_repeats_ignore_sentence_boundary():
    assert remove_repeats("Go. Go now.") == "Go. Go now."


def test_dictionary_runs_after_cleanup():
    c = Cleaner(fillers=FILLERS, dictionary=[("Mailzzy", "mail sey"), ("pnpm", ""), ("Keycloak", "")])
    assert c.clean("Um, pnpm install, then the mail-sey key cloak setup.") == \
        "pnpm install, then the Mailzzy Keycloak setup."


def test_default_settings_fix_the_old_replacements():
    from parakeet_dictate import config
    c = Cleaner.from_config(config.parse(""))
    assert c.clean("Open key cloak, ink free and the mailsey app on code commit with fly way.") == \
        "Open Keycloak, Inkfree and the Mailzzy app on CodeCommit with Flyway."
