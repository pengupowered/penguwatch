"""textsearch.normalize and the search text built from it."""

import pytest

from penguwatch.textsearch import (
    build_search_text, clean_alt_titles, clean_en_title, compact, normalize, strip_accents, MAX_ALT_TITLES,
)


@pytest.mark.parametrize("text", ["Shōgun", "shogun", "SHOGUN", "Shougun", "shoogun", "Shōgun!"])
def test_shogun_spellings_meet(text):
    assert normalize(text) == "shogun"


@pytest.mark.parametrize("a,b", [
    ("Pokémon", "pokemon"), ("Ørsted", "orsted"), ("Æon Flux", "aeon flux"), ("Straße", "strasse"),
    ("Łódź", "lodz"), ("Đakovo", "dakovo"), ("Yūki Yūna", "yuki yuna"), ("Yuuki Yuuna", "yuki yuna"),
    ("Tōkyō", "tokyo"), ("Toukyou", "tokyo"), ("Ōoku", "oku"), ("Aa! Megami-sama", "a megami sama"),
    ("Sennin Ee", "sennin e"), ("Kiiroi", "kiroi"),
])
def test_accents_special_letters_and_long_vowels(a, b):
    assert normalize(a) == b


def test_oh_is_not_folded():
    assert normalize("John") == "john"
    assert normalize("Oh My Goddess") == "oh my goddess"


def test_punctuation_and_spacing():
    assert normalize("Re:Zero") == "re zero"
    assert normalize("  Frieren:  Beyond Journey's End ") == "frieren beyond jorney s end"
    assert compact("Re:Zero") == "rezero"
    assert compact("Re Zero") == "rezero"


def test_empty_and_non_latin_text():
    assert normalize(None) == "" and normalize("  ") == "" and normalize("!!!") == ""
    assert normalize("葬送のフリーレン") == "葬送のフリーレン"
    assert normalize("がっこう") == "がっこう"  # kana keep their dakuten


def test_search_text_has_fields_and_a_compact_copy():
    text = build_search_text("Re:Zero", "Re:Zero - Starting Life", ["Re Zero kara"], "Crunchyroll", ["Anime", "Fantasy"])
    spaced, packed = text.split("\n\n")
    assert spaced.split("\n") == ["re zero", "re zero starting life", "re zero kara", "crunchyroll", "anime", "fantasy"]
    assert "rezerostartinglife" in packed
    assert "rezero" in packed.split("\n")


def test_clean_en_title_drops_restatements():
    assert clean_en_title("Shogun", "Shōgun") is None
    assert clean_en_title("Sousou no Frieren", "") is None
    assert clean_en_title("Sousou no Frieren", None) is None
    assert clean_en_title("Sousou no Frieren", " Frieren: Beyond Journey's End ") == "Frieren: Beyond Journey's End"


def test_clean_alt_titles_names_deduplicated_and_capped():
    raw = [{"name": "A", "lang": 1, "type": "official"}, {"name": "A"}, "B", {"name": ""}, {"lang": 2}, 7, {"name": " C "}]
    assert clean_alt_titles(raw) == ["A", "B", "C"]
    assert clean_alt_titles([{"name": f"T{i}"} for i in range(40)]) == [f"T{i}" for i in range(MAX_ALT_TITLES)]
    assert clean_alt_titles(None) == [] and clean_alt_titles("x") == []


def test_strip_accents_keeps_case_and_punctuation():
    assert strip_accents("Shōgun: Ørsted & Æther") == "Shogun: Orsted & Aether"
    assert strip_accents("ŁÓDŹ") == "LODZ"
    assert strip_accents("Plain") == "Plain" and strip_accents("葬送") == "葬送"
