"""The settings hub: how a component hears that a saved setting changed, so it can apply it without a restart.

Most settings need no applier: after a save the settings manager copies the new effective values into the live
config objects in place (every holder of `ctx.config` / `container.config` sees them at once). A component whose
running parts keep a copy (the clock's zone, the notifier's URL, the webhook workers) subscribes in its `register`:

    @ctx.settings.on(keys=["server.timezone"], name="clock.zone")
    def _zone(change):
        clock.configure(ctx.config.server.timezone)

After every commit the manager calls `publish(change)`. Each applier whose subscription the change touches is called
once, in registration order (= component order, penguwatch.main.COMPONENTS), and each is contained: one that raises
is logged (type and place, never the message, which can quote a value) and named in the result, so the save reports
its parts in `live_failed` ("applies at the next start": the store already holds them).

A subscription matches a change by exact `keys`, by key `prefixes` ("notify." matches "notify.hour"), by
`lists` ("webhooks") and by `slots` (a secret slot such as "notify.url"; an entry ending in "." matches by prefix, so
"webhook." matches "webhook.home.url" and "webhook.home.secret").
"""

import inspect
import logging
import os
import traceback
from dataclasses import dataclass, field
from typing import Awaitable, Callable, FrozenSet, Iterable, List, Optional, Union

from penguwatch.settings.document import SettingsDocument

_log = logging.getLogger("penguwatch.settings")


@dataclass(frozen=True)
class Change:
    """What one save changed. `keys`: registry keys whose effective value changed; `lists`: list names; `slots`:
    secret slots written or cleared (never their values). `old` and `new` are the whole documents before and after
    (`old` is None for the start-up load)."""

    keys: FrozenSet[str] = frozenset()
    lists: FrozenSet[str] = frozenset()
    slots: FrozenSet[str] = frozenset()
    old: Optional[SettingsDocument] = None
    new: SettingsDocument = field(default_factory=SettingsDocument)

    @classmethod
    def of(cls, *, keys: Iterable[str] = (), lists: Iterable[str] = (), slots: Iterable[str] = (),
           old: Optional[SettingsDocument] = None, new: Optional[SettingsDocument] = None) -> "Change":
        return cls(frozenset(keys), frozenset(lists), frozenset(slots), old,
                   new if new is not None else SettingsDocument())

    @property
    def empty(self) -> bool:
        return not (self.keys or self.lists or self.slots)


ApplyFn = Callable[[Change], Union[Awaitable[None], None]]


@dataclass(frozen=True)
class Subscription:
    name: str
    apply: ApplyFn
    keys: FrozenSet[str]
    prefixes: tuple
    lists: FrozenSet[str]
    slots: FrozenSet[str]

    def touched(self, change: Change) -> List[str]:
        """The parts of `change` this subscription covers, sorted: keys, list names and slots."""
        keys = {k for k in change.keys if k in self.keys or k.startswith(self.prefixes)}
        slots = {s for s in change.slots
                 if s in self.slots or any(p.endswith(".") and s.startswith(p) for p in self.slots)}
        return sorted(keys | (change.lists & self.lists) | slots)


class SettingsHub:
    """`on_failure(name)` is called once per applier that raised (the settings component passes the telemetry counter
    penguwatch.settings.live_failures)."""

    def __init__(self, on_failure: Optional[Callable[[str], None]] = None) -> None:
        self._subscriptions: List[Subscription] = []
        self._on_failure = on_failure

    def on(self, keys: Iterable[str] = (), *, prefixes: Iterable[str] = (), lists: Iterable[str] = (),
           slots: Iterable[str] = (), name: str) -> Callable[[ApplyFn], ApplyFn]:
        """Decorator: subscribes the function to the changes that touch any of these. `name` is what logs and the
        `live_failed` list call it ("notify.channel"); names are unique."""
        keys, prefixes = frozenset([keys] if isinstance(keys, str) else keys), tuple(prefixes)
        if not (keys or prefixes or lists or slots):
            raise ValueError(f"settings applier {name!r} subscribes to nothing")
        if any(s.name == name for s in self._subscriptions):
            raise ValueError(f"settings applier {name!r} is registered twice")

        def register(fn: ApplyFn) -> ApplyFn:
            self._subscriptions.append(Subscription(name, fn, keys, prefixes, frozenset(lists), frozenset(slots)))
            return fn
        return register

    @property
    def names(self) -> List[str]:
        """The appliers' names, in registration order."""
        return [s.name for s in self._subscriptions]

    def touched_by(self, name: str, change: Change) -> List[str]:
        """The parts of `change` the applier `name` covers (for the save's `live_failed` list)."""
        for subscription in self._subscriptions:
            if subscription.name == name:
                return subscription.touched(change)
        raise KeyError(name)

    def subscribers(self, change: Change) -> List[str]:
        """The names of the appliers `change` would call, in order."""
        return [s.name for s in self._subscriptions if s.touched(change)]

    async def publish(self, change: Change) -> List[str]:
        """Calls every applier the change touches, in registration order; returns the names of those that raised."""
        failed: List[str] = []
        for subscription in self._subscriptions:
            if not subscription.touched(change):
                continue
            try:
                result = subscription.apply(change)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                failed.append(subscription.name)
                if self._on_failure is not None:
                    try:
                        self._on_failure(subscription.name)
                    except Exception:  # a counter must never turn a contained failure into an uncontained one
                        pass
                # The error text and the traceback can quote a value (a URL a library refused), so the ERROR line has
                # the type, the parts (setting keys, list names, slot names; never a value) and where it was raised.
                frame = traceback.extract_tb(e.__traceback__)[-1]
                _log.error(f"Settings applier {subscription.name} failed: {type(e).__name__} at "
                           f"{os.path.basename(frame.filename)}:{frame.lineno} (parts: "
                           f"{', '.join(subscription.touched(change))}); the change applies at the next start")
                # the frames only, not the exception's message (it can quote a value)
                _log.debug(f"Settings applier {subscription.name} failed: {type(e).__name__}\n"
                           + "".join(traceback.format_tb(e.__traceback__)))
        return failed
