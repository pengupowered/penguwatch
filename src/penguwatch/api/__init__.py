"""REST API routes, one module per domain. `ROUTERS` lists the routers that `router.py` mounts, in mounting order
(which decides which route wins when two paths overlap). `titles` and `ical` are helpers of other modules and have
no routes."""

from penguwatch.api import (
    catalog, decide, discover, drivers, exports, history, insights, notifications, planner, ratings, schedule, services,
    settings, shows, sync_review, system, themes, upnext, webhooks,
)

ROUTERS = [system.router, shows.router, upnext.router, services.router, decide.router, schedule.router,
           discover.router, catalog.router, drivers.router, sync_review.router, notifications.router, exports.router,
           history.router, insights.router, planner.router, ratings.router,
           themes.router, webhooks.router, settings.router]
