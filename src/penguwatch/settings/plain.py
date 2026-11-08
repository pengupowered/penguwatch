"""Validation messages in plain words, for the Settings tab and its import notes. The checks are written for the code
that raises them ("Input should be less than or equal to 23", "household.members: each name needs ..."); a person
looking at the page knows the settings by their labels and numbers by "at most"."""

import re
from collections import Counter
from typing import Dict, Optional

_NUMBER = (
    (re.compile(r"Input should be greater than or equal to (\S+)"), r"Enter at least \1"),
    (re.compile(r"Input should be less than or equal to (\S+)"), r"Enter at most \1"),
    (re.compile(r"Input should be greater than (\S+)"), r"Enter more than \1"),
    (re.compile(r"Input should be less than (\S+)"), r"Enter less than \1"),
    (re.compile(r"Input should be a valid (number|integer)[^.]*"), r"Enter a \1"),
    (re.compile(r"List should have at least (\d+) items? after validation, not \d+"), r"Enter at least \1"),
    (re.compile(r"List should have at most (\d+) items? after validation, not \d+"), r"Enter at most \1"),
    (re.compile(r"String should have at least (\d+) characters?"), r"Enter at least \1 characters"),
    (re.compile(r"String should have at most (\d+) characters?"), r"Enter at most \1 characters"),
)
_SECTION = re.compile(r"^(?:[a-z_]+\.)?[a-z_]+: (?=\S)")  # "household.members: each name ..." -> "each name ..."
_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_KEY = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\b")  # a full key ("optimizer.max_active_services") in a message
_labels: Optional[Dict[str, str]] = None


def _leaf_labels() -> Dict[str, str]:
    """Last part of a setting's key -> its label, for the names that only one setting has."""
    global _labels
    if _labels is None:
        from penguwatch.settings import registry
        leaf = Counter(s.key.rsplit(".", 1)[-1] for s in registry.SETTINGS)
        _labels = {s.key.rsplit(".", 1)[-1]: s.label for s in registry.SETTINGS if leaf[s.key.rsplit(".", 1)[-1]] == 1}
    return _labels


def plain(text: str, scope: str = "") -> str:
    """`text` with the code's words swapped for the page's: numbers as "Enter at most 23", a setting's name as its
    label. `scope` is the config section the message comes from ("notify"): a field name two settings share is the
    one under that section, by its full key."""
    for pattern, repl in _NUMBER:
        text = pattern.sub(repl, text)
    text = _SECTION.sub("", text, count=1)
    labels = _leaf_labels()
    from penguwatch.settings import registry

    def label(m):
        name = m.group(0)
        spec = registry.BY_KEY.get(f"{scope}.{name}") if scope else None
        found = spec.label if spec else labels.get(name)
        return f"“{found}”" if found else name

    def key_label(m):
        spec = registry.BY_KEY.get(m.group(0))
        return f"“{spec.label}”" if spec else m.group(0)

    text = _KEY.sub(key_label, text)  # full keys first, so "section." is not left in front of a label
    text = _NAME.sub(label, text)
    return text[:1].upper() + text[1:] if text else text
