BEGIN TRANSACTION;
CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO "app_state" VALUES('sync_review_mode','on');
INSERT INTO "app_state" VALUES('simkl_client_id','placeholder-client-id');
INSERT INTO "app_state" VALUES('simkl_access_token','placeholder-access-token');
INSERT INTO "app_state" VALUES('simkl_refresh_token','placeholder-refresh-token');
INSERT INTO "app_state" VALUES('simkl_token_expires_at','2026-11-20T10:00:00+00:00');
INSERT INTO "app_state" VALUES('simkl_token_renewed_at','2026-11-13T10:00:00+00:00');
INSERT INTO "app_state" VALUES('simkl_renewal_note','Automatic renewal failed; reconnect before the login expires on Nov 20, 2026.');
INSERT INTO "app_state" VALUES('simkl_scope','media:read media:write');
INSERT INTO "app_state" VALUES('simkl_last_sync','2026-11-05T09:00:00+00:00');
INSERT INTO "app_state" VALUES('simkl_last_error','SIMKL HTTP 503: down');
INSERT INTO "app_state" VALUES('simkl_activities','{"all": "2026-11-05T08:00:00Z", "tv_shows": {"all": "2026-11-05T08:00:00Z"}}');
INSERT INTO "app_state" VALUES('simkl_refresh_attempts','{"enrich:2": "2026-11-05T08:00:00+00:00"}');
INSERT INTO "app_state" VALUES('schedule_last_sync','2026-11-05T09:00:00+00:00');
INSERT INTO "app_state" VALUES('incoming_staged','{"activities": {"all": "x"}, "at": "2026-11-05T09:00:00+00:00", "entries": [{"key": "show:1002", "kind": "update", "media": "show", "simkl_id": 1002, "show_id": 2, "title": "Beta", "poster": null, "item": {"status": "dropped", "show": {"title": "Beta", "ids": {"simkl": 1002}}}, "changes": [{"field": "status", "local": "hold", "remote": "dropped"}]}]}');
INSERT INTO "app_state" VALUES('recs_cache_simkl','{"at": "2026-11-05T09:00:00+00:00", "items": [], "seeds": []}');
INSERT INTO "app_state" VALUES('trending:tv:week','{"at": "2026-11-05T09:00:00+00:00", "items": []}');
INSERT INTO "app_state" VALUES('tidy_dismissed_stale','{"2": "2027-01-01T00:00:00+00:00"}');
CREATE TABLE episodes (
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
INSERT INTO "episodes" VALUES(1,1,50001,1,1,'Ep 1','2026-01-01',1,'2026-01-05T20:00:00+00:00');
INSERT INTO "episodes" VALUES(2,1,50002,1,2,'Ep 2','2026-01-02',1,'2026-10-10T12:00:00+00:00');
INSERT INTO "episodes" VALUES(3,1,50003,1,3,'Ep 3','2026-01-03',1,'2026-10-10T12:00:00+00:00');
INSERT INTO "episodes" VALUES(4,1,50004,1,4,'Ep 4','2026-01-04',0,NULL);
INSERT INTO "episodes" VALUES(8,3,NULL,1,1,NULL,NULL,1,'2026-10-10T12:00:00+00:00');
INSERT INTO "episodes" VALUES(9,3,NULL,1,2,NULL,NULL,1,'2026-10-10T12:00:00+00:00');
INSERT INTO "episodes" VALUES(12,2,NULL,2,3,NULL,NULL,1,'2026-10-10T12:00:00+00:00');
CREATE TABLE rating_sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            scale_min REAL NOT NULL DEFAULT 0,
            scale_max REAL NOT NULL DEFAULT 10,
            decimals INTEGER NOT NULL DEFAULT 1,
            show_on_cards INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT 'api',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
INSERT INTO "rating_sources" VALUES('letterboxd','Letterboxd','LB',0.0,5.0,1,0,2,'api','2026-11-06 00:21:12','2026-11-06 00:21:12');
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
INSERT INTO "schema_migrations" VALUES(1,'initial','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(2,'legacy_columns','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(3,'episode_watched_index','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(4,'service_bundles','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(5,'billing_period','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(6,'service_price_history','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(7,'english_titles','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(8,'drop_tmdb_state','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(9,'clear_stale_providers','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(10,'neutral_household_tags','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(11,'drop_rotten_tomatoes','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(12,'service_titles','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(13,'drop_partner_rating','2026-11-06 00:21:12');
INSERT INTO "schema_migrations" VALUES(14,'rating_sources','2026-11-06 00:21:12');
CREATE TABLE service_price_history (id INTEGER PRIMARY KEY, service_id TEXT NOT NULL, monthly_cost REAL NOT NULL, effective_from TEXT NOT NULL);
INSERT INTO "service_price_history" VALUES(1,'netflix',15.49,'2026-10-10T12:00:00+00:00');
CREATE TABLE services (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    monthly_cost REAL DEFAULT 0.0,
    is_active INTEGER DEFAULT 0,
    renewal_day INTEGER,
    renewal_date TEXT,
    aliases_json TEXT DEFAULT '[]',
    notes TEXT
, includes_json TEXT DEFAULT '[]', billing_period TEXT DEFAULT 'monthly', titles_json TEXT DEFAULT '[]');
INSERT INTO "services" VALUES('netflix','Netflix',15.49,0,NULL,NULL,'["Netflix"]',NULL,'[]','monthly','[]');
CREATE TABLE shows (
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
    updated_at TEXT DEFAULT (datetime('now')), en_title TEXT, alt_titles_json TEXT, search_text TEXT,
    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE SET NULL
);
INSERT INTO "shows" VALUES(1,1001,'tt0001001',501,701,'show','Alpha',NULL,NULL,NULL,NULL,'[]',NULL,NULL,0,'watching','medium','person1',8.3,8.1,1200,8.0,4,3,4,45,NULL,NULL,NULL,NULL,0,'S01E03','S01E04',NULL,'2026-10-10T12:00:00+00:00','2026-10-10T12:00:00+00:00',NULL,'[{"simkl_id": 2002, "title": "Gamma 2", "year": 2026, "poster": null, "anime_type": "tv"}]','2026-10-10T12:00:00+00:00','2026-10-10T12:00:00+00:00',NULL,NULL,'alpha

alpha');
INSERT INTO "shows" VALUES(2,1002,'tt0001002',NULL,NULL,'show','Beta',NULL,NULL,NULL,NULL,'[]',NULL,NULL,0,'watching','medium','person1',NULL,NULL,NULL,NULL,10,1,10,45,NULL,NULL,NULL,NULL,0,'S02E03','S02E04',NULL,'2026-10-10T12:00:00+00:00',NULL,NULL,NULL,'2026-10-10T12:00:00+00:00','2026-10-10T12:00:00+00:00',NULL,NULL,'beta

beta');
INSERT INTO "shows" VALUES(3,2001,NULL,601,NULL,'anime','Gamma',NULL,NULL,NULL,NULL,'[]',NULL,NULL,0,'completed','medium','person1',NULL,NULL,NULL,9.0,12,2,12,45,NULL,NULL,NULL,NULL,0,'S01E02','S01E03',NULL,'2026-10-10T12:00:00+00:00',NULL,NULL,NULL,'2026-10-10T12:00:00+00:00','2026-10-10T12:00:00+00:00',NULL,NULL,'gamma

gamma');
INSERT INTO "shows" VALUES(4,3001,'tt0003001',801,NULL,'movie','Delta',NULL,NULL,NULL,NULL,'[]',NULL,NULL,0,'plantowatch','medium','person1',NULL,NULL,NULL,8.0,1,0,1,45,NULL,NULL,NULL,NULL,0,NULL,NULL,NULL,'2026-10-10T12:00:00+00:00',NULL,NULL,NULL,'2026-10-10T12:00:00+00:00','2026-10-10T12:00:00+00:00',NULL,NULL,'delta

delta');
INSERT INTO "shows" VALUES(5,3002,NULL,NULL,NULL,'movie','Epsilon',NULL,NULL,NULL,NULL,'[]',NULL,NULL,0,'plantowatch','medium','person1',NULL,NULL,NULL,NULL,1,0,1,45,NULL,NULL,NULL,NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-10-10T12:00:00+00:00','2026-10-10T12:00:00+00:00',NULL,NULL,'epsilon

epsilon');
INSERT INTO "shows" VALUES(6,NULL,NULL,901,NULL,'show','Local only',NULL,NULL,NULL,NULL,'[]',NULL,NULL,0,'hold','medium','person1',NULL,NULL,NULL,NULL,0,0,0,45,NULL,NULL,NULL,NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-10-10T12:00:00+00:00','2026-10-10T12:00:00+00:00',NULL,NULL,'local only

localonly');
CREATE TABLE sync_outbox (
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
INSERT INTO "sync_outbox" VALUES(1,1,1001,'history_add','{"shows": [{"ids": {"simkl": 1001, "imdb": "tt0001001", "tmdb": 501}, "seasons": [{"number": 1, "episodes": [{"number": 2, "watched_at": "2026-10-10T12:00:00+00:00"}, {"number": 3, "watched_at": "2026-10-10T12:00:00+00:00"}]}]}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Mark S01E02, S01E03 watched','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(2,3,2001,'history_add','{"shows": [{"ids": {"simkl": 2001, "tmdb": 601}, "seasons": [{"number": 1, "episodes": [{"number": 1, "watched_at": "2026-10-10T12:00:00+00:00"}, {"number": 2, "watched_at": "2026-10-10T12:00:00+00:00"}]}]}]}',2,'SIMKL HTTP 503: down','2099-01-01T00:00:00+00:00',1,'Mark S01E01, S01E02 watched','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(3,4,3001,'history_add','{"movies": [{"ids": {"simkl": 3001, "imdb": "tt0003001", "tmdb": 801}, "watched_at": "2026-10-10T12:00:00+00:00"}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Mark movie watched','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(4,2,1002,'list_move','{"to": "hold", "shows": [{"ids": {"simkl": 1002, "imdb": "tt0001002"}, "to": "hold"}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Set status to On Hold','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(5,1,1001,'rating_set','{"shows": [{"ids": {"simkl": 1001, "imdb": "tt0001001", "tmdb": 501}, "rating": 8}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Rate 8/10','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(6,4,3001,'rating_set','{"movies": [{"ids": {"simkl": 3001, "imdb": "tt0003001", "tmdb": 801}, "rating": 8}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Rate 8/10','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(7,2,1002,'rating_remove','{"shows": [{"ids": {"simkl": 1002, "imdb": "tt0001002"}}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Remove rating','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(8,1,1001,'history_remove','{"shows": [{"ids": {"simkl": 1001, "imdb": "tt0001001", "tmdb": 501}, "seasons": [{"number": 1, "episodes": [{"number": 3}]}]}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Mark S01E03 unwatched','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(9,5,3002,'history_remove','{"movies": [{"ids": {"simkl": 3002}}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Mark movie unwatched','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(10,3,2001,'list_move','{"to": "completed", "shows": [{"ids": {"simkl": 2001, "tmdb": 601}, "to": "completed"}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Set status to Completed','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(11,4,3001,'list_move','{"to": "plantowatch", "movies": [{"ids": {"simkl": 3001, "imdb": "tt0003001", "tmdb": 801}, "to": "plantowatch"}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Set status to Plan to Watch','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(12,5,3002,'remove_item','{"movies": [{"ids": {"simkl": 3002}}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Remove from SIMKL (lists and watch history)','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(13,1,1001,'history_add','{"shows": [{"ids": {"simkl": 1001, "imdb": "tt0001001", "tmdb": 501}, "seasons": [{"number": 1, "episodes": [{"number": 3, "watched_at": "2026-10-10T12:00:00+00:00"}]}]}]}',0,NULL,'2026-10-10T12:00:00+00:00',1,'Mark S01E03 watched','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(14,2,1002,'history_add','{"shows": [{"ids": {"simkl": 1002, "imdb": "tt0001002"}, "seasons": [{"number": 2, "episodes": [{"number": 3, "watched_at": "2026-10-10T12:00:00+00:00"}]}]}]}',0,NULL,'2026-10-10T12:00:00+00:00',0,'Mark S02E03 watched','2026-11-06 00:21:12');
INSERT INTO "sync_outbox" VALUES(15,3,2001,'rating_set','{"shows": [{"ids": {"simkl": 2001, "tmdb": 601}, "rating": 9}]}',0,NULL,'2026-10-10T12:00:00+00:00',0,'Rate 9/10','2026-11-06 00:21:12');
CREATE TABLE title_ratings (
            show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES rating_sources(id) ON DELETE CASCADE,
            value REAL NOT NULL,
            votes INTEGER,
            url TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (show_id, source_id)
        );
INSERT INTO "title_ratings" VALUES(1,'letterboxd',4.5,12,'https://example.org/a','2026-10-10T12:00:00+00:00');
INSERT INTO "title_ratings" VALUES(4,'letterboxd',3.0,NULL,NULL,'2026-10-10T12:00:00+00:00');
CREATE INDEX idx_shows_status ON shows(status);
CREATE INDEX idx_shows_service ON shows(service_id);
CREATE INDEX idx_shows_priority ON shows(priority);
CREATE INDEX idx_shows_household ON shows(household_tag);
CREATE INDEX idx_shows_simkl_id ON shows(simkl_id);
CREATE INDEX idx_episodes_show_id ON episodes(show_id);
CREATE INDEX idx_episodes_air_date ON episodes(air_date);
CREATE INDEX idx_outbox_simkl ON sync_outbox(simkl_id);
CREATE INDEX idx_episodes_watched ON episodes(is_watched, watched_at);
CREATE INDEX idx_price_history_service ON service_price_history(service_id, effective_from);
CREATE INDEX idx_title_ratings_source_value ON title_ratings(source_id, value);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('shows',6);
INSERT INTO "sqlite_sequence" VALUES('episodes',12);
INSERT INTO "sqlite_sequence" VALUES('sync_outbox',15);
COMMIT;
