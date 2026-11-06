"""0001: create all tables and indexes (every statement is IF NOT EXISTS), as the first release had them. Later
migrations change them (0015 moved the external ids to title_ids, 0016 made the outbox driver-neutral)."""

import sqlite3

import aiosqlite



CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    monthly_cost REAL DEFAULT 0.0,
    is_active INTEGER DEFAULT 0,
    renewal_day INTEGER,
    renewal_date TEXT,
    aliases_json TEXT DEFAULT '[]',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    simkl_id INTEGER UNIQUE,
    imdb_id TEXT,
    tmdb_id INTEGER,
    tvdb_id INTEGER,
    media_type TEXT DEFAULT 'show',
    title TEXT NOT NULL,
    year INTEGER,
    poster TEXT,
    banner TEXT,
    overview TEXT,
    genres_json TEXT DEFAULT '[]',
    network TEXT,
    service_id TEXT,
    service_manual INTEGER DEFAULT 0,
    status TEXT DEFAULT 'plantowatch',
    priority TEXT DEFAULT 'medium',
    household_tag TEXT DEFAULT 'both',
    simkl_rating REAL,
    imdb_rating REAL,
    imdb_votes INTEGER,
    user_rating REAL,
    total_episodes INTEGER DEFAULT 0,
    watched_episodes INTEGER DEFAULT 0,
    aired_episodes INTEGER DEFAULT 0,
    runtime_minutes INTEGER DEFAULT 45,
    next_episode_air_date TEXT,
    next_episode_season INTEGER,
    next_episode_number INTEGER,
    next_episode_title TEXT,
    is_ended INTEGER DEFAULT 0,
    last_watched TEXT,
    next_to_watch TEXT,
    notes TEXT,
    last_watched_at TEXT,
    episodes_synced_at TEXT,
    enriched_at TEXT,
    sequels_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL,
    simkl_id INTEGER,
    season INTEGER NOT NULL,
    number INTEGER NOT NULL,
    title TEXT,
    air_date TEXT,
    is_watched INTEGER DEFAULT 0,
    watched_at TEXT,
    FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE,
    UNIQUE(show_id, season, number)
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Local changes waiting to be pushed to SIMKL. Rows are deleted once SIMKL accepts them,
-- so nothing is lost if SIMKL is down or the container restarts.
CREATE TABLE IF NOT EXISTS sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER,
    simkl_id INTEGER,
    op TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    next_attempt_at TEXT,
    -- 0 = waiting for the user to approve it (review mode), 1 = OK to send
    approved INTEGER DEFAULT 0,
    -- human readable description, e.g. "Mark S02E03 watched"
    summary TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_shows_status ON shows(status);
CREATE INDEX IF NOT EXISTS idx_shows_service ON shows(service_id);
CREATE INDEX IF NOT EXISTS idx_shows_priority ON shows(priority);
CREATE INDEX IF NOT EXISTS idx_shows_household ON shows(household_tag);
CREATE INDEX IF NOT EXISTS idx_shows_simkl_id ON shows(simkl_id);
CREATE INDEX IF NOT EXISTS idx_episodes_show_id ON episodes(show_id);
CREATE INDEX IF NOT EXISTS idx_episodes_air_date ON episodes(air_date);
CREATE INDEX IF NOT EXISTS idx_outbox_simkl ON sync_outbox(simkl_id);
"""


def split_statements(script: str) -> list:
    """Split a SQL script into complete statements (handles -- comments)."""
    statements = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip() and sqlite3.complete_statement(buf + ";"):
        statements.append(buf.strip())
    return statements


async def migrate(conn: aiosqlite.Connection) -> None:
    for statement in split_statements(CREATE_TABLES_SQL):
        await conn.execute(statement)
