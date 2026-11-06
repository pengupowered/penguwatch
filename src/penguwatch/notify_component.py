"""The notifications component: the daily digest worker. Optional: a failure here costs the digest, not the app."""

import logging

from penguwatch.components import Component, Context
from penguwatch.notify import Notifier, notify_worker

OPTIONAL = True
logger = logging.getLogger("penguwatch")


async def register(ctx: Context) -> Component:
    notifier = ctx.notifier = ctx.container.notifier = Notifier.from_env(ctx.env)

    async def start() -> None:
        if notifier.enabled:
            hour = ctx.env.notify_hour
            ctx.spawn(notify_worker(ctx.repository, ctx.optimizer, notifier, hour=hour))
            logger.info(f"Notifications enabled ({notifier.kind}); daily check at {hour}:00.")

    return Component("notifications", start=start)
