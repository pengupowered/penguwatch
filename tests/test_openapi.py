"""The OpenAPI schema is the contract: every route is tagged and summarised, and the documented stable routes exist
and are written down in docs/api.md."""

import re
from pathlib import Path

import pytest

from penguwatch.api.stable import STABLE, TAGS
from penguwatch.main import app

DOC = Path(__file__).resolve().parent.parent / "docs" / "api.md"


@pytest.fixture(scope="module")
def schema():
    return app.openapi()


def operations(schema):
    return [(m.upper(), p, op) for p, ops in schema["paths"].items() for m, op in ops.items()]


def test_every_route_has_a_tag_and_a_written_summary(schema):
    from fastapi.routing import APIRoute
    from penguwatch.api import ROUTERS
    routes = [r for router in ROUTERS for r in router.routes if isinstance(r, APIRoute) and r.include_in_schema]
    assert len(routes) > 60
    # FastAPI would make a summary from the function name ("Get Stats") when none is written
    assert [f"{sorted(r.methods)} {r.path}" for r in routes if not r.summary] == []
    assert [f"{sorted(r.methods)} {r.path}" for r in routes if not r.tags] == []
    assert [r.summary for r in routes if r.summary == r.name.replace("_", " ").title()] == []


def test_every_tag_used_is_described(schema):
    described = {t["name"] for t in TAGS}
    used = {t for _, _, op in operations(schema) for t in op.get("tags", [])}
    assert used <= described, used - described
    assert {t["name"] for t in schema["tags"]} == described


@pytest.mark.parametrize("method,path", STABLE)
def test_documented_stable_routes_exist(schema, method, path):
    assert method.lower() in schema["paths"].get(path, {}), f"{method} {path} is documented as stable but is not routed"
    assert schema["paths"][path][method.lower()].get("x-stable") is True


def test_every_stable_route_is_in_the_api_doc():
    text = DOC.read_text(encoding="utf-8")
    missing = [f"{m} {p}" for m, p in STABLE if not re.search(rf"`{m}`\s*\|?\s*`{re.escape(p)}`|`{m} {re.escape(p)}`", text)]
    assert missing == []


def test_only_the_stable_routes_are_marked(schema):
    marked = {(m, p) for m, p, op in operations(schema) if op.get("x-stable")}
    assert marked == set(STABLE)


def test_the_dashboard_page_is_not_part_of_the_api_schema(schema):
    assert "/" not in schema["paths"]
