"""SQLite layer: schema, connection handling, settings access."""
import json
import os
import sqlite3
import threading

DATA_DIR = os.environ.get("CF_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DATA_DIR, "channelforge.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    epg_url TEXT DEFAULT '',
    stream_format TEXT NOT NULL DEFAULT 'HLS',          -- HLS | MPEG-TS
    priority INTEGER NOT NULL DEFAULT 100,              -- lower wins
    active INTEGER NOT NULL DEFAULT 1,
    check_streams INTEGER NOT NULL DEFAULT 0,
    last_fetched TEXT DEFAULT '',
    last_status TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS channels (                   -- canonical lineup entries ("parents")
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    number TEXT DEFAULT '',
    gracenote_id TEXT DEFAULT '',
    tvg_id TEXT DEFAULT '',
    logo TEXT DEFAULT '',
    grp TEXT DEFAULT '',
    description TEXT DEFAULT '',
    preferred_source_id INTEGER DEFAULT NULL,
    attrs TEXT NOT NULL DEFAULT '{}'                    -- extra tvc-* overrides as JSON
);
CREATE INDEX IF NOT EXISTS idx_channels_name ON channels(name);

CREATE TABLE IF NOT EXISTS source_channels (            -- channels as they appear in a source ("children")
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,                          -- channel-id / tvg-id / url hash within the source
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}',                   -- parsed EXTINF attributes as JSON
    channel_id INTEGER DEFAULT NULL REFERENCES channels(id) ON DELETE SET NULL,
    ignored INTEGER NOT NULL DEFAULT 0,
    stream_format_override TEXT DEFAULT '',
    present INTEGER NOT NULL DEFAULT 1,                 -- still in the source's latest fetch
    healthy INTEGER NOT NULL DEFAULT 1,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_checked TEXT DEFAULT '',
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_sc_channel ON source_channels(channel_id);
CREATE INDEX IF NOT EXISTS idx_sc_unassigned ON source_channels(channel_id) WHERE channel_id IS NULL AND ignored = 0;

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1,
    source_id INTEGER DEFAULT NULL,                     -- NULL = all sources
    match_field TEXT NOT NULL DEFAULT 'name',           -- name|tvg_id|tvg_name|group|url|external_id|any
    match_type TEXT NOT NULL DEFAULT 'equals',          -- equals|not_equals|contains|not_contains|starts|ends|regex|not_regex
    pattern TEXT NOT NULL,
    action TEXT NOT NULL,                               -- assign|ignore|set_field
    target_channel_id INTEGER DEFAULT NULL REFERENCES channels(id) ON DELETE CASCADE,
    set_field TEXT DEFAULT '',
    set_value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',             -- running|done|failed
    started TEXT NOT NULL,
    finished TEXT DEFAULT '',
    log TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_SETTINGS = {
    "channels_dvr_url": "",                    # one or more, comma separated, e.g. http://192.168.1.10:8089
    "push_outputs_to_dvr": "0",                # 1 = re-register output m3us in DVR after each refresh
    "output_max_per_m3u": "1200",
    "output_start_number": "",                 # auto-number channels from here (persisted per channel); blank = keep source numbers
    "health_fail_threshold": "3",
    "health_concurrency": "20",
    "schedule.refresh": "",                    # HH:MM daily, blank = off
    "schedule.health": "",
    "schedule.reset_passes": "",
    "schedule.refresh_dvr_m3u": "",
    "base_url": "",                            # external URL of this app for m3u links; blank = derive from request
    "auto_rule_on_assign": "1",                # manual assign/ignore also creates a matching equals rule
}


def connect():
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init():
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        # jobs left 'running' by a previous process are dead; don't let them wedge the UI
        conn.execute("UPDATE jobs SET status = 'failed', log = log || 'interrupted by restart\n' WHERE status = 'running'")


def q(sql, params=()):
    return connect().execute(sql, params).fetchall()


def q1(sql, params=()):
    return connect().execute(sql, params).fetchone()


def execute(sql, params=()):
    conn = connect()
    with conn:
        return conn.execute(sql, params)


def executemany(sql, rows):
    conn = connect()
    with conn:
        conn.executemany(sql, rows)


def get_setting(key, default=""):
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def attrs_of(row):
    try:
        return json.loads(row["attrs"] or "{}")
    except (ValueError, TypeError):
        return {}
