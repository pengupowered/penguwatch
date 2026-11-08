"""Build a made-up demo library for screenshots and trying PenguWatch out.

    PYTHONPATH=src python scripts/demo_library.py demo.db [--signed-in]

Every title, network pairing and rating here is invented; posters are generated SVG artwork (data: URLs, so the page
needs no network). Air dates are relative to today, so Up Next and the Air Schedule always have something to show.
The library also has what the other views need for a good picture: changes waiting for review in Services & Sync (three
outgoing, two incoming), Tidy suggestions (a show with a new season, a title with no service, a stalled one), two custom
rating sources and your own ratings.
Start the app on the result with PENGUWATCH_DB_PATH=demo.db.

--signed-in stores a made-up SIMKL app id, token and sync state, so the page shows SIMKL as connected and the review
changes can be opened. None of it is real: SIMKL would refuse the token, so use it only for pictures and for trying the
page out with SIMKL out of reach (the app then reports a sync problem at its next sync).
"""

import asyncio
import base64
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from penguwatch.catalog import load_catalog
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers.base import OP_STATUS, OP_WATCHED, OP_RATE, SyncOp
from penguwatch.models import HouseholdTag, MediaType, Priority, Show, ShowStatus
from penguwatch.ratings import RatingSourceSpec, RatingValue

# (title, year, kind, network, status, household, priority, simkl, imdb, seasons x episodes, watched, genres)
TITLES = [
    ("The Quiet Harbor", 2024, "show", "Netflix", "watching", "both", "high", 8.4, 8.2, (2, 8), 13, ["Drama", "Mystery"]),
    ("Signal Lost", 2023, "show", "HBO", "watching", "person1", "high", 8.1, 8.0, (3, 10), 24, ["Thriller", "Sci-Fi"]),
    ("Paper Moons", 2025, "show", "Hulu", "watching", "person2", "medium", 7.6, 7.4, (1, 10), 6, ["Comedy"]),
    ("Northbound", 2022, "show", "Apple TV+", "watching", "both", "high", 8.6, 8.5, (3, 9), 22, ["Drama", "Adventure"]),
    ("Kitchen Lights", 2024, "show", "PBS", "watching", "person2", "low", 7.2, 7.1, (2, 6), 7, ["Reality", "Food"]),
    ("Brass Meridian", 2025, "show", "Prime Video", "watching", "person1", "medium", 7.9, 7.7, (1, 8), 3, ["Sci-Fi"]),
    ("Starlit Ramen", 2024, "anime", "Tokyo MX", "watching", "both", "high", 8.3, 8.1, (1, 12), 9, ["Anime", "Slice of Life"]),
    ("Sky Forge Academy", 2025, "anime", "Crunchyroll", "watching", "person1", "medium", 7.8, 7.6, (2, 12), 15, ["Anime", "Action"]),
    ("Hollow Pines", 2021, "show", "AMC", "plantowatch", "both", "high", 8.0, 7.9, (2, 8), 0, ["Horror", "Mystery"]),
    ("Static Bloom", 2025, "show", "Disney+", "plantowatch", "person2", "medium", 7.5, 7.3, (1, 8), 0, ["Fantasy"]),
    ("Tidewater", 2023, "show", "BBC One", "plantowatch", "person1", "medium", 7.7, 7.8, (2, 6), 0, ["Crime", "Drama"]),
    ("Neon Ferry", 2024, "show", "Peacock", "plantowatch", "both", "low", 7.1, 6.9, (1, 10), 0, ["Comedy", "Crime"]),
    ("Velvet Circuit", 2022, "show", "Paramount+", "hold", "person1", "low", 7.0, 7.2, (2, 10), 11, ["Drama"]),
    ("Moonlit Blade Sisters", 2023, "anime", "Crunchyroll", "plantowatch", "both", "medium", 8.2, 8.0, (1, 13), 0, ["Anime", "Action"]),
    ("Pocket Dragon Delivery", 2024, "anime", "Tokyo MX", "completed", "person2", "medium", 7.9, 7.8, (1, 12), 12, ["Anime", "Comedy"]),
    ("Fox & Compass", 2020, "show", "Netflix", "completed", "both", "medium", 8.3, 8.4, (3, 8), 16, ["Adventure", "Family"]),
    ("The Lantern Keepers", 2019, "show", "HBO", "completed", "person1", "high", 8.7, 8.8, (4, 10), 40, ["Drama"]),
    ("Orbit of Small Things", 2021, "show", "Apple TV+", "dropped", "person2", "low", 6.4, 6.6, (1, 10), 3, ["Sci-Fi"]),
    ("The Last Cartographer", 2024, "movie", None, "plantowatch", "both", "high", 7.8, 7.6, None, 0, ["Adventure"]),
    ("Paper Planets", 2023, "movie", None, "completed", "person2", "medium", 7.3, 7.1, None, 1, ["Animation"]),
    ("Quiet Thunder", 2025, "movie", None, "plantowatch", "person1", "medium", 7.0, 6.8, None, 0, ["Action"]),
    ("Winter Signal", 2022, "movie", None, "completed", "both", "low", 6.9, 7.0, None, 1, ["Thriller"]),
    ("Lighthouse Row", 2024, "show", "Acme Plus", "watching", "person2", "medium", 7.4, 7.2, (1, 8), 2, ["Drama"]),
]
# What the account's activity listing said at the last (made-up) sync
DEMO_ACTIVITY = {"all": "2020-01-01T00:00:00Z"}
# Titles nobody has watched for months (Tidy offers to move a stalled one to Plan to watch; a service only they are on
# stays unused, so the Optimizer has something to advise)
STALE = {"Kitchen Lights": 330, "Velvet Circuit": 120}
# Titles whose first episodes were marked watched in one go (bulk marks: n episodes sharing one timestamp, that many
# days ago); the History tab leaves such groups out of the watching time
BULK = {"Fox & Compass": (8, 200), "Pocket Dragon Delivery": (12, 45), "Velvet Circuit": (4, 120)}
# Your own ratings (SIMKL's 1-10 scale)
USER_RATINGS = {"Fox & Compass": 9, "The Lantern Keepers": 10, "Pocket Dragon Delivery": 8, "Paper Planets": 7,
                "Winter Signal": 6, "Northbound": 9, "Signal Lost": 8}
