"""The container component: what the request handlers read, installed on the app once the parts it holds exist."""

from penguwatch.api.deps import AppContainer, install
from penguwatch.components import Component, Context


async def register(ctx: Context) -> Component:
    ctx.container = AppContainer(
        config=ctx.config, repository=ctx.repository, drivers=ctx.drivers, sync=ctx.sync,
        optimizer=ctx.optimizer, security=ctx.security, settings=ctx.settings_manager,
    )
    install(ctx.app, ctx.container)
    return Component("container")
