"""Validation messages in the page's words (settings/plain.py)."""

from penguwatch.settings import registry
from penguwatch.settings.plain import plain


def _spec(leaf: str):
    return next(s for s in registry.SETTINGS if s.key.endswith("." + leaf))


def test_a_full_key_in_a_message_becomes_its_label_with_no_section_left_in_front():
    # the driver's cross-field check names the first setting by its full key, the second by its leaf
    idle, auto = _spec("idle_sync_interval_minutes"), _spec("auto_sync_interval_minutes")
    section = idle.key.split(".", 1)[0]
    text = plain(f"{idle.key} must not be below {auto.key.rsplit('.', 1)[-1]}", section)
    assert text == f"“{idle.label}” must not be below “{auto.label}”"
    assert section + "." not in text.lower()


def test_an_unknown_dotted_word_is_left_alone():
    assert plain("see example.com for details") == "See example.com for details"
