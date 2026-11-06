"""Forgiving text matching: one normal form for titles and queries.

`normalize` ignores case, accents and punctuation and folds Japanese long vowels, so "Shogun", "Shougun",
"Shoogun" and "Shōgun" all become "shogun". It is applied to both sides of a search. `build_search_text`
makes the stored haystack of a title: every searchable field normalized, plus a copy with the spaces removed
so that "rezero" finds "Re:Zero".
"""

import html
import re
import unicodedata
from typing import Iterable, List, Optional

# Letters Unicode does not decompose into a base letter plus a mark.
_EXTRA = {"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"}
_NOT_ALNUM = re.compile(r"[\W_]+")
# Long vowels as romanised in different systems: ou/oo/ō -> o, uu/ū -> u, aa/ā -> a, ii/ī -> i, ee/ē -> e.
# "oh" is deliberately left alone (John).
_LONG_VOWELS = ((re.compile(r"o[ou]+"), "o"), (re.compile(r"u{2,}"), "u"), (re.compile(r"a{2,}"), "a"),
                (re.compile(r"i{2,}"), "i"), (re.compile(r"e{2,}"), "e"))
FIELD_SEPARATOR = "\n"
COPY_SEPARATOR = "\n\n"
MAX_ALT_TITLES = 20


def _strip_marks(text: str) -> str:
    """Drops the accents of Latin letters; other scripts (kana with dakuten, ...) keep their marks."""
    out: List[str] = []
    for ch in text:
        if ch.isascii():
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        if decomposed[0].isascii() or "LATIN" in unicodedata.name(decomposed[0], ""):
            out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
        else:
            out.append(unicodedata.normalize("NFKC", ch))
    return "".join(out)


def normalize(text: Optional[str]) -> str:
    """Lower-case, accent-free, punctuation-free, long-vowel-folded form of `text` (words joined by one space)."""
    if not text:
        return ""
    folded = "".join(_EXTRA.get(c, c) for c in _strip_marks(text).casefold())
    folded = _NOT_ALNUM.sub(" ", folded)
    for pattern, repl in _LONG_VOWELS:
        folded = pattern.sub(repl, folded)
    return folded.strip()


def compact(text: Optional[str]) -> str:
    """`normalize` with the spaces removed ("Re:Zero" -> "rezero")."""
    return normalize(text).replace(" ", "")


def build_search_text(title: Optional[str], en_title: Optional[str], alt_titles: Iterable[str],
                      network: Optional[str], genres: Iterable[str]) -> str:
    """The haystack stored with a title: normalized fields (one per line), a blank line, then the same fields
    with their spaces removed. A query matches inside one field, never across two."""
    fields = [normalize(f) for f in (title, en_title, *alt_titles, network, *genres)]
    fields = [f for f in fields if f]
    return FIELD_SEPARATOR.join(fields) + COPY_SEPARATOR + FIELD_SEPARATOR.join(f.replace(" ", "") for f in fields)


def clean_en_title(title: Optional[str], en_title: Optional[str]) -> Optional[str]:
    """The English title to store: None when absent or when it is the title under another spelling. HTML
    entities catalogues sometimes leave in ("World&#039;s") are decoded."""
    en_title = html.unescape(en_title or "").strip()
    if not en_title or normalize(en_title) == normalize(title):
        return None
    return en_title


def clean_alt_titles(raw: object) -> List[str]:
    """Names from a catalogue's `alt_titles` list ([{"name": ..., "lang": 1, "type": "official"}] or plain strings),
    de-duplicated in order, at most MAX_ALT_TITLES."""
    names: List[str] = []
    for item in raw if isinstance(raw, list) else []:
        name = item.get("name") if isinstance(item, dict) else item
        name = html.unescape(name).strip() if isinstance(name, str) else ""
        if name and name not in names:
            names.append(name)
    return names[:MAX_ALT_TITLES]


def strip_accents(text: str) -> str:
    """`text` with Latin accents and special letters replaced by plain ones, case and punctuation kept
    ("Shōgun" -> "Shogun"); for services that only know the plain spelling."""
    plain = _strip_marks(text)
    return "".join(
        (_EXTRA[c.lower()].capitalize() if c.isupper() else _EXTRA[c]) if c.lower() in _EXTRA else c for c in plain
    )
