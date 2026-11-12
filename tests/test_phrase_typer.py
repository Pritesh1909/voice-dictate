from parakeet_dictate.output import PhraseTyper


def make(trailing=True):
    sent, held = [], [False]
    t = PhraseTyper(lambda raw: raw.replace("um ", ""), "type", trailing,
                    send=lambda text, method: sent.append(text), mods=lambda: held[0])
    return t, sent, held


def test_types_each_phrase_as_it_comes():
    t, sent, _ = make()
    t.add("Deploy core first.")
    t.pump()
    t.add("um Then CRM.")
    t.pump()
    assert sent == ["Deploy core first. ", "Then CRM. "]
    assert t.text == "Deploy core first. Then CRM."


def test_without_trailing_space_phrases_are_still_separated():
    t, sent, _ = make(trailing=False)
    for p in ("One.", "Two."):
        t.add(p)
        t.pump()
    assert "".join(sent) == "One. Two."


def test_waits_while_a_modifier_is_held_then_sends_together():
    t, sent, held = make()
    held[0] = True
    t.add("One.")
    t.pump()
    t.add("Two.")
    t.pump()
    assert sent == [] and t.pending
    held[0] = False
    t.pump()
    assert sent == ["One. Two. "] and not t.pending


def test_end_of_dictation_does_not_skip_on_held_keys():
    t, sent, held = make()
    held[0] = True
    t.add("Last.")
    t.pump(wait=True)          # deliver() itself waits up to 2 s for the keys
    assert sent == ["Last. "]


def test_cancel_keeps_typed_and_drops_the_rest():
    t, sent, held = make()
    t.add("Kept.")
    t.pump()
    held[0] = True
    t.add("Dropped.")
    t.cancel()
    held[0] = False
    t.pump(wait=True)
    assert sent == ["Kept. "] and t.text == "Kept." and not t.pending


def test_filler_only_phrase_types_nothing():
    t, sent, _ = make()
    t.add("um ")
    t.pump()
    assert sent == [] and t.text == ""


def test_typing_failure_keeps_what_was_typed_and_collects_the_rest():
    sent = []

    def send(text, method):
        if len(sent) == 1:
            raise OSError("SendInput blocked")
        sent.append(text)

    t = PhraseTyper(lambda raw: raw, "type", True, send=send, mods=lambda: False)
    for p in ("One.", "Two.", "Three."):
        t.add(p)
        t.pump()
    t.pump(wait=True)
    assert sent == ["One. "] and t.text == "One."
    assert t.untyped == "Two. Three." and isinstance(t.failed, OSError) and not t.pending


def test_no_failure_leaves_nothing_untyped():
    t, sent, _ = make()
    t.add("One.")
    t.pump(wait=True)
    assert t.failed is None and t.untyped == ""
