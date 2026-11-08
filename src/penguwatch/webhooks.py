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
import ipaddress
import json
import logging
import posixpath
import re
import socket
import time
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple
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


# --- what the settings page may set --------------------------------------------------------------
# The settings can be edited without an access token, so everything here is a security rule. Each check returns a
# fixed message or None; a message never repeats the value. The settings validation (settings/validate.py) is
# wired to these functions:
#
#   check_item_secrets(item, stored_item)  per webhook item of a save, paired with the stored item BY ID ONLY (never
#       by name; None for an id that is not stored). The page may not SET a `secret_env` or `secret_file`: a new or
#       changed value is refused, because PenguWatch would sign a body of the caller's choosing with that variable
#       or file of the server and send it to an address of the caller's choosing (an offline guessing oracle), and a
#       failing file would tell the caller whether it exists. An unchanged value (imported from config.yaml) is kept,
#       and clearing it switches the hook to the stored secret `webhook.<id>.secret`.
#   check_save(old_item, new_item, url_changed, secret_slot_set)  per item, with the facts of the whole save: an
#       item that still reads an imported secret_env / secret_file may not get a new address (that would send the
#       old secret's signature wherever the caller likes). Signature: see its docstring.
#   check_hook_url(url)   per `webhook.<id>.url` slot written: http(s), a host, no credentials, valid port, and not a
#       host that is this machine or its link-local / multicast ranges (SSRF); RFC 1918 stays allowed.
#   check_slot_secret(value)  per `webhook.<id>.secret` slot written.
# check_secret_env / check_secret_file validate values that come from the IMPORT only (config.yaml).
# The same address denylist is enforced again at connect time against the RESOLVED address (GuardedTransport), which
# closes DNS rebinding; it applies to the webhook client and to the digest's client.

SECRET_ENV_RE = re.compile(r"^PENGUWATCH_WEBHOOK_[A-Z0-9_]{1,40}$")
SECRET_FILE_DIR = "/run/secrets/"
NOT_SETTABLE = "Cannot be set here. Use a stored signing secret instead."
ADDRESS_NOT_ALLOWED = "That address is this machine or a local-link address; use the address of another device."


def check_secret_env(name: str) -> Optional[str]:
    """For an imported value: a variable named for webhooks."""
    if not SECRET_ENV_RE.fullmatch(name or ""):
        return "Must be a variable named PENGUWATCH_WEBHOOK_ followed by capital letters, digits or '_'."
    return None


def check_secret_file(path: str) -> Optional[str]:
    """For an imported value: an absolute, normalised path under /run/secrets/."""
    path = path or ""
    clean = posixpath.normpath(path) == path  # no '..', '.', '//' or trailing '/'
    if "\0" in path or not clean or not path.startswith(SECRET_FILE_DIR) or len(path) == len(SECRET_FILE_DIR):
        return "Must be a file path under /run/secrets/."
    return None


def check_slot_secret(value: str) -> Optional[str]:
    if len((value or "").strip()) < MIN_SECRET_LENGTH:  # stripped, as resolve_secret strips before its own check
        return f"Must be at least {MIN_SECRET_LENGTH} characters."
    return None


