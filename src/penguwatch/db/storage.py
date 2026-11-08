"""The storage component: opens (migrates) the database, and closes it last. The first entry of COMPONENTS.

It reads only the bootstrap config (database.db_path) and seeds nothing: the settings component, right after it,
imports config.yaml once, hands its services and rating sources over to their tables, and seeds what depends on the
effective drivers (the built-in rating sources, the repository's identity providers)."""

from penguwatch.components import Component, Context
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import identity_providers
from penguwatch.security import session_store_for


async def register(ctx: Context) -> Component:
    config = ctx.config
    repo = DatabaseRepository(db_path=config.database.db_path, key_providers=tuple(identity_providers(config.drivers.library, config.drivers.catalog)))
    await repo.initialize()
    ctx.repository = repo
    # the guards were built at import; the data directory is only known now (sign-outs are kept next to the database)
    ctx.security.session_store = session_store_for(config.database.db_path)
    return Component("storage", stop=repo.close)
