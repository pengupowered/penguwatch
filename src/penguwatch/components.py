"""The component contract: how a part of the backend plugs into the app. The design is in docs/architecture.md.

A component is a module with `async def register(ctx) -> Component | None`, listed once in penguwatch.main.COMPONENTS.
`register` builds its parts from `ctx` (the config, and what earlier components left on the context) and returns what
the app runs for it, or None when there is nothing to run. A module may set `OPTIONAL = True`, which contains a
failure of its register or start: the error is logged and the app runs without that part.

`run_components` is the lifespan's middle: register every component in list order, start them in list order; at
shutdown cancel every task they spawned, then stop them in reverse order.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Coroutine, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from fastapi import FastAPI

    from penguwatch.api.deps import AppContainer
    from penguwatch.config import AppConfig, EnvOverrides
    from penguwatch.db.repository import DatabaseRepository
    from penguwatch.notify import Notifier
    from penguwatch.optimizer.engine import SubscriptionOptimizer
    from penguwatch.security import SecurityConfig
    from penguwatch.settings.hub import SettingsHub
    from penguwatch.sync.active import ActiveDrivers
    from penguwatch.sync.engine import SyncEngine
    from penguwatch.telemetry import Telemetry
    from penguwatch.webhooks import WebhookDispatcher

logger = logging.getLogger("penguwatch.components")


@dataclass
class Component:
    """What the app runs for one part. Both steps are optional.

    `start` runs after every component has registered, in list order, and starts the background work with ctx.spawn.
    `stop` runs at shutdown, after every spawned task has ended, in reverse list order; it closes what the part owns."""

    name: str
    start: Optional[Callable[[], Awaitable[None]]] = None
    stop: Optional[Callable[[], Awaitable[None]]] = None


@dataclass
class Context:
    """What register(ctx) gets: the app, the config, and the parts earlier components built for later ones. A field
    is None until its component has registered (the list order is what makes a later component's use of it safe)."""

    app: "FastAPI"
    config: "AppConfig"
    env: Optional["EnvOverrides"] = None
    security: Optional["SecurityConfig"] = None
    otel: Optional["Telemetry"] = None
    repository: Optional["DatabaseRepository"] = None  # storage
    settings: Optional["SettingsHub"] = None  # settings: appliers subscribe here in their register (settings/hub.py)
    settings_manager: Any = None  # settings: the settings store (settings.manager.SettingsManager)
    drivers: Optional["ActiveDrivers"] = None  # drivers: the running drivers, by name and by role
    sync: Optional["SyncEngine"] = None  # drivers: the sync engine on the library driver
    optimizer: Optional["SubscriptionOptimizer"] = None  # optimizer
    container: Optional["AppContainer"] = None  # api.component: what the request handlers read
    notifier: Optional["Notifier"] = None  # notifications
    webhooks: Optional["WebhookDispatcher"] = None  # webhooks
    components: List[Component] = field(default_factory=list)  # registered so far, in list order
    tasks: List[asyncio.Task] = field(default_factory=list)

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """Starts a background task that runs until shutdown, when it is cancelled before any component stops."""
        task = asyncio.create_task(coro)
        task.add_done_callback(self._forget)
        self.tasks.append(task)
        return task

    def _forget(self, task: "asyncio.Task") -> None:
        """A task that ends on its own is not kept; one that died of an exception is logged when it dies."""
        try:
            self.tasks.remove(task)
        except ValueError:
            pass
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            logger.error(f"Background task {task.get_name()} stopped: {type(exc).__name__}: {exc}", exc_info=exc)


def _name(module) -> str:
    return module.__name__.removeprefix("penguwatch.")


async def _contained(module, what: str, step: Callable[[], Awaitable[Any]]) -> Tuple[bool, Any]:
    """Runs a register or start step. A required component's failure stops the start; an optional one's is logged
    and costs only that part. Returns (ok, result)."""
    if not getattr(module, "OPTIONAL", False):
        return True, await step()
    try:
        return True, await step()
    except Exception as e:
        logger.error(f"{_name(module)} could not {what}: {e}; running without it", exc_info=True)
        return False, None


async def _shut_down(ctx: Context) -> None:
    """Cancels every spawned task and waits for them, then stops the components in reverse order; a stop that fails
    is logged and the others still run."""
    tasks = list(ctx.tasks)  # (a task that ends leaves the list)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for component in reversed(ctx.components):
        if component.stop is None:
            continue
        try:
            await component.stop()
        except Exception as e:
            logger.error(f"{component.name} did not stop cleanly: {e}", exc_info=True)


@asynccontextmanager
async def run_components(app: "FastAPI", config: "AppConfig", modules: Sequence, **parts: Any) -> AsyncIterator[Context]:
    """Registers and starts `modules` (each with register(ctx)); stops them again when the block ends. `parts` are
    the Context fields the frame already has (env, security, otel). A required component's failure before the yield
    propagates, after the ones already registered have been stopped: the start fails, nothing is left open."""
    ctx = Context(app=app, config=config, **parts)
    try:
        registered = []  # (module, component), for the start step
        for module in modules:
            ok, component = await _contained(module, "register", lambda m=module: m.register(ctx))
            if ok and component is not None:
                ctx.components.append(component)
                registered.append((module, component))
        for module, component in registered:
            if component.start is not None:
                await _contained(module, "start", component.start)  # an optional one that failed is still stopped
    except BaseException:
        await _shut_down(ctx)
        raise
    try:
        yield ctx
    finally:
        await _shut_down(ctx)
