"""Event webhooks: deliver PenguWatch events to URLs from config.yaml (docs/extending.md).

Delivery is in the background and cannot hurt the app: each webhook has its own bounded queue and its own task, so a
receiver that is down delays only itself, and a full queue drops new events (counted) instead of growing. Requests
have a timeout, never follow a redirect, and only the status is read from the answer. A failed delivery is retried
a few times with growing pauses; a client error (4xx apart from 408 and 429) is not retried. The signature header
lets the receiver check the body came from this app (`sign`).

Secrets and URLs stay out of logs, errors and status: a webhook is named by its `name`, shown by host only, and a
failure is described by its HTTP status or the kind of error, never by text that could carry the URL.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from penguwatch import __version__, clock, telemetry
from penguwatch.events import EVENT_TYPES, envelope
from penguwatch.observability import metrics

logger = logging.getLogger("penguwatch.webhooks")

QUEUE_SIZE = 200  # events waiting per webhook
TIMEOUT_SECONDS = 10.0
RETRY_DELAYS = (2.0, 10.0, 60.0)  # pauses before the 2nd, 3rd and 4th attempt
MAX_BODY_BYTES = 64 * 1024
MIN_SECRET_LENGTH = 16
DROPPING_WINDOW_SECONDS = 600

SIGNATURE_HEADER = "X-PenguWatch-Signature"
TIMESTAMP_HEADER = "X-PenguWatch-Timestamp"
EVENT_HEADER = "X-PenguWatch-Event"
DELIVERY_HEADER = "X-PenguWatch-Delivery"


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    """`sha256=<hex>`: HMAC-SHA256 with the secret over the timestamp, a dot, and the exact body bytes."""
    mac = hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def display_host(url: str) -> str:
    """host[:port] of the URL: all that is ever shown of it (the path or query may hold a token)."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    return f"{host}:{parts.port}" if parts.port else host


