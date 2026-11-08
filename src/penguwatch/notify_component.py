"""The notifications component: the daily digest worker. Optional: a failure here costs the digest, not the app."""

import logging

from penguwatch.components import Component, Context
from penguwatch.notify import Notifier, notify_worker

OPTIONAL = True
logger = logging.getLogger("penguwatch")


def digest_url(ctx: Context) -> str:
    """The digest address: the settings manager's slot `notify.url` (the variable PENGUWATCH_NOTIFY_URL wins there,
    else secrets.json); the variable alone when there is no manager. Never logged."""
    manager = ctx.settings_manager
    if manager is not None:
        return manager.secret_value("notify.url") or ""
    return ctx.env.notify_url if ctx.env is not None else ""


async def register(ctx: Context) -> Component:
    # The kind and the hour are settings (notify.kind, notify.hour) that PENGUWATCH_NOTIFY_KIND / _HOUR override
    # (config.apply_env). The notifier is always built and the worker always runs: it checks `notifier.enabled`, the
    # hour and the renewal warning days on every round, so the Settings page can turn the digest on and off.
    notifier = ctx.notifier = ctx.container.notifier = Notifier(digest_url(ctx), ctx.config.notify.kind)

    if ctx.settings is not None:
        @ctx.settings.on(keys=["notify.kind", "notify.hour"], slots=["notify.url"], name="notify.channel")
        def _channel(change) -> None:
            # the hour and the renewal days are read live from the config; the hour is listed only so that the
            # worker looks again at once (reconfigure wakes it)
            notifier.reconfigure(url=digest_url(ctx), kind=ctx.config.notify.kind)

    async def start() -> None:
        ctx.spawn(notify_worker(ctx.repository, ctx.optimizer, notifier, config=ctx.config.notify))
        if notifier.enabled:
            logger.info(f"Notifications enabled ({notifier.kind}); daily check at {ctx.config.notify.hour}:00.")

    return Component("notifications", start=start)
