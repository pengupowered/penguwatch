"""Push notifications (ntfy or a generic JSON webhook) for the things worth interrupting you for.

Events (each sent at most once, remembered in app_state):
  * a paid service renews within N days and the optimizer says it's unused or will be finished in time
  * an episode of a show on a service you have right now airs today
  * the outbox (changes for the library driver) has had failing items for over an hour

Configure with env vars:
  PENGUWATCH_NOTIFY_URL   ntfy topic URL (https://ntfy.sh/<topic>) or a webhook URL
  PENGUWATCH_NOTIFY_KIND  ntfy (default) | webhook
  PENGUWATCH_NOTIFY_HOUR  local hour of the daily check, default 9
"""

from penguwatch import clock, telemetry
from penguwatch.webhooks import GuardedTransport, failure_label
from penguwatch.config import EnvOverrides, NotifyConfig
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("penguwatch.notify")

RENEWAL_WARNING_DAYS = 3  # the default of notify.renewal_warning_days; the digest reads the setting


@dataclass
class Event:
    key: str  # stable id: the same event is never sent twice
    title: str
    message: str
    priority: int = 3  # 1 (min) .. 5 (max), ntfy semantics
    tags: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "title": self.title, "message": self.message, "priority": self.priority, "tags": self.tags}


class Notifier:
    def __init__(self, url: str = "", kind: str = "ntfy", timeout: float = 10.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.transport = transport  # tests; None = GuardedTransport
        self.url = (url or "").strip()
        self.kind = (kind or "ntfy").lower()
        self.timeout = timeout
        self.changed = asyncio.Event()  # set by reconfigure: the worker looks again at once instead of in 10 minutes

    def reconfigure(self, url: str, kind: str) -> None:
        """Takes a new address and kind while running (settings applier notify.channel). Never logs the address."""
        self.url = (url or "").strip()
        self.kind = (kind or "ntfy").lower()
        self.changed.set()

    @classmethod
    def from_env(cls, env: EnvOverrides) -> "Notifier":
        return cls(env.notify_url, env.notify_kind)

    @property
    def enabled(self) -> bool:
        return self.url.startswith(("http://", "https://"))

    async def send(self, event: Event) -> None:
        if not self.enabled:
            raise RuntimeError("notifications are not configured")
        # the guarded transport refuses to connect to this machine or a link-local address (also after DNS), and no
        # proxy variable can route around it
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport or GuardedTransport(),
                                     trust_env=False) as client:
            if self.kind == "ntfy":
                # Header values must be latin-1; titles with other characters are percent-safe via ntfy's JSON API
                headers = {"Title": event.title.encode("ascii", "replace").decode(), "Priority": str(event.priority),
                           "Tags": ",".join(event.tags)}
                resp = await client.post(self.url, content=event.message.encode("utf-8"), headers=headers)
            else:
                resp = await client.post(self.url, json=event.as_dict())
            resp.raise_for_status()


async def build_events(repo, optimizer, today: Optional[date] = None,
                       renewal_warning_days: int = RENEWAL_WARNING_DAYS) -> List[Event]:
    today = today or clock.today()
    events: List[Event] = []

    plan = await optimizer.calculate_plan()
    for r in plan.service_rankings:
        if not r.is_active or r.renewal_in_days is None or r.renewal_in_days > renewal_warning_days:
            continue
        if r.verdict == "unused":
            events.append(Event(
                key=f"renewal:{r.service_id}:{r.next_renewal}",
                title=f"{r.service_name} renews in {r.renewal_in_days} day(s)",
                message=r.verdict_text or f"Nothing watched on {r.service_name} in {plan.value_window_days} days. Cancel before it renews?",
                priority=4, tags=["moneybag", "warning"],
            ))
        elif r.verdict == "finish_before_renewal":
            events.append(Event(
                key=f"renewal:{r.service_id}:{r.next_renewal}",
                title=f"{r.service_name}: cancel before {r.next_renewal}",
                message=r.verdict_text or "",
                priority=3, tags=["moneybag"],
            ))

    for item in await repo.get_upcoming_schedule(days_ahead=0):
        if (item.get("next_episode_air_date") or "")[:10] != today.isoformat() or not item.get("is_service_active"):
            continue
        s, n = item.get("next_episode_season") or 1, item.get("next_episode_number") or 1
        events.append(Event(
            key=f"air:{item['id']}:{s}:{n}",
            title=f"New today: {item['title']} S{s:02d}E{n:02d}",
            message=f"{item.get('next_episode_title') or ''} on {item.get('service_name') or item.get('network') or 'your service'}".strip(),
            priority=3, tags=["tv"],
        ))

    snap = await repo.get_system_snapshot()
    ob = snap["outbox"]
    if (ob.get("failing") or 0) > 0 and ob.get("oldest"):
        try:
            oldest = datetime.fromisoformat(ob["oldest"].replace(" ", "T")).replace(tzinfo=timezone.utc)
            if clock.utc_now() - oldest > timedelta(hours=1):
                events.append(Event(
                    key=f"sync-failing:{today.isoformat()}",
                    title="Sync is failing",
                    message=f"{ob['failing']} change(s) could not be delivered. Open PenguWatch > System for the error.",
                    priority=4, tags=["rotating_light"],
                ))
        except ValueError:
            pass
    return events


async def deliver_new(repo, notifier: Notifier, events: List[Event]) -> int:
    """Sends events that haven't been sent before; returns how many were delivered."""
    sent = 0
    for ev in events:
        marker = f"notified:{ev.key}"
        if await repo.get_state(marker):
            continue
        try:
            await notifier.send(ev)
        except Exception as e:
            telemetry.NOTIFICATIONS.add(1, {"outcome": "failed"})
            # not the error's text: an HTTP error names the URL, and an ntfy topic URL is as good as a password
            logger.warning(f"Notification '{ev.key}' failed: {failure_label(e)}")
            continue
        await repo.set_state(marker, clock.utc_now().isoformat())
        telemetry.NOTIFICATIONS.add(1, {"outcome": "sent"})
        sent += 1
    return sent


async def notify_worker(repo, optimizer, notifier: Notifier, hour: int = 9, config: Optional[NotifyConfig] = None,
                        poll_seconds: float = 600) -> None:
    """Runs the daily check once per day, at or after the hour, local time. With `config` (the live
    `config.notify`) the hour and the renewal warning days are read again on every round and the worker always runs:
    it checks `notifier.enabled` each round, so a digest address set (or cleared) on the Settings page counts at once.
    Without it, `hour` is fixed (the old call)."""
    last_day: Optional[str] = None
    while True:
        try:
            notifier.changed.clear()  # (before the checks: a reconfigure during them makes the wait below return)
            now = clock.now()
            at = config.hour if config is not None else hour
            warn = config.renewal_warning_days if config is not None else RENEWAL_WARNING_DAYS
            if notifier.enabled and now.hour >= at and last_day != now.date().isoformat():
                with telemetry.span("notify.daily_check") as sp:
                    n = await deliver_new(repo, notifier, await build_events(repo, optimizer, renewal_warning_days=warn))
                    sp.set_attribute("penguwatch.notify.sent", n)
                logger.info(f"Daily notification check: {n} sent")
                last_day = now.date().isoformat()
            try:
                await asyncio.wait_for(notifier.changed.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            # the type only: an HTTP error's text names the address, and an ntfy topic URL is as good as a password
            logger.error(f"Notification worker error: {type(e).__name__}")
            logger.debug("Notification worker error", exc_info=True)
            await asyncio.sleep(poll_seconds)
