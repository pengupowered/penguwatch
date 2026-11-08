"""The webhooks component: reads `webhooks:` from config.yaml, resolves each secret, connects the dispatcher to the
data changes and runs the deliveries. Optional: a failure here costs the webhooks, not the app."""

import asyncio
from datetime import timedelta
import logging
import os
import re
import stat
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

from penguwatch import clock
from penguwatch.components import Component, Context
from penguwatch.config import WebhookConfig
from penguwatch.events import title_ref
from penguwatch.webhooks import MIN_SECRET_LENGTH, Webhook, WebhookDispatcher

OPTIONAL = True
logger = logging.getLogger("penguwatch.webhooks")

AIRS_CHECK_SECONDS = 900
_MAX_SECRET_FILE = 4096
_ENV_NAME_SHOWN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_FILE_SHOWN = re.compile(r"/[A-Za-z0-9_./-]{1,200}")  # a path is named only when it looks like one


Stored = Callable[[str], Optional[str]]  # the settings manager's secret_value(slot)


def resolve_secret(cfg: WebhookConfig, environ: Optional[Mapping[str, str]] = None,
                   stored: Optional[Stored] = None) -> Tuple[Optional[bytes], Optional[str]]:
    """(secret, None), (None, None) for an unsigned webhook, or (None, why it cannot run). The reason names the
    variable or file, never a value. The order: `secret_env`, else `secret_file`, else the stored slot
    `webhook.<id>.secret` (through `stored`)."""
    environ = os.environ if environ is None else environ
    if cfg.secret_env:
        value = (environ.get(cfg.secret_env) or "").strip()
        # the name is shown only when it looks like one: a secret pasted into secret_env must not reach the System tab
        shown = cfg.secret_env if _ENV_NAME_SHOWN.fullmatch(cfg.secret_env) else None
        source = f"environment variable {shown}" if shown else "the secret_env setting"
        if not shown:
            return None, "the secret_env setting is not a variable name"
        if not value:
            return None, f"{source} is not set"
    elif cfg.secret_file:
        source = f"secret file {cfg.secret_file}" if _FILE_SHOWN.fullmatch(cfg.secret_file) else "the secret_file setting"
        try:
            path = Path(cfg.secret_file)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                return None, f"{source} is not a regular file"
            if info.st_size > _MAX_SECRET_FILE:
                return None, f"{source} is too large"
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError, ValueError):  # ValueError: a NUL in the path
            return None, f"{source} cannot be read"
        if not value:
            return None, f"{source} is empty"
    else:
        value = ((stored(f"webhook.{cfg.id}.secret") or "").strip() if stored and cfg.id else "")
        if not value:
            return None, None
        source = "the stored secret"
    if len(value) < MIN_SECRET_LENGTH:
        return None, f"the secret in {source} is shorter than {MIN_SECRET_LENGTH} characters"
    return value.encode("utf-8"), None


def build_hooks(configs, environ: Optional[Mapping[str, str]] = None, stored: Optional[Stored] = None):
    """One Webhook per config. A failure on one config (a bad path, a bug) turns off that hook only: it is returned
    disabled with a fixed reason, and the others are built as usual."""
    hooks = []
    for cfg in configs:
        name = cfg.name or cfg.id or "webhook"
        try:
            secret, why = resolve_secret(cfg, environ, stored)
            if why:
                # configured to be signed but cannot be: it must not send unsigned, so it stays off
                logger.error(f"Webhook '{name}' is disabled: {why}.")
            hooks.append(Webhook(name=cfg.name, url=cfg.url, events=frozenset(cfg.events), secret=secret,
                                 disabled_reason=why, id=cfg.id))
        except Exception as e:  # the type only: the message of a bad path or address can quote it
            logger.error(f"Webhook '{name}' is disabled: internal error ({type(e).__name__}).")
            hooks.append(Webhook(name=cfg.name or name, url="", events=frozenset(), secret=None,
                                 disabled_reason="the webhook could not be set up", id=cfg.id))
    return hooks


async def airs_today_worker(repo, dispatcher: WebhookDispatcher, interval: float = AIRS_CHECK_SECONDS) -> None:
    """Publishes episode.airs_today once per episode, the first time it is seen airing today."""
    while True:
        try:
            await publish_airs_today(repo, dispatcher)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"episode.airs_today check failed: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


async def publish_airs_today(repo, dispatcher: WebhookDispatcher) -> int:
    if not dispatcher.subscribers("episode.airs_today"):
        return 0
    today = clock.today().isoformat()
    sent = 0
    for item in await repo.get_upcoming_schedule(days_ahead=0):
        if (item.get("next_episode_air_date") or "")[:10] != today:
            continue
        season, number = item.get("next_episode_season") or 1, item.get("next_episode_number") or 1
        marker = f"webhook:airs:{item['id']}:{season}:{number}:{today}"
        if await repo.get_state(marker):
            continue
        await repo.set_state(marker, today)
        show = await repo.get_show_by_id(item["id"])
        dispatcher.publish("episode.airs_today", {
            "title": title_ref({"id": show.id, "title": show.title, "year": show.year, "media_type": show.media_type.value},
                               show.ids) if show else {"id": item["id"], "title": item["title"]},
            "season": season, "number": number, "episode_title": item.get("next_episode_title"),
            "air_date": today, "service": item.get("service_name") or item.get("network"),
            "service_active": bool(item.get("is_service_active")),
        })
        sent += 1
    # the markers only matter for the day they are made
    await repo.delete_states_before("webhook:airs:", (clock.today() - timedelta(days=2)).isoformat())
    return sent


async def register(ctx: Context) -> Component:
    manager = ctx.settings_manager
    stored = manager.secret_value if manager is not None else None
    dispatcher = WebhookDispatcher(build_hooks(ctx.config.webhooks, stored=stored))
    ctx.webhooks = ctx.container.webhooks = dispatcher
    # Always installed: the hooks can change while running (webhooks.hooks below), and publish() with no hook that
    # wants the event does nothing
    ctx.repository.add_event_listener(dispatcher.publish)

    if ctx.settings is not None:
        @ctx.settings.on(lists=["webhooks"], slots=["webhook."], name="webhooks.hooks")
        def _hooks(change) -> None:
            # No log line here: a hook is named by its name only, and build_hooks logs why one stays off
            dispatcher.set_hooks(build_hooks(ctx.config.webhooks, stored=stored), ctx.spawn)

    async def start() -> None:
        dispatcher.start(ctx.spawn)
        ctx.spawn(airs_today_worker(ctx.repository, dispatcher))  # a no-op each round until a hook wants the event
        active = [h for h in dispatcher.hooks if not h.disabled_reason]
        if active:
            logger.info(f"Webhooks enabled: {', '.join(h.name for h in active)}.")

    async def stop() -> None:
        ctx.repository.remove_event_listener(dispatcher.publish)
        await dispatcher.close()

    return Component("webhooks", start=start, stop=stop)
