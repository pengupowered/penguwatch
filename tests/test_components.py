"""The component list (docs/architecture.md): adding a part edits only main.py's COMPONENTS, a failure at start-up of an
optional part costs only that part, and shutdown keeps its order (every task ends first, the outbox is flushed before
the SIMKL client closes, the database closes after both, the telemetry flush comes last)."""

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from penguwatch import main
from penguwatch.components import Component, Context, run_components

SRC = Path(__file__).resolve().parent.parent / "src" / "penguwatch"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)


def _defines_register(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.AsyncFunctionDef) and n.name == "register" for n in tree.body)


def _runtime_imports(tree: ast.Module):
    """(line, module) of every import outside an `if TYPE_CHECKING:` block; `from a import b` yields a and a.b."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
            skip |= {id(n) for n in ast.walk(node)}
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name


SOURCES = {_module_name(p): ast.parse(p.read_text(encoding="utf-8"))
           for p in sorted(SRC.rglob("*.py")) if "homeguard" not in p.parts}
LISTED = [m.__name__ for m in main.COMPONENTS]


# --- guards: adding a component edits only main.py's list --------------------------------------------------------

def test_every_module_with_a_register_hook_is_in_the_component_list_and_only_once():
    with_register = sorted(name for name, tree in SOURCES.items() if _defines_register(tree))
    assert sorted(LISTED) == with_register, "a module with register(ctx) is a component: list it in main.COMPONENTS"
    assert len(set(LISTED)) == len(LISTED)


def test_no_module_but_main_imports_a_component_module():
    """So the list in main.py is the one place that names a component."""
    offenders = [f"{name}:{line} imports {imported}" for name, tree in SOURCES.items() if name != "penguwatch.main"
                 for line, imported in _runtime_imports(tree) if imported in LISTED]
    assert offenders == []


def test_the_list_order_keeps_what_each_component_needs():
    """Storage first (it builds the repository every later one reads, and closes after all of them), the SIMKL client
    before the sync worker (whose stop flushes the outbox before the client closes), the container before the
    notifier and the webhooks that sit on it (the webhooks also need the database to listen to), the sampler last (it reads the finished container)."""
    names = [n.removeprefix("penguwatch.") for n in LISTED]
    assert names == ["db.storage", "sync.component", "optimizer.component", "api.component", "sync.worker",
                     "notify_component", "webhooks_component", "telemetry_component"]


# --- the lifespan runs the list ---------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_app(tmp_path, monkeypatch):
    for var in ("SIMKL_ACCESS_TOKEN", "SIMKL_CLIENT_ID", "PENGUWATCH_NOTIFY_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PENGUWATCH_DB_PATH", str(tmp_path / "data" / "app.db"))
    level = logging.getLogger().level
    yield
    logging.getLogger().setLevel(level)
    if hasattr(main.app.state, "container"):
        del main.app.state.container


class _FakeModule:
    """A component module made up for the test: register(ctx) records its calls."""

    def __init__(self, name, events, fail=None, optional=False):
        self.__name__ = f"penguwatch.fake.{name}"
        self.name, self.events, self.fail = name, events, fail
        if optional:
            self.OPTIONAL = True

    async def register(self, ctx):
        self.events.append((self.name, "register", ctx.container is not None))
        if self.fail == "register":
            raise RuntimeError("boom")

        async def start():
            self.events.append((self.name, "start"))
            if self.fail == "start":
                raise RuntimeError("boom")
            ctx.spawn(asyncio.sleep(3600))

        async def stop():
            self.events.append((self.name, "stop", all(t.done() for t in ctx.tasks)))

        return Component(self.name, start=start, stop=stop)


async def test_a_component_added_to_the_list_is_registered_started_and_stopped(monkeypatch):
    """Adding a part is one more entry in COMPONENTS: it registers after the container, starts, and stops first,
    after every task has ended."""
    events = []
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, _FakeModule("extra", events)])
    async with main.lifespan(main.app):
        assert main.app.state.container is not None
        assert events == [("extra", "register", True), ("extra", "start")]
    assert events[-1] == ("extra", "stop", True)


@pytest.mark.parametrize("step", ["register", "start"])
async def test_an_optional_components_failure_costs_only_that_part(monkeypatch, caplog, step):
    from httpx import ASGITransport, AsyncClient
    events = []
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, _FakeModule("flaky", events, fail=step, optional=True),
                                             _FakeModule("after", events)])
    with caplog.at_level(logging.ERROR, logger="penguwatch"):
        async with main.lifespan(main.app):
            async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
                assert (await ac.get("/api/health")).status_code == 200
    assert ("after", "start") in events and ("after", "stop", True) in events
    assert [r.getMessage() for r in caplog.records if "flaky" in r.getMessage()] == [
        f"fake.flaky could not {step}: boom; running without it"]


async def test_a_required_components_failure_stops_the_start_and_closes_what_was_opened(monkeypatch):
    from penguwatch.db.repository import DatabaseRepository
    closed = []
    real_close = DatabaseRepository.close

    async def close(self):
        closed.append(True)
        await real_close(self)

    monkeypatch.setattr(DatabaseRepository, "close", close)
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, _FakeModule("core", [], fail="register")])
    with pytest.raises(RuntimeError, match="boom"):
        async with main.lifespan(main.app):
            pass
    assert closed == [True]  # the database the start had opened is closed again


async def test_a_stop_that_fails_is_logged_and_the_rest_still_stop(monkeypatch, caplog):
    from penguwatch.db.repository import DatabaseRepository
    closed = []
    real_close = DatabaseRepository.close

    async def close(self):
        closed.append(True)
        await real_close(self)

    class Breaks(_FakeModule):
        async def register(self, ctx):
            async def stop():
                raise RuntimeError("stuck")
            return Component("breaks", stop=stop)

    monkeypatch.setattr(DatabaseRepository, "close", close)
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, Breaks("breaks", [])])
    with caplog.at_level(logging.ERROR, logger="penguwatch"):
        async with main.lifespan(main.app):
            pass
    assert closed == [True]
    assert "breaks did not stop cleanly: stuck" in caplog.text


async def test_shutdown_order(monkeypatch):
    """Every spawned task ends before any component stops; the outbox is flushed before the SIMKL client closes, and
    the repository closes after both; the telemetry flush comes last, after the final log line."""
    from penguwatch.db.repository import DatabaseRepository
    from penguwatch.drivers.simkl.client import SimklClient
    from tests.simkl_helpers import simkl_engine

    events = []
    tasks = []
    real_spawn = Context.spawn

    def spawn(self, coro):
        tasks.append(real_spawn(self, coro))
        return tasks[-1]

    monkeypatch.setattr(Context, "spawn", spawn)

    def spy(cls, method, label):
        real = getattr(cls, method)

        async def wrapper(self, *args, **kwargs):
            events.append((label, all(t.done() for t in tasks)))
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(cls, method, wrapper)

    from penguwatch.sync.engine import SyncEngine
    spy(SyncEngine, "flush_outbox", "outbox flushed")
    spy(SimklClient, "aclose", "client closed")
    spy(DatabaseRepository, "close", "repository closed")

    class Telemetry:
        meter_provider = object()

        def shutdown(self):
            events.append(("telemetry flushed", True))

    real_info = main.logger.info

    def info(msg, *args, **kwargs):
        if msg == "PenguWatch shut down.":
            events.append(("last log line", True))
        real_info(msg, *args, **kwargs)

    monkeypatch.setattr(main.telemetry, "setup_telemetry", lambda *a, **k: Telemetry())
    monkeypatch.setattr(main.logger, "info", info)
    monkeypatch.setattr("penguwatch.telemetry.sample_loop", lambda container: asyncio.sleep(3600))
    monkeypatch.setenv("PENGUWATCH_NOTIFY_URL", "http://127.0.0.1:9/topic")
    async with main.lifespan(main.app):
        assert len(tasks) == 3  # sync worker, notification worker, telemetry sampler

    assert [label for label, _ in events] == ["outbox flushed", "client closed", "repository closed", "last log line",
                                              "telemetry flushed"]
    assert all(done for _, done in events), events  # every task had ended


async def test_a_spawned_task_that_dies_is_logged_when_it_dies_and_a_cancelled_one_is_not(caplog):
    async def crash():
        raise RuntimeError("boom")

    ctx = Context(app=None, config=None)
    idle = ctx.spawn(asyncio.sleep(3600))
    with caplog.at_level(logging.ERROR, logger="penguwatch.components"):
        task = ctx.spawn(crash())
        await asyncio.wait([task])
        await asyncio.sleep(0)
        assert [r for r in caplog.records if "boom" in r.getMessage()]
        caplog.clear()
        idle.cancel()
        await asyncio.gather(idle, return_exceptions=True)
        await asyncio.sleep(0)
    assert not caplog.records


async def test_stops_run_in_reverse_order_after_the_tasks_end():
    order = []

    class Part:
        def __init__(self, name):
            self.__name__ = f"penguwatch.fake.{name}"
            self.name = name

        async def register(self, ctx):
            async def stop():
                order.append((self.name, all(t.done() for t in ctx.tasks)))
            ctx.spawn(asyncio.sleep(3600))
            return Component(self.name, stop=stop)

    async with run_components(None, None, [Part("a"), Part("b"), Part("c")]):
        pass
    assert order == [("c", True), ("b", True), ("a", True)]
