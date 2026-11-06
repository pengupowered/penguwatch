"""The API is one router module per domain; api/router.py only mounts them."""

import ast
from pathlib import Path

from penguwatch import api
from penguwatch.api import ROUTERS, router as router_module
from penguwatch.main import app

API_DIR = Path(api.__file__).parent


def _defines_router(path: Path) -> bool:
    """True for a module with a top-level `router = APIRouter(...)`."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "router" for t in node.targets)
                and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "APIRouter"):
            return True
    return False


def test_every_module_with_a_router_is_listed_in_ROUTERS_and_only_once():
    with_router = sorted(p.stem for p in API_DIR.glob("*.py") if p.stem != "router" and _defines_router(p))
    by_id = {id(getattr(api, name).router): name for name in with_router}
    assert sorted(by_id.get(id(r), "?") for r in ROUTERS) == with_router, "a router module is missing from api.ROUTERS"


def test_api_router_py_holds_no_route_handlers_only_includes():
    tree = ast.parse(Path(router_module.__file__).read_text(encoding="utf-8"))
    handlers = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert handlers == [], f"route handlers belong in their domain's module, not api/router.py: {handlers}"
    decorated = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                 and n.attr in {"get", "post", "put", "delete", "patch", "api_route"}]
    assert decorated == []


def test_the_app_serves_every_listed_router_under_api():
    paths = set(app.openapi()["paths"])
    for feature_router in ROUTERS:
        documented = {f"/api{route.path}" for route in feature_router.routes if route.include_in_schema}
        assert documented and documented <= paths
