"""REST API routes, one module per domain. `ROUTERS` lists the routers that `router.py` mounts, in mounting order
(which decides which route wins when two paths overlap). `titles` and `ical` are helpers of other modules and have
no routes."""

from penguwatch.api import (
    catalog, decide, discover, drivers, exports, insights, notifications, planner, ratings, schedule, services, shows,
    sync_review, system, upnext, webhooks,
)

ROUTERS = [system.router, shows.router, upnext.router, services.router, decide.router, schedule.router,
           discover.router, catalog.router, drivers.router, sync_review.router, notifications.router, exports.router,
           insights.router, planner.router, ratings.router,
           webhooks.router]