@dataclass
class Webhook:
    name: str
    url: str = field(repr=False)
    events: FrozenSet[str] = frozenset(EVENT_TYPES)
    secret: Optional[bytes] = field(default=None, repr=False)
    disabled_reason: Optional[str] = None  # set when config asked for it but it cannot run (missing secret)
    delivered: int = 0
    failed: int = 0
    dropped: int = 0
    last_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_dropped_at: Optional[float] = None  # time.time() of the latest drop (a full queue)
    last_error: Optional[str] = None  # an HTTP status or the kind of error; never text from the answer or the URL
    queue: "asyncio.Queue[Tuple[str, str, bytes]]" = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE), repr=False)

    @property
    def signed(self) -> bool:
        return self.secret is not None

    @property
    def state(self) -> str:
        if self.disabled_reason:
            return "disabled"
        if self.last_error:  # cleared by the next success
            return "failing"
        if self.last_dropped_at and time.time() - self.last_dropped_at < DROPPING_WINDOW_SECONDS:
            return "dropping"  # the queue was full lately: events are being lost
        return "ok" if self.last_success_at else "idle"

    def describe(self) -> Dict[str, Any]:
        """What the System tab and /api/webhooks show. No URL, no secret."""
        return {
            "name": self.name, "host": display_host(self.url), "events": sorted(self.events), "signed": self.signed,
            "state": self.state, "disabled_reason": self.disabled_reason, "queued": self.queue.qsize(),
            "delivered": self.delivered, "failed": self.failed, "dropped": self.dropped,
            "last_dropped_at": (datetime.fromtimestamp(self.last_dropped_at, timezone.utc).isoformat()
                                if self.last_dropped_at else None),
            "last_attempt_at": self.last_attempt_at, "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }


Outcome = Tuple[str, str]  # ("ok" | "retry" | "fail", detail)


class WebhookDispatcher:
    def __init__(self, hooks: List[Webhook], *, transport: Optional[httpx.AsyncBaseTransport] = None,
                 retry_delays: Tuple[float, ...] = RETRY_DELAYS, timeout: float = TIMEOUT_SECONDS,
                 max_body: int = MAX_BODY_BYTES, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.hooks = hooks
        self.timeout = timeout
        self.retry_delays = retry_delays
        self.max_body = max_body
        self._sleep = sleep
        # No redirects: a receiver (or whoever sits in front of it) must not be able to point the request elsewhere
        self._client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False,  # no proxy or CA variables from the environment
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={"User-Agent": f"PenguWatch/{__version__}"})
        self.recent: Deque[Dict[str, Any]] = deque(maxlen=20)

    # --- publishing (called from the repository, synchronously, after a commit) ---

    def publish(self, event_type: str, data: Dict[str, Any], event_id: Optional[str] = None) -> int:
        """Queues the event for every webhook that subscribed to it; returns how many took it. Never blocks."""
        taken = 0
        body = None
        for hook in self.hooks:
            if hook.disabled_reason or event_type not in hook.events:
                continue
            if body is None:
                env = envelope(event_type, data, event_id)
                body = (env["id"], json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            try:
                hook.queue.put_nowait((event_type, body[0], body[1]))
                taken += 1
            except asyncio.QueueFull:
                hook.dropped += 1
                hook.last_dropped_at = time.time()
                self._count(hook, "dropped")
        return taken

    def subscribers(self, event_type: str) -> bool:
        return any(event_type in h.events and not h.disabled_reason for h in self.hooks)

    # --- delivering ---

    def workers(self) -> List[Awaitable[None]]:
        return [self._run(hook) for hook in self.hooks if not hook.disabled_reason]

    async def _run(self, hook: Webhook) -> None:
        while True:
            event_type, event_id, body = await hook.queue.get()
            try:
                await self.deliver(hook, event_type, event_id, body)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a bug here must not end the worker
                self._failed(hook, f"internal error ({type(e).__name__})", event_type)

    async def deliver(self, hook: Webhook, event_type: str, event_id: str, body: bytes) -> bool:
        """One event to one webhook, with retries. True when the receiver accepted it."""
        if len(body) > self.max_body:
            self._failed(hook, "payload too large", event_type)
            return False
        attempts = len(self.retry_delays) + 1
        detail = ""
        for attempt in range(attempts):
            hook.last_attempt_at = clock.utc_now().isoformat()
            result, detail = await self._attempt(hook, event_type, event_id, body)
            if result == "ok":
                hook.delivered += 1
                hook.last_success_at = hook.last_attempt_at
                hook.last_error = None
                self._count(hook, "delivered")
                self._remember(hook, event_type, "delivered", detail)
                return True
            hook.last_error = detail
            if result == "fail" or attempt == attempts - 1:
                break
            await self._sleep(self.retry_delays[attempt])
        self._failed(hook, detail, event_type)
        return False

    async def _attempt(self, hook: Webhook, event_type: str, event_id: str, body: bytes) -> Outcome:
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", EVENT_HEADER: event_type, DELIVERY_HEADER: event_id,
                   TIMESTAMP_HEADER: timestamp}
        if hook.secret is not None:
            headers[SIGNATURE_HEADER] = sign(hook.secret, timestamp, body)
        try:
            # stream(): only the status line and headers are read; the answer's body is never downloaded
            # one deadline for the whole attempt: the per-read timeouts alone let a receiver that trickles bytes hold it
            async with asyncio.timeout(self.timeout):
                async with self._client.stream("POST", hook.url, content=body, headers=headers) as response:
                    code = response.status_code
        except (httpx.TimeoutException, TimeoutError):
            return "retry", "timeout"
        except httpx.TransportError as e:
            return "retry", f"connection error ({type(e).__name__})"
        except (httpx.InvalidURL, ValueError) as e:
            return "fail", f"invalid request ({type(e).__name__})"
        if 200 <= code < 300:
            return "ok", f"HTTP {code}"
        if 300 <= code < 400:
            return "fail", f"HTTP {code} (redirects are not followed)"
        if code in (408, 429) or code >= 500:
            return "retry", f"HTTP {code}"
        return "fail", f"HTTP {code}"

    def _failed(self, hook: Webhook, detail: str, event_type: str = "") -> None:
        hook.failed += 1
        hook.last_error = detail
        self._count(hook, "failed")
        self._remember(hook, event_type, "failed", detail)
        logger.warning(f"Webhook '{hook.name}' delivery failed: {detail}")

    def _remember(self, hook: Webhook, event_type: str, outcome: str, detail: str) -> None:
        self.recent.append({"at": clock.utc_now().isoformat(), "webhook": hook.name, "event": event_type,
                            "outcome": outcome, "detail": detail})

    @staticmethod
    def _count(hook: Webhook, outcome: str) -> None:
        metrics.observe_webhook(hook.name, outcome)
        telemetry.WEBHOOK_DELIVERIES.add(1, {"outcome": outcome})

    async def send_test(self, hook: Webhook) -> bool:
        """A `webhook.test` event straight to one webhook (not queued, not retried)."""
        env = envelope("webhook.test", {"message": "PenguWatch webhook test"})
        body = json.dumps(env, separators=(",", ":")).encode("utf-8")
        hook.last_attempt_at = clock.utc_now().isoformat()
        result, detail = await self._attempt(hook, "webhook.test", env["id"], body)
        if result == "ok":
            hook.delivered += 1
            hook.last_success_at = hook.last_attempt_at
            hook.last_error = None
            self._count(hook, "delivered")
            return True
        hook.last_error = detail
        self._failed(hook, detail, "webhook.test")
        return False

    def describe(self) -> List[Dict[str, Any]]:
        return [h.describe() for h in self.hooks]

    async def close(self) -> None:
        await self._client.aclose()