def _blocked_ip(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or (ipaddress.IPv4Address(ip.packed[-4:]) if ip in _NAT64 or ip in _V4_COMPATIBLE else None)
        if embedded is not None:
            return _blocked_ip(embedded)
    return (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
            or (isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK))


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


def blocked_host(host: str) -> bool:
    """True for a host a webhook or the digest must not reach: localhost, loopback, link-local, unspecified and
    multicast addresses (IPv4-mapped forms included), and any numeric host that is not a plain dotted quad (decimal,
    octal and hex forms such as 2130706433 or 0x7f.1 are how a loopback address hides). RFC 1918 and unique-local
    addresses are allowed: the receiver is usually on the LAN."""
    host = (host or "").strip().strip("[]").rstrip(".").lower().split("%", 1)[0]
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return _blocked_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        socket.inet_aton(host)  # accepts 2130706433, 0x7f.1, 0177.0.0.1 ...
    except (OSError, ValueError):
        return False
    return True  # numeric but not a dotted quad


def check_hook_url(url: str) -> Optional[str]:
    """http(s) with a host, no user:password@, no whitespace, control or DEL characters, a valid port, and a host
    that `blocked_host` allows."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018  (raises for a port outside 0-65535 or not a number)
    except ValueError:
        return "The port is not valid."
    if parts.scheme not in ("http", "https") or not parts.hostname \
            or any(c.isspace() or ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in url):
        return "Must be an http:// or https:// address."
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        return "Must not contain a user name or password."
    if blocked_host(parts.hostname):
        return ADDRESS_NOT_ALLOWED
    return None


def check_item_secrets(item: Mapping[str, Any], stored: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """{field: message} for the `secret_env` / `secret_file` of one webhook item of a save. `stored` is the item as
    it is stored now, found BY ID (None for a new item). A value equal to the stored one is kept; any other value
    (new or changed) is refused with a fixed message: the page can only keep an imported value or clear it."""
    stored = stored or {}
    return {name: NOT_SETTABLE for name in ("secret_env", "secret_file")
            if item.get(name) and item.get(name) != stored.get(name)}


def check_save(old_item: Optional[Mapping[str, Any]], new_item: Mapping[str, Any], url_changed: bool,
               secret_slot_set: bool) -> Optional[str]:
    """One webhook item of a save, with the facts of the whole save; returns a fixed message or None.

    old_item         the stored item with the SAME ID (never matched by name), None when the id is not stored
    new_item         the item as the save would store it
    url_changed      the save writes the `webhook.<id>.url` slot (set to another value, or cleared and set again)
    secret_slot_set  the save writes a non-empty `webhook.<id>.secret`

    An item that reads an imported `secret_env` / `secret_file` signs with a secret the page cannot see. Moving it to
    another address (a new id counts as another address) would hand a signature made with that secret to whoever
    typed the address. So that is refused, unless the same save clears both fields and stores a secret of its own."""
    def reads_file_or_env(item) -> bool:
        return bool(item and (item.get("secret_env") or item.get("secret_file")))

    if not (old_item is None or url_changed):
        return None
    if reads_file_or_env(new_item) or (reads_file_or_env(old_item) and not secret_slot_set):
        return "The address of a webhook that signs with a variable or file can't be changed here; set a signing secret instead."
    return None


# --- connect-time guard -------------------------------------------------------------------------------------

async def resolve_host(host: str, port: int) -> List[str]:
    """The addresses a name resolves to (an IP literal resolves to itself)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0].split("%", 1)[0] for info in infos]


class GuardedTransport(httpx.AsyncBaseTransport):
    """Connects to the RESOLVED address and refuses one that `blocked_host` refuses, so a name that points at this
    machine (or changes its answer between the settings check and the delivery: DNS rebinding) never gets a request.
    The request goes to the checked IP with its Host header and SNI name unchanged, so TLS still verifies the name."""

    def __init__(self, inner: Optional[httpx.AsyncBaseTransport] = None):
        self._inner = inner or httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            addresses = await resolve_host(host, port)
        except OSError as e:
            raise httpx.ConnectError("name not resolved") from e
        if not addresses or any(blocked_host(a) for a in addresses):
            raise httpx.ConnectError("address not allowed")
        if addresses[0] != host:
            request.extensions = {**request.extensions, "sni_hostname": host}
            request.url = request.url.copy_with(host=addresses[0])
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def failure_label(error: BaseException) -> str:
    """A coarse, fixed description of a failed request: no status code, no exception name, no text."""
    if isinstance(error, httpx.HTTPStatusError):
        return status_label(error.response.status_code)
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "timed out"
    return "could not connect"


