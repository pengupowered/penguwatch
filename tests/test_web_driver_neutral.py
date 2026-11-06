"""The page knows no service: nothing under web/ names a driver's service. The label, the account's controls, the
links to a title's page and the rating sources all come from /api/drivers (js/drivers.js), so a new driver needs no
change to the page. (The Python side has the same rule: tests/test_driver_layer.py.)"""

import re
from pathlib import Path

from penguwatch.drivers import DRIVERS

WEB = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web"
TEXT = {".js", ".html", ".css", ".svg", ".json", ".webmanifest", ".txt"}
# Services drivers exist or are planned for, besides every registered driver (the local driver names none)
KNOWN = {"simkl", "tvmaze", "anilist", "trakt"}
# Files that may name one, and why (none today)
ALLOWED: dict = {}


def service_names():
    return KNOWN | {name for name in DRIVERS if name != "local"}


def mentions(text: str, names=None):
    names = names or service_names()
    pattern = re.compile("|".join(re.escape(n) for n in sorted(names)), re.I)
    return [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


def test_no_file_under_web_names_a_service():
    offenders = []
    for path in sorted(WEB.rglob("*")):
        rel = path.relative_to(WEB).as_posix()
        if not path.is_file() or path.suffix not in TEXT or rel in ALLOWED:
            continue
        offenders += [f"{rel}:{n}: {line[:120]}" for n, line in mentions(path.read_text(encoding="utf-8"))]
    assert offenders == []


def test_the_guard_finds_a_service_in_any_case_and_every_registered_driver_is_covered():
    assert mentions("label = 'Sent to SIMKL'") and mentions('<a href="https://tvmaze.com">') and mentions("AniList")
    assert mentions("nothing here") == []
    assert {n for n in DRIVERS if n != "local"} <= service_names()
    assert any(p.suffix == ".js" for p in WEB.rglob("*") if p.is_file())  # the walk finds files


def test_the_allowed_files_exist_and_say_why():
    for rel, reason in ALLOWED.items():
        assert (WEB / rel).is_file() and reason
