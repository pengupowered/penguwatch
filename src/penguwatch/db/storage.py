"""The storage component: opens (migrates) the database, and closes it last. The first entry of COMPONENTS."""

from penguwatch.components import Component, Context
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import identity_providers
from penguwatch.ratings import stored_builtin_specs
from penguwatch.security import session_store_for


async def register(ctx: Context) -> Component:
    config = ctx.config
    repo = DatabaseRepository(db_path=config.database.db_path, key_providers=tuple(identity_providers(config.drivers.library, config.drivers.catalog)))
    await repo.initialize(default_services=config.services)
    roles = config.drivers
    await repo.seed_builtin_rating_sources(
        stored_builtin_specs([roles.library, *roles.catalog, *roles.schedule, *roles.discovery]))
    await repo.seed_rating_sources(config.ratings.sources)
    ctx.repository = repo
    # the guards were built at import; the data directory is only known now (sign-outs are kept next to the database)
    ctx.security.session_store = session_store_for(config.database.db_path)
    return Component("storage", stop=repo.close)