def status_label(code: int) -> str:
    if 200 <= code < 300:
        return "delivered"
    if 300 <= code < 400:
        return "redirect not followed"
    if 400 <= code < 500:
        return "refused by the receiver (HTTP 4xx)"
    return "server error (HTTP 5xx)"


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
    id: Optional[str] = None  # the settings item's id (None for a hook built without one)

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
            transport=transport if transport is not None else GuardedTransport(), follow_redirects=False, trust_env=False,  # no proxy or CA variables from the environment
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={"User-Agent": f"PenguWatch/{__version__}"})
        self.recent: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._spawn: Optional[Callable[[Awaitable[None]], "asyncio.Task"]] = None  # set by start(); None before it
        self._tasks: Dict[int, "asyncio.Task"] = {}  # id(hook) -> its worker

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

    def start(self, spawn: Callable[[Awaitable[None]], "asyncio.Task"]) -> None:
        """Spawns a worker for every hook that can run, through `spawn` (the component's ctx.spawn), and remembers
        `spawn` so that set_hooks can start workers for hooks added later."""
        self._spawn = spawn
        for hook in self.hooks:
            self._start_worker(hook)

    def _start_worker(self, hook: Webhook) -> None:
        if self._spawn is not None and not hook.disabled_reason and id(hook) not in self._tasks:
            self._tasks[id(hook)] = self._spawn(self._run(hook))

    @staticmethod
    def _key(hook: Webhook) -> Tuple[str, str]:
        """Hooks pair by id only; an id-less (legacy, built without the store) hook pairs by name in its own space."""
        return ("id", hook.id) if hook.id else ("name", hook.name)

    def set_hooks(self, hooks: List[Webhook], spawn: Optional[Callable[[Awaitable[None]], "asyncio.Task"]] = None) -> None:
        """Replaces the hook list while running (settings applier). A hook with the same id (or, without one, the
        same name) and the same address, events, secret and state keeps its Webhook object, so its queue, counters
        and recent results survive. Otherwise the old worker is cancelled (a delivery sleeping between retries
        never sends again to the old address) and its queued events are counted as dropped; when only the events
        changed, the queued events the new hook still subscribes to are moved to it. A new hook gets a worker
        through `spawn`. Before start() the list is just replaced."""
        if spawn is not None:
            self._spawn = spawn
        current = {self._key(h): h for h in self.hooks}
        kept: List[Webhook] = []
        replaced: Dict[int, Webhook] = {}  # id(old) -> the hook that took its place
        for new in hooks:
            old = current.get(self._key(new))
            if old is not None and (old.url, old.events, old.secret, old.disabled_reason) == \
                    (new.url, new.events, new.secret, new.disabled_reason):
                old.name = new.name  # a rename alone changes nothing else
                kept.append(old)
            else:
                kept.append(new)
                if old is not None:
                    replaced[id(old)] = new
        keep_ids = {id(h) for h in kept}
        for old in self.hooks:
            if id(old) in keep_ids:
                continue
            task = self._tasks.pop(id(old), None)
            if task is not None:
                task.cancel()
            heir = replaced.get(id(old))
            same_target = heir is not None and (old.url, old.secret, old.disabled_reason) == \
                (heir.url, heir.secret, heir.disabled_reason)
            while not old.queue.empty():
                queued = old.queue.get_nowait()
                if same_target and queued[0] in heir.events and not heir.disabled_reason:
                    try:
                        heir.queue.put_nowait(queued)
                        continue
                    except asyncio.QueueFull:
                        pass
                (heir or old).dropped += 1
                self._count(old, "dropped")
        self.hooks = kept
        for hook in kept:
            self._start_worker(hook)

    async def _run(self, hook: Webhook) -> None:
        while True:
            event_type, event_id, body = await hook.queue.get()
            try:
                await self.deliver(hook, event_type, event_id, body)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a bug here must not end the worker
                self._failed(hook, "internal error", event_type)

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
            return "retry", "timed out"
        except httpx.TransportError:
            return "retry", "could not connect"
        except (httpx.InvalidURL, ValueError):
            return "fail", "invalid address"
        # coarse on purpose (no code, no exception name): the status is a port scanner for whoever can set the address
        label = status_label(code)
        if 200 <= code < 300:
            return "ok", label
        if code in (408, 429) or code >= 500:
            return "retry", label
        return "fail", label

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
