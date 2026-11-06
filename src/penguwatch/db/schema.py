"""Columns added to the first schema after the first release (migration 0002 adds the ones an older database lacks).

The first schema itself is migration 0001's (db/migrations/0001_initial.py); every later change is a numbered
migration."""


# Columns added after the first release. Migration 0002 (db/migrations) adds any that are
# missing from an older database, checking PRAGMA table_info first.
SHOW_COLUMN_MIGRATIONS = [
    "last_watched TEXT",
    "next_to_watch TEXT",
    "media_type TEXT DEFAULT 'show'",
    "service_manual INTEGER DEFAULT 0",
    "episodes_synced_at TEXT",
    "enriched_at TEXT",
    "sequels_json TEXT",
]

OUTBOX_COLUMN_MIGRATIONS = [
    "approved INTEGER DEFAULT 0",
    "summary TEXT",
]
