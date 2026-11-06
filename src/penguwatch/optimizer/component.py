"""The optimizer component."""

from penguwatch.components import Component, Context
from penguwatch.optimizer.engine import SubscriptionOptimizer


async def register(ctx: Context) -> Component:
    ctx.optimizer = SubscriptionOptimizer(repository=ctx.repository, config=ctx.config.optimizer)
    return Component("optimizer")