ACTIVE = ["netflix", "max", "apple_tv", "crunchyroll", "paramount_plus"]  # the last one nothing current is on
PALETTE = [("#1b3a5c", "#5aa2ff"), ("#3a1f4d", "#9d8cff"), ("#123d36", "#2bb5a0"), ("#4a2a12", "#ec835a"),
           ("#2b2f45", "#8cc0ff"), ("#40142a", "#e87ba4"), ("#1d3b1f", "#6fcf73"), ("#3d3510", "#f0ad1c")]


def poster(title: str) -> str:
    """A 2:3 SVG poster: a gradient chosen by the title, a moon and hills (no text: the cards crop posters)."""
    h = int(hashlib.sha1(title.encode()).hexdigest(), 16)
    dark, light = PALETTE[h % len(PALETTE)]
    cx, cy = 60 + h % 90, 70 + (h >> 8) % 50
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300" width="200" height="300">'
           f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{light}"/>'
           f'<stop offset="1" stop-color="{dark}"/></linearGradient></defs>'
           f'<rect width="200" height="300" fill="url(#g)"/>'
           f'<circle cx="{cx}" cy="{cy}" r="26" fill="#f4f7fb" fill-opacity=".85"/>'
           f'<path d="M0 210 Q50 160 100 195 T200 180 V300 H0Z" fill="{dark}" fill-opacity=".9"/></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def watch_stamp(today: date, days_ago: int, hour: int, n: int) -> str:
    """A made-up watch time `days_ago` days back, in the shapes the real library has: with a Z, with +00:00 and
    microseconds, and now and then a date only."""
    day = today - timedelta(days=days_ago)
    kind = n % 7
    if kind == 6:
        return day.isoformat()
    minute, second = (n * 13) % 60, (n * 29) % 60
    base = f"{day.isoformat()}T{hour % 24:02d}:{minute:02d}:{second:02d}"
    return base + "Z" if kind % 2 == 0 else base + f".{n * 7919 % 1000000:06d}+00:00"


