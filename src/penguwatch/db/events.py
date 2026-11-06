"""Events raised by data changes. Listeners (the webhook dispatcher) are called after the change is committed."""

import logging
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger("penguwatch.events")

Listener = Callable[[str, Dict[str, Any]], None]


class EventsMixin:
    """Needs `_event_listeners` and `_pending_events` (set in DatabaseRepository.__init__) and write() calling
    `_publish_pending` after a commit."""

    _event_listeners: List[Listener]
    _pending_events: List[Tuple[str, Dict[str, Any]]]

    def add_event_listener(self, listener: Listener) -> None:
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Listener) -> None:
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    _events_muted = 0

    def mute_events(self) -> None:
        """Data-change events are not raised until unmute_events (the first full sync sends one summary instead)."""
        self._events_muted += 1

    def unmute_events(self) -> None:
        self._events_muted = max(0, self._events_muted - 1)

    def queue_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Inside a write() block: the event is published when (and only if) the transaction commits."""
        if self._event_listeners and not self._events_muted:
            self._pending_events.append((event_type, data))

    def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Publishes now; for things that are not a database change (a failed sync). A listener must not block."""
        for listener in list(self._event_listeners):
            try:
                listener(event_type, data)
            except Exception:
                logger.exception("an event listener failed")

    def _publish_pending(self, committed: bool) -> None:
        events, self._pending_events = self._pending_events, []
        if committed:
            for event_type, data in events:
                self.emit(event_type, data)