async def build(path: str, signed_in: bool = False) -> None:
    repo = DatabaseRepository(path)
    # the built-in catalogue, as a first start seeds an empty table (config.yaml no longer lists services)
    await repo.initialize(load_catalog())
    for sid in ACTIVE:
        await repo.set_service_active(sid, True, renewal_day=sum(map(ord, sid)) % 27 + 1)
    today = date.today()
    ids = []
    for i, (title, year, kind, network, status, who, prio, simkl, imdb, shape, watched, genres) in enumerate(TITLES):
        media = {"show": MediaType.SHOW, "anime": MediaType.ANIME, "movie": MediaType.MOVIE}[kind]
        show = Show(title=title, year=year, media_type=media, network=network, status=ShowStatus(status),
                    household_tag=HouseholdTag(who), priority=Priority(prio), simkl_rating=simkl, imdb_rating=imdb,
                    imdb_votes=1000 + 137 * i, poster=poster(title), runtime_minutes=24 if kind == "anime" else (110 if kind == "movie" else 45),
                    genres=genres, overview=f"{title} is an invented title from the PenguWatch demo library.",
                    is_ended=status in ("completed", "dropped"), simkl_id=900000 + i)
        if kind == "movie":
            show.total_episodes, show.watched_episodes = 1, watched
        show_id = await repo.upsert_show(show)
        ids.append(show_id)
        if not shape:
            if kind == "movie" and watched:  # watched once, a while ago (Insights and History count it at this stamp)
                await repo.update_show_fields(show_id, {"last_watched_at": watch_stamp(today, 12 + (i * 28) % 60, 20, i)})
            continue
        seasons, per = shape
        total = seasons * per
        airing = status in ("watching", "plantowatch") and i % 3 != 2  # some still airing, some finished
        # Airing: the last three episodes are still to come, one a week, on a different weekday for each title.
        # Finished: the whole run is over.
        first = (today - timedelta(days=7 * (total - 3) - 1 - i % 6)) if airing else today - timedelta(days=7 * total + 30)
        episodes = [{"season": s, "number": n, "title": f"Chapter {n}",
                     "air_date": (first + timedelta(days=7 * ((s - 1) * per + n - 1))).isoformat()}
                    for s in range(1, seasons + 1) for n in range(1, per + 1)]
        await repo.upsert_episode_list(show_id, episodes)
        ago = STALE.get(title, 2)
        # Spread the watched episodes back from the newest (every title at its own pace) so the History tab has a
        # year to show; a bulk-marked start shares one stamp
        stamps = []
        bulk_n, bulk_ago = BULK.get(title, (0, 0))
        step = 2 + i % 5
        for k in range(watched):
            if k < bulk_n:
                stamps.append(watch_stamp(today, bulk_ago, 9, 0))
            else:
                stamps.append(watch_stamp(today, ago + (watched - 1 - k) * step, 18 + (k * 7 + i) % 5, k + i))
        await repo.replace_watched_episodes(show_id, [(e["season"], e["number"], at) for e, at in zip(episodes[:watched], stamps)])
        await repo.update_show_fields(show_id, {"episodes_synced_at": today.isoformat() + "T00:00:00+00:00",
                                                "total_episodes": total,
                                                "last_watched_at": max(stamps) if watched else None})
        await repo.recompute_progress_from_episodes(show_id)
    for k, (title, *_rest) in enumerate(TITLES):
        if title in USER_RATINGS:
            await repo.update_show_fields(ids[k], {"user_rating": float(USER_RATINGS[title])})
    # Changes waiting for review (visible when SIMKL is the library driver): three of yours not sent yet...
    by_title = {t[0]: (k, ids[k]) for k, t in enumerate(TITLES)}
    waiting = [("Signal Lost", SyncOp(OP_WATCHED, "show", {"simkl": 900000 + by_title["Signal Lost"][0]},
                                      episodes=[(3, 5)], watched_at=today.isoformat() + "T20:00:00+00:00")),
               ("Hollow Pines", SyncOp(OP_STATUS, "show", {"simkl": 900000 + by_title["Hollow Pines"][0]}, status="watching")),
               ("Northbound", SyncOp(OP_RATE, "show", {"simkl": 900000 + by_title["Northbound"][0]}, rating=9))]
    for title, op in waiting:
        index, show_id = by_title[title]
        await repo.enqueue_outbox("simkl", op, show_id, 900000 + index, approved=False)
    # ...and two found on the account: a title that is not here yet, and one watched further on the other device
    others = by_title["Paper Moons"]
    incoming = [
        {"key": "show:931001", "media": "show", "ids": {"simkl": 931001}, "title": "Copper Tides", "poster": poster("Copper Tides"),
         "item": None, "kind": "new", "show_id": None, "changes": [{"field": "status", "local": None, "remote": "plantowatch"}]},
        {"key": f"show:{900000 + others[0]}", "media": "show", "ids": {"simkl": 900000 + others[0]}, "title": "Paper Moons",
         "poster": poster("Paper Moons"), "item": None, "kind": "update", "show_id": others[1],
         "changes": [{"field": "watched", "local": 6, "remote": 9}]},
    ]
    await repo.set_state("incoming_staged", json.dumps({"cursor": DEMO_ACTIVITY, "at": today.isoformat() + "T08:00:00+00:00",
                                                         "entries": incoming}))
    # Custom rating sources, as a user would add with the Ratings tab or PUT /api/ratings/critics
    await repo.create_rating_source(RatingSourceSpec(id="critics", name="Critics' circle", label="CC", scale_min=0,
                                                     scale_max=100, decimals=0, show_on_cards=True))
    await repo.set_title_ratings("critics", {sid: RatingValue(value=55 + (k * 37) % 44) for k, sid in enumerate(ids)
                                             if k % 4 != 3}, events="summary")
    await repo.create_rating_source(RatingSourceSpec(id="club", name="Weekend club", label="WC", scale_min=0,
                                                     scale_max=5, decimals=1, show_on_cards=False))
    await repo.set_title_ratings("club", {sid: RatingValue(value=round(2 + ((k * 7) % 7) / 2, 1)) for k, sid in enumerate(ids)
                                          if k % 3 == 0}, events="summary")
    if signed_in:
        now = datetime.now(timezone.utc).isoformat()
        await repo.set_state("driver.simkl.client_id", "demo-app-id-not-real")
        await repo.set_state("driver.simkl.access_token", "demo-token-not-real")
        await repo.set_state("driver.simkl.refresh_token", "demo-refresh-not-real")  # the page says it renews itself
        await repo.set_state("driver.simkl.token_expires_at", (datetime.now(timezone.utc) + timedelta(days=60)).isoformat())
        await repo.set_state("driver.simkl.token_renewed_at", now)
        await repo.set_state("driver.simkl.cursor", json.dumps(DEMO_ACTIVITY))  # "nothing changed since the last sync"
        await repo.set_state("driver.simkl.last_sync", now)
        await repo.set_state("schedule_last_sync", now)
    await repo.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--signed-in"]
    if len(args) != 1 or Path(args[0]).exists():
        sys.exit("usage: demo_library.py <new database file> [--signed-in]")
    asyncio.run(build(args[0], "--signed-in" in sys.argv[1:]))
    print(f"demo library written to {args[0]} ({len(TITLES)} titles)")
