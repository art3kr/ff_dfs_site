import os
import sys
from dotenv import load_dotenv
load_dotenv()
import json
import click
from flask import Flask, render_template, g, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import bcrypt

# ---------------------------------------------------------------------------
# Database driver selection
# Prod (Render): DATABASE_URL starts with "postgres" → use psycopg2
# Local dev:     DATABASE_URL is a file path or unset   → use sqlite3
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "ff_dfs.db")

def _is_postgres():
    return DATABASE_URL.startswith("postgres")

if _is_postgres():
    import psycopg2
    import psycopg2.extras   # for RealDictCursor
else:
    import sqlite3


def _connect():
    """Open a raw DB connection (used in _auto_init and CLI commands)."""
    if _is_postgres():
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        conn = sqlite3.connect(DATABASE_URL)
        conn.row_factory = sqlite3.Row
        return conn


def _cursor(conn):
    """Return a dict-style cursor appropriate for the driver."""
    if _is_postgres():
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        return conn.cursor()


def _ph(n=1):
    """
    Return parameter placeholder(s) for the active driver.
    SQLite uses ?, Postgres uses %s.
    n=1 → single placeholder string; n>1 → comma-joined string of n placeholders.
    """
    ph = "%s" if _is_postgres() else "?"
    return ", ".join([ph] * n) if n > 1 else ph


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

SALARY_CAP = 50_000


def normalize_name(name: str) -> str:
    """
    Matches the exact normalization used by the scrapers (scrape_pfr.py,
    scrape_rotoguru.py, etc.) so names in a submitted lineup_json can be
    joined against hist_player_stats the same way History/Player pages
    already do.
    """
    import re
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


# DST names in submitted lineups can be in wildly different formats
# depending on which salary source was used at ingest time ("HOU DST",
# "Seahawks", "SEA", "Kansas City Chiefs", ...) — normalize_team()
# resolves any of these to the canonical PFR abbreviation used in
# hist_dst_stats.team.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scrapers'))
from team_mapping import normalize_team

ROSTER_SLOTS = {
    "QB":   (["QB"],                     1),
    "RB":   (["RB"],                     2),
    "WR":   (["WR"],                     3),
    "TE":   (["TE"],                     1),
    "FLEX": (["RB", "WR", "TE"],         1),
    "DST":  (["DST", "D", "DEF"],        1),
}

# Each team's real primary brand color, used as a low-opacity row tint
# on Best Matchups so players from the same team are visually grouped
# at a glance. Kept as an rgba() string directly (rather than a hex +
# separate opacity) so the template can drop it straight into an
# inline style with no further conversion. Alpha kept low (0.10-0.14)
# deliberately — full-saturation brand colors would be visually loud
# and hurt text contrast against the dark theme; this is meant as a
# subtle grouping cue, not a bold color block. Keyed by the canonical
# PFR-style abbreviation used everywhere else in this project (team_mapping's
# normalize_team() output), not raw brand/ESPN abbreviations.
TEAM_ROW_COLORS = {
    "ari": "rgba(151, 35, 63, 0.13)",     # Cardinals — cardinal red
    "atl": "rgba(167, 25, 48, 0.13)",     # Falcons — red
    "bal": "rgba(36, 23, 115, 0.14)",     # Ravens — purple
    "buf": "rgba(0, 51, 141, 0.13)",      # Bills — royal blue
    "car": "rgba(0, 133, 202, 0.12)",     # Panthers — blue
    "chi": "rgba(200, 56, 3, 0.13)",      # Bears — orange
    "cin": "rgba(251, 79, 20, 0.12)",     # Bengals — orange
    "cle": "rgba(255, 60, 0, 0.12)",      # Browns — orange
    "dal": "rgba(134, 147, 151, 0.14)",   # Cowboys — silver
    "den": "rgba(0, 34, 68, 0.16)",       # Broncos — navy
    "det": "rgba(0, 118, 182, 0.13)",     # Lions — blue
    "gnb": "rgba(32, 55, 49, 0.16)",      # Packers — green
    "hou": "rgba(167, 25, 48, 0.12)",     # Texans — red
    "ind": "rgba(0, 44, 95, 0.14)",       # Colts — blue
    "jax": "rgba(215, 162, 42, 0.14)",    # Jaguars — gold
    "kan": "rgba(227, 24, 55, 0.13)",     # Chiefs — red
    "lac": "rgba(0, 128, 198, 0.13)",     # Chargers — powder blue
    "lar": "rgba(255, 163, 0, 0.14)",     # Rams — gold
    "lvr": "rgba(165, 172, 175, 0.14)",   # Raiders — silver
    "mia": "rgba(0, 142, 151, 0.13)",     # Dolphins — aqua
    "min": "rgba(79, 38, 131, 0.13)",     # Vikings — purple
    "nwe": "rgba(198, 12, 48, 0.12)",     # Patriots — red
    "nor": "rgba(211, 188, 141, 0.16)",   # Saints — old gold
    "nyg": "rgba(11, 34, 101, 0.14)",     # Giants — blue
    "nyj": "rgba(18, 87, 64, 0.14)",      # Jets — green
    "phi": "rgba(0, 76, 84, 0.16)",       # Eagles — midnight green
    "pit": "rgba(255, 182, 18, 0.14)",    # Steelers — gold
    "sea": "rgba(105, 190, 40, 0.13)",    # Seahawks — action green
    "sfo": "rgba(170, 0, 0, 0.13)",       # 49ers — red
    "tam": "rgba(213, 10, 10, 0.12)",     # Buccaneers — red
    "ten": "rgba(75, 146, 219, 0.13)",    # Titans — light blue
    "was": "rgba(90, 20, 20, 0.16)",      # Commanders — burgundy
}

# ---------------------------------------------------------------------------
# Auto-init: create tables + optional bootstrap user on startup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Schema migration for hist_player_stats
#
# CREATE TABLE IF NOT EXISTS only handles brand-new databases — it does
# NOTHING to a table that already exists with an older schema, even if
# we've since added new columns to the CREATE TABLE statement above. This
# bit us: a table created early in development (before game_date,
# fumbles_rec_td, kick_ret*/punt_ret*, and dk_pts_pfr_reported existed)
# stayed stuck on that old shape on Render's Postgres, causing every
# insert to fail with "column does not exist" once load-history tried to
# write those newer fields.
#
# These functions add any missing columns to an already-existing table,
# so schema changes made here going forward apply automatically on next
# startup — no more manually dropping tables when we add a column.
# ---------------------------------------------------------------------------

# Columns added to hist_player_stats after its original creation.
# (column_name, SQL type) — keep this in sync whenever a new column is
# added to the CREATE TABLE statements above.
HIST_PLAYER_STATS_MIGRATIONS = [
    ("game_date",           "TEXT"),
    ("dk_pts_pfr_reported", "REAL"),
    ("fumbles_rec_td",      "INTEGER"),
    ("kick_ret",            "INTEGER"),
    ("kick_ret_yds",        "INTEGER"),
    ("kick_ret_td",         "INTEGER"),
    ("punt_ret",            "INTEGER"),
    ("punt_ret_yds",        "INTEGER"),
    ("punt_ret_td",         "INTEGER"),
]

GAME_SCHEDULE_MIGRATIONS = [
    ("home_away", "TEXT"),
]


def _migrate_table_pg(cur, table_name, migrations):
    """Add any missing columns to `table_name` on Postgres."""
    for col, coltype in migrations:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col} {coltype}")


def _migrate_table_sqlite(conn, table_name, migrations):
    """Add any missing columns to `table_name` on SQLite (no IF NOT
    EXISTS support for ADD COLUMN, so check existing columns first)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    for col, coltype in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {coltype}")


def _migrate_hist_player_stats_pg(cur):
    _migrate_table_pg(cur, "hist_player_stats", HIST_PLAYER_STATS_MIGRATIONS)


def _migrate_hist_player_stats_sqlite(conn):
    _migrate_table_sqlite(conn, "hist_player_stats", HIST_PLAYER_STATS_MIGRATIONS)


def _auto_init():
    """
    Creates tables if they don't exist and optionally creates a first user
    from BOOTSTRAP_USER / BOOTSTRAP_PASS env vars.
    Safe to run on every startup (all statements are idempotent).
    """
    conn = _connect()
    cur  = _cursor(conn)

    if _is_postgres():
        # Postgres: SERIAL for autoincrement, NOW() for timestamp
        #
        # This whole branch runs on EVERY startup of app.py — including
        # every gunicorn worker on Render, and any CLI command (like
        # `flask load-history`) run alongside the live app. If two of
        # these happen to start at the same moment, they can both try
        # to ALTER/CREATE the same tables simultaneously and deadlock
        # (confirmed: this actually happened — "DeadlockDetected...
        # AccessExclusiveLock on relation... blocked by process...").
        # An advisory lock makes the second process simply WAIT for the
        # first to finish instead of colliding — after which its own
        # CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS calls
        # are safe no-ops, since the first process already did the work.
        cur.execute("SELECT pg_advisory_lock(918273645)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       SERIAL PRIMARY KEY,
                username TEXT   NOT NULL UNIQUE,
                password TEXT   NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id              SERIAL PRIMARY KEY,
                week            INTEGER NOT NULL,
                year            INTEGER NOT NULL,
                name            TEXT    NOT NULL,
                position        TEXT,
                team            TEXT,
                opponent        TEXT,
                salary          INTEGER,
                projected_pts   REAL,
                ownership_pct   REAL,
                UNIQUE(week, year, name)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lineups (
                id           SERIAL PRIMARY KEY,
                week         INTEGER NOT NULL,
                year         INTEGER NOT NULL,
                submitter    TEXT    NOT NULL,
                lineup_json  TEXT    NOT NULL,
                total_salary INTEGER NOT NULL,
                submitted_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(week, year, submitter)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prop_bets (
                id                      SERIAL PRIMARY KEY,
                year                    INTEGER NOT NULL,
                week                    INTEGER NOT NULL,
                player_name             TEXT    NOT NULL,
                player_name_normalized  TEXT    NOT NULL,
                stat_field              TEXT    NOT NULL,
                line                    REAL    NOT NULL,
                created_at              TIMESTAMP DEFAULT NOW(),
                UNIQUE(year, week, player_name_normalized, stat_field)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_prop_bets_lookup
                ON prop_bets (year, week)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prop_picks (
                id           SERIAL PRIMARY KEY,
                year         INTEGER NOT NULL,
                week         INTEGER NOT NULL,
                submitter    TEXT    NOT NULL,
                prop_bet_id  INTEGER NOT NULL REFERENCES prop_bets(id),
                pick         TEXT    NOT NULL,
                submitted_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(year, week, submitter, prop_bet_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_prop_picks_lookup
                ON prop_picks (year, week, submitter)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hist_dfs_salaries (
                id              SERIAL PRIMARY KEY,
                week            INTEGER NOT NULL,
                year            INTEGER NOT NULL,
                name            TEXT    NOT NULL,
                name_normalized TEXT    NOT NULL,
                position        TEXT,
                team            TEXT,
                opponent        TEXT,
                home_away       TEXT,
                dk_salary       INTEGER,
                dk_pts_scored   REAL,
                projected_pts   REAL,
                ownership_pct   REAL,
                source          TEXT,
                UNIQUE(week, year, name_normalized, source)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_salaries_lookup
                ON hist_dfs_salaries (year, week, name_normalized)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hist_player_stats (
                id              SERIAL PRIMARY KEY,
                pfr_id          TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                name_normalized TEXT    NOT NULL,
                year            INTEGER NOT NULL,
                week            INTEGER NOT NULL,
                team            TEXT,
                opponent        TEXT,
                home_away       TEXT,
                position        TEXT,
                game_date       TEXT,
                dk_pts          REAL,
                dk_pts_pfr_reported REAL,
                pass_cmp        INTEGER,
                pass_att        INTEGER,
                pass_yds        INTEGER,
                pass_td         INTEGER,
                pass_int        INTEGER,
                rush_att        INTEGER,
                rush_yds        INTEGER,
                rush_td         INTEGER,
                rec_tgt         INTEGER,
                rec             INTEGER,
                rec_yds         INTEGER,
                rec_td          INTEGER,
                fumbles_lost    INTEGER,
                fumbles_rec_td  INTEGER,
                kick_ret        INTEGER,
                kick_ret_yds    INTEGER,
                kick_ret_td     INTEGER,
                punt_ret        INTEGER,
                punt_ret_yds    INTEGER,
                punt_ret_td     INTEGER,
                snap_pct        REAL,
                UNIQUE(pfr_id, year, week)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_stats_lookup
                ON hist_player_stats (year, week, name_normalized)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_stats_player
                ON hist_player_stats (pfr_id)
        """)
        _migrate_hist_player_stats_pg(cur)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hist_dst_stats (
                id                SERIAL PRIMARY KEY,
                year              INTEGER NOT NULL,
                week              INTEGER NOT NULL,
                team              TEXT    NOT NULL,
                opponent          TEXT,
                points_allowed    INTEGER,
                dk_pts            REAL,
                sack              INTEGER,
                interception      INTEGER,
                fumble_rec        INTEGER,
                forced_fumble     INTEGER,
                def_td            INTEGER,
                safety            INTEGER,
                special_teams_td  INTEGER,
                UNIQUE(year, week, team)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_dst_lookup
                ON hist_dst_stats (year, week, team)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_schedule (
                id                SERIAL PRIMARY KEY,
                year              INTEGER NOT NULL,
                week              INTEGER NOT NULL,
                team              TEXT    NOT NULL,
                opponent          TEXT,
                home_away         TEXT,
                kickoff           TIMESTAMP NOT NULL,
                UNIQUE(year, week, team)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_game_schedule_lookup
                ON game_schedule (year, week, team)
        """)
        _migrate_table_pg(cur, "game_schedule", GAME_SCHEDULE_MIGRATIONS)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hist_weather (
                id             SERIAL PRIMARY KEY,
                year           INTEGER NOT NULL,
                week           INTEGER NOT NULL,
                game_date      TEXT,
                status         TEXT,
                away_team      TEXT    NOT NULL,
                home_team      TEXT    NOT NULL,
                away_score     INTEGER,
                home_score     INTEGER,
                temp_f         INTEGER,
                condition      TEXT,
                wind_mph       INTEGER,
                wind_direction TEXT,
                UNIQUE(year, week, away_team, home_team)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_weather_lookup
                ON hist_weather (year, week)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hist_game_info (
                id           SERIAL PRIMARY KEY,
                boxscore_url TEXT    NOT NULL,
                year         INTEGER NOT NULL,
                week         INTEGER NOT NULL,
                team_home    TEXT,
                team_away    TEXT,
                date         TEXT,
                time         TEXT,
                location     TEXT,
                won_toss     TEXT,
                won_ot_toss  TEXT,
                roof         TEXT,
                surface      TEXT,
                duration     TEXT,
                attendance   TEXT,
                vegas_line   TEXT,
                over_under   TEXT,
                temp         TEXT,
                humidity     TEXT,
                wind         TEXT,
                UNIQUE(boxscore_url)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_game_info_lookup
                ON hist_game_info (year, week)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hist_fantasy_points_against (
                id                     SERIAL PRIMARY KEY,
                year                   INTEGER NOT NULL,
                position               TEXT    NOT NULL,
                team                   TEXT    NOT NULL,
                games                  INTEGER,
                fantasy_pts            REAL,
                dk_pts                 REAL,
                fd_pts                 REAL,
                fantasy_pts_per_game   REAL,
                dk_pts_per_game        REAL,
                fd_pts_per_game        REAL,
                UNIQUE(year, position, team)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_fpa_lookup
                ON hist_fantasy_points_against (year, position)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hist_team_points (
                id             SERIAL PRIMARY KEY,
                year           INTEGER NOT NULL,
                week           INTEGER NOT NULL,
                team           TEXT    NOT NULL,
                opponent       TEXT,
                home_away      TEXT,
                points_scored  INTEGER,
                points_allowed INTEGER,
                UNIQUE(year, week, team)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hist_team_points_lookup
                ON hist_team_points (year, week)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS depth_charts (
                id                 SERIAL PRIMARY KEY,
                team               TEXT    NOT NULL,
                pos                TEXT    NOT NULL,
                string_rank        INTEGER NOT NULL,
                player_name        TEXT    NOT NULL,
                player_name_normalized TEXT NOT NULL,
                ourlads_player_id  TEXT,
                updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team, pos, string_rank)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_depth_charts_name
                ON depth_charts (player_name_normalized)
        """)
        cur.execute("SELECT pg_advisory_unlock(918273645)")
    else:
        # SQLite: executescript for multi-statement init
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT    NOT NULL UNIQUE,
                password TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS players (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                week            INTEGER NOT NULL,
                year            INTEGER NOT NULL,
                name            TEXT    NOT NULL,
                position        TEXT,
                team            TEXT,
                opponent        TEXT,
                salary          INTEGER,
                projected_pts   REAL,
                ownership_pct   REAL,
                UNIQUE(week, year, name)
            );
            CREATE TABLE IF NOT EXISTS lineups (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                week         INTEGER NOT NULL,
                year         INTEGER NOT NULL,
                submitter    TEXT    NOT NULL,
                lineup_json  TEXT    NOT NULL,
                total_salary INTEGER NOT NULL,
                submitted_at TEXT    DEFAULT (datetime('now')),
                UNIQUE(week, year, submitter)
            );
            CREATE TABLE IF NOT EXISTS prop_bets (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                year                    INTEGER NOT NULL,
                week                    INTEGER NOT NULL,
                player_name             TEXT    NOT NULL,
                player_name_normalized  TEXT    NOT NULL,
                stat_field              TEXT    NOT NULL,
                line                    REAL    NOT NULL,
                created_at              TEXT    DEFAULT (datetime('now')),
                UNIQUE(year, week, player_name_normalized, stat_field)
            );
            CREATE INDEX IF NOT EXISTS idx_prop_bets_lookup
                ON prop_bets (year, week);
            CREATE TABLE IF NOT EXISTS prop_picks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                year         INTEGER NOT NULL,
                week         INTEGER NOT NULL,
                submitter    TEXT    NOT NULL,
                prop_bet_id  INTEGER NOT NULL REFERENCES prop_bets(id),
                pick         TEXT    NOT NULL,
                submitted_at TEXT    DEFAULT (datetime('now')),
                UNIQUE(year, week, submitter, prop_bet_id)
            );
            CREATE INDEX IF NOT EXISTS idx_prop_picks_lookup
                ON prop_picks (year, week, submitter);
            CREATE TABLE IF NOT EXISTS hist_dfs_salaries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                week            INTEGER NOT NULL,
                year            INTEGER NOT NULL,
                name            TEXT    NOT NULL,
                name_normalized TEXT    NOT NULL,
                position        TEXT,
                team            TEXT,
                opponent        TEXT,
                home_away       TEXT,
                dk_salary       INTEGER,
                dk_pts_scored   REAL,
                projected_pts   REAL,
                ownership_pct   REAL,
                source          TEXT,
                UNIQUE(week, year, name_normalized, source)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_salaries_lookup
                ON hist_dfs_salaries (year, week, name_normalized);
            CREATE TABLE IF NOT EXISTS hist_player_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pfr_id          TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                name_normalized TEXT    NOT NULL,
                year            INTEGER NOT NULL,
                week            INTEGER NOT NULL,
                team            TEXT,
                opponent        TEXT,
                home_away       TEXT,
                position        TEXT,
                game_date       TEXT,
                dk_pts          REAL,
                dk_pts_pfr_reported REAL,
                pass_cmp        INTEGER,
                pass_att        INTEGER,
                pass_yds        INTEGER,
                pass_td         INTEGER,
                pass_int        INTEGER,
                rush_att        INTEGER,
                rush_yds        INTEGER,
                rush_td         INTEGER,
                rec_tgt         INTEGER,
                rec             INTEGER,
                rec_yds         INTEGER,
                rec_td          INTEGER,
                fumbles_lost    INTEGER,
                fumbles_rec_td  INTEGER,
                kick_ret        INTEGER,
                kick_ret_yds    INTEGER,
                kick_ret_td     INTEGER,
                punt_ret        INTEGER,
                punt_ret_yds    INTEGER,
                punt_ret_td     INTEGER,
                snap_pct        REAL,
                UNIQUE(pfr_id, year, week)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_stats_lookup
                ON hist_player_stats (year, week, name_normalized);
            CREATE INDEX IF NOT EXISTS idx_hist_stats_player
                ON hist_player_stats (pfr_id);
            CREATE TABLE IF NOT EXISTS hist_dst_stats (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                year              INTEGER NOT NULL,
                week              INTEGER NOT NULL,
                team              TEXT    NOT NULL,
                opponent          TEXT,
                points_allowed    INTEGER,
                dk_pts            REAL,
                sack              INTEGER,
                interception      INTEGER,
                fumble_rec        INTEGER,
                forced_fumble     INTEGER,
                def_td            INTEGER,
                safety            INTEGER,
                special_teams_td  INTEGER,
                UNIQUE(year, week, team)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_dst_lookup
                ON hist_dst_stats (year, week, team);
            CREATE TABLE IF NOT EXISTS game_schedule (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                year      INTEGER NOT NULL,
                week      INTEGER NOT NULL,
                team      TEXT    NOT NULL,
                opponent  TEXT,
                home_away TEXT,
                kickoff   TEXT    NOT NULL,
                UNIQUE(year, week, team)
            );
            CREATE INDEX IF NOT EXISTS idx_game_schedule_lookup
                ON game_schedule (year, week, team);
            CREATE TABLE IF NOT EXISTS hist_weather (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                year           INTEGER NOT NULL,
                week           INTEGER NOT NULL,
                game_date      TEXT,
                status         TEXT,
                away_team      TEXT    NOT NULL,
                home_team      TEXT    NOT NULL,
                away_score     INTEGER,
                home_score     INTEGER,
                temp_f         INTEGER,
                condition      TEXT,
                wind_mph       INTEGER,
                wind_direction TEXT,
                UNIQUE(year, week, away_team, home_team)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_weather_lookup
                ON hist_weather (year, week);
            CREATE TABLE IF NOT EXISTS hist_game_info (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                boxscore_url TEXT    NOT NULL,
                year         INTEGER NOT NULL,
                week         INTEGER NOT NULL,
                team_home    TEXT,
                team_away    TEXT,
                date         TEXT,
                time         TEXT,
                location     TEXT,
                won_toss     TEXT,
                won_ot_toss  TEXT,
                roof         TEXT,
                surface      TEXT,
                duration     TEXT,
                attendance   TEXT,
                vegas_line   TEXT,
                over_under   TEXT,
                temp         TEXT,
                humidity     TEXT,
                wind         TEXT,
                UNIQUE(boxscore_url)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_game_info_lookup
                ON hist_game_info (year, week);
            CREATE TABLE IF NOT EXISTS hist_fantasy_points_against (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                year                 INTEGER NOT NULL,
                position             TEXT    NOT NULL,
                team                 TEXT    NOT NULL,
                games                INTEGER,
                fantasy_pts          REAL,
                dk_pts               REAL,
                fd_pts               REAL,
                fantasy_pts_per_game REAL,
                dk_pts_per_game      REAL,
                fd_pts_per_game      REAL,
                UNIQUE(year, position, team)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_fpa_lookup
                ON hist_fantasy_points_against (year, position);
            CREATE TABLE IF NOT EXISTS hist_team_points (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                year           INTEGER NOT NULL,
                week           INTEGER NOT NULL,
                team           TEXT    NOT NULL,
                opponent       TEXT,
                home_away      TEXT,
                points_scored  INTEGER,
                points_allowed INTEGER,
                UNIQUE(year, week, team)
            );
            CREATE INDEX IF NOT EXISTS idx_hist_team_points_lookup
                ON hist_team_points (year, week);
            CREATE TABLE IF NOT EXISTS depth_charts (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                team               TEXT    NOT NULL,
                pos                TEXT    NOT NULL,
                string_rank        INTEGER NOT NULL,
                player_name        TEXT    NOT NULL,
                player_name_normalized TEXT NOT NULL,
                ourlads_player_id  TEXT,
                updated_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team, pos, string_rank)
            );
            CREATE INDEX IF NOT EXISTS idx_depth_charts_name
                ON depth_charts (player_name_normalized);
        """)
        _migrate_hist_player_stats_sqlite(conn)
        _migrate_table_sqlite(conn, "game_schedule", GAME_SCHEDULE_MIGRATIONS)

    conn.commit()

    # Bootstrap user from env vars
    bootstrap_user = os.environ.get("BOOTSTRAP_USER", "").strip()
    bootstrap_pass = os.environ.get("BOOTSTRAP_PASS", "").strip()
    if bootstrap_user and bootstrap_pass:
        ph = _ph()
        cur.execute(f"SELECT id FROM users WHERE username = {ph}", (bootstrap_user,))
        if not cur.fetchone():
            hashed = bcrypt.hashpw(bootstrap_pass.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                f"INSERT INTO users (username, password) VALUES ({_ph(2)})",
                (bootstrap_user, hashed)
            )
            conn.commit()
            app.logger.info(f"Bootstrap user '{bootstrap_user}' created.")
        else:
            app.logger.info(f"Bootstrap user '{bootstrap_user}' already exists, skipping.")

    cur.close()
    conn.close()


with app.app_context():
    _auto_init()

# ---------------------------------------------------------------------------
# Flask-Login
# ---------------------------------------------------------------------------

login_manager = LoginManager(app)
login_manager.login_view             = "login"
login_manager.login_message          = "Please log in to submit a lineup."
login_manager.login_message_category = "info"


class User(UserMixin):
    def __init__(self, id_, username):
        self.id       = id_
        self.username = username


@login_manager.user_loader
def load_user(user_id):
    conn = _connect()
    cur  = _cursor(conn)
    cur.execute(f"SELECT id, username FROM users WHERE id = {_ph()}", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return User(row["id"], row["username"])
    return None


# ---------------------------------------------------------------------------
# Per-request DB connection (Flask g)
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = _connect()
    return g.db


def db_fetchone(query, params=()):
    """Execute a SELECT and return one row as a dict (or None)."""
    cur = _cursor(get_db())
    cur.execute(query, params)
    return cur.fetchone()


def db_fetchall(query, params=()):
    """Execute a SELECT and return all rows as a list of dicts."""
    cur = _cursor(get_db())
    cur.execute(query, params)
    return cur.fetchall()


def db_execute(query, params=()):
    """Execute an INSERT/UPDATE/DELETE and commit."""
    db  = get_db()
    cur = _cursor(db)
    cur.execute(query, params)
    db.commit()
    return cur


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@app.cli.command("create-user")
@click.option("--username", required=True, prompt=True)
@click.option("--password", required=True, prompt=True, hide_input=True,
              confirmation_prompt=True)
def create_user_command(username, password):
    """Create a participant account."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = _connect()
    cur  = _cursor(conn)
    try:
        cur.execute(
            f"INSERT INTO users (username, password) VALUES ({_ph(2)})",
            (username, hashed)
        )
        conn.commit()
        click.echo(f"User '{username}' created.")
    except Exception as e:
        click.echo(f"ERROR: {e}", err=True)
    finally:
        cur.close()
        conn.close()


@app.cli.command("list-users")
def list_users_command():
    """List all registered participants."""
    conn = _connect()
    cur  = _cursor(conn)
    cur.execute("SELECT id, username FROM users ORDER BY username")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        click.echo("No users yet. Run: flask create-user")
    else:
        click.echo(f"{'ID':>4}  Username")
        click.echo("-" * 24)
        for r in rows:
            click.echo(f"{r['id']:>4}  {r['username']}")


@app.cli.command("delete-user")
@click.option("--username", required=True, prompt=True)
def delete_user_command(username):
    """Remove a participant account."""
    conn = _connect()
    cur  = _cursor(conn)
    cur.execute(f"DELETE FROM users WHERE username = {_ph()}", (username,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if deleted:
        click.echo(f"User '{username}' deleted.")
    else:
        click.echo(f"No user named '{username}' found.", err=True)


@app.cli.command("ingest-slate")
@click.option("--week",     required=True, type=int)
@click.option("--year",     default=2026,  type=int)
@click.option("--slate-id", default=None,  type=int)
def ingest_slate_command(week, year, slate_id):
    """Pull weekly DraftKings slate from RotoWire into the DB."""
    from scraper import fetch_slate_data, find_latest_slate

    click.echo(f"Ingesting slate for week {week}, {year}...")

    if slate_id is None:
        click.echo("Searching for latest Thu-Mon Classic slate...")
        slate_id = find_latest_slate()
        if slate_id is None:
            click.echo("ERROR: Could not find a valid slate. Pass --slate-id manually.", err=True)
            return

    click.echo(f"Using slate ID: {slate_id}")
    players = fetch_slate_data(slate_id, week, year)
    if not players:
        click.echo("ERROR: No player data returned from RotoWire.", err=True)
        return

    conn = _connect()
    cur  = _cursor(conn)
    inserted = skipped = 0

    for p in players:
        try:
            if _is_postgres():
                cur.execute("""
                    INSERT INTO players
                        (week, year, name, position, team, opponent, salary, projected_pts, ownership_pct)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (week, year, name) DO UPDATE SET
                        position      = EXCLUDED.position,
                        team          = EXCLUDED.team,
                        opponent      = EXCLUDED.opponent,
                        salary        = EXCLUDED.salary,
                        projected_pts = EXCLUDED.projected_pts,
                        ownership_pct = EXCLUDED.ownership_pct
                """, (p["week"], p["year"], p["name"], p["position"], p["team"],
                      p["opponent"], p["salary"], p["projected_pts"], p["ownership_pct"]))
            else:
                cur.execute("""
                    INSERT INTO players
                        (week, year, name, position, team, opponent, salary, projected_pts, ownership_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(week, year, name) DO UPDATE SET
                        position      = excluded.position,
                        team          = excluded.team,
                        opponent      = excluded.opponent,
                        salary        = excluded.salary,
                        projected_pts = excluded.projected_pts,
                        ownership_pct = excluded.ownership_pct
                """, (p["week"], p["year"], p["name"], p["position"], p["team"],
                      p["opponent"], p["salary"], p["projected_pts"], p["ownership_pct"]))
            inserted += 1
        except Exception as e:
            click.echo(f"  Skipped {p.get('name','?')}: {e}")
            skipped += 1

    conn.commit()
    cur.close()
    conn.close()
    click.echo(f"Done. {inserted} players upserted, {skipped} skipped.")


VALID_PROP_STAT_FIELDS = {
    'pass_cmp', 'pass_att', 'pass_yds', 'pass_td', 'pass_int',
    'rush_att', 'rush_yds', 'rush_td',
    'rec_tgt', 'rec', 'rec_yds', 'rec_td',
    'fumbles_lost', 'kick_ret_yds', 'kick_ret_td', 'punt_ret_yds', 'punt_ret_td',
}

# Composite props (e.g. "passing + rushing yards") sum two existing
# columns rather than reading one directly — we already have both
# underlying stats in hist_player_stats, so these are genuinely
# addressable, unlike props needing data we don't track at all (kicker
# stats, single-play "longest X" records, which would need a real new
# scraper). COALESCE guards against one side legitimately being NULL
# (e.g. a pure runner with no pass attempts that week) — without it,
# SQL's NULL-propagation would make the whole sum NULL and the prop
# would incorrectly show as pending forever.
COMPOSITE_PROP_STAT_FIELDS = {
    'pass_rush_yds': '(COALESCE(pass_yds, 0) + COALESCE(rush_yds, 0))',
    'rush_rec_yds':  '(COALESCE(rush_yds, 0) + COALESCE(rec_yds, 0))',
    # "Touchdowns" (anytime scorer) on scoresandodds is a moneyline
    # yes/no proposition, but it's mathematically equivalent to "over
    # 0.5 combined touchdowns" — auto-scorable the same way as the
    # yards composites above, just summing three columns instead of
    # two, with an implicit 0.5 line (the site shows odds, not an
    # explicit line, since it's framed as yes/no rather than over/under).
    'any_td': '(COALESCE(pass_td, 0) + COALESCE(rush_td, 0) + COALESCE(rec_td, 0))',
}

VALID_PROP_STAT_FIELDS = VALID_PROP_STAT_FIELDS | set(COMPOSITE_PROP_STAT_FIELDS.keys())


@app.cli.command("add-props")
@click.argument("csv_path", type=click.Path(exists=True))
@click.option("--year", required=True, type=int)
@click.option("--week", required=True, type=int)
def add_props_command(csv_path, year, week):
    """
    Load a week's prop bet slate from a CSV — the manually-curated
    ~20 props participants pick 5 of. Expected columns: player_name,
    stat_field, line (e.g. "Bijan Robinson, rec, 5.5").

    stat_field must be a real hist_player_stats column name (see
    VALID_PROP_STAT_FIELDS) — props auto-score once that week's real
    results are loaded via load-history, by comparing the actual value
    in that column against the line. No separate manual result entry
    needed. Composite prop types (e.g. "anytime TD" combining rush_td
    + rec_td) aren't supported yet — every prop must map to exactly
    one existing stat column.

    Safe to re-run for the same week (upserts on player+stat_field),
    e.g. to fix a typo in a line before anyone's picked yet.

    Example CSV:
        player_name,stat_field,line
        Bijan Robinson,rec,5.5
        Josh Allen,rush_td,0.5
    """
    import pandas as pd
    df = pd.read_csv(csv_path)

    required_cols = {'player_name', 'stat_field', 'line'}
    if not required_cols.issubset(df.columns):
        click.echo(f"ERROR: CSV must have columns {required_cols}. Found: {list(df.columns)}", err=True)
        return

    invalid_fields = set(df['stat_field'].unique()) - VALID_PROP_STAT_FIELDS
    if invalid_fields:
        click.echo(f"ERROR: unrecognized stat_field value(s): {invalid_fields}", err=True)
        click.echo(f"Valid options: {sorted(VALID_PROP_STAT_FIELDS)}", err=True)
        return

    conn = get_db()
    cur = conn.cursor()
    ph = _ph(6)

    if _is_postgres():
        sql = f"""
            INSERT INTO prop_bets (year, week, player_name, player_name_normalized, stat_field, line)
            VALUES ({ph})
            ON CONFLICT (year, week, player_name_normalized, stat_field) DO UPDATE SET
                line = EXCLUDED.line
        """
    else:
        sql = f"""
            INSERT INTO prop_bets (year, week, player_name, player_name_normalized, stat_field, line)
            VALUES ({ph})
            ON CONFLICT(year, week, player_name_normalized, stat_field) DO UPDATE SET
                line = excluded.line
        """

    count = 0
    for _, row in df.iterrows():
        name = str(row['player_name']).strip()
        cur.execute(sql, (year, week, name, normalize_name(name), row['stat_field'], float(row['line'])))
        count += 1

    conn.commit()
    cur.close()
    conn.close()
    click.echo(f"Loaded {count} props for Week {week}, {year}.")


@app.cli.command("load-weekly-salary")
@click.argument("csv_path", type=click.Path(exists=True))
@click.option("--year", default=None, type=int,
              help="Season year. Auto-detected from the filename when possible "
                   "(DFF: date in filename; RotoWire: parent folder named by year). "
                   "Required as a fallback if it can't be detected.")
def load_weekly_salary_command(csv_path, year):
    """
    Load one week's salary file directly into the live `players` table —
    the same table the Slate page (/) reads from — bypassing RotoWire's
    live scraper entirely.

    Use this once the real season starts and RotoWire's free-tier 1-player
    cap makes `flask ingest-slate` unusable: download the week's salary
    export from DraftKings or Daily Fantasy Fuel (DFF), then run this
    command directly against that file.

    Reuses the exact same format-detection and parsing logic already
    proven in scrapers/combine_dk_salaries.py (which builds the
    historical hist_dfs_salaries data) — DFF exports, RotoWire exports,
    and pre-processed files (e.g. scrapers/scrape_fp_dk_salaries.py's
    output, identified by having name_normalized/dk_salary columns
    already present) are all auto-detected and handled. Week comes from
    the file's own `week` column, same as year detection.

    Examples:
        flask load-weekly-salary "C:\\Downloads\\DFF_NFL_cheatsheet_2026-09-10.csv"
        flask load-weekly-salary "C:\\Downloads\\Week1_salaries_rotowire.csv" --year 2026
        flask load-weekly-salary data\\fp_dk_salaries_week1_2026.csv.gz
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scrapers'))
    import pandas as pd

    try:
        df = pd.read_csv(csv_path, encoding='utf-8', on_bad_lines='skip')
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, encoding='latin-1', on_bad_lines='skip')

    if df.empty:
        click.echo("ERROR: file is empty.", err=True)
        return

    # Pre-processed formats (already in final shape, no DFF/RotoWire-style
    # transformation needed) — currently just the FantasyPros salary
    # scraper's output, identified by having both name_normalized and
    # dk_salary columns already present. Skip format detection and
    # parse_dff/parse_rotowire entirely; use the file as-is. This also
    # means this path has no dependency on combine_dk_salaries.py at all
    # (that import is deferred below, only for the DFF/RotoWire path).
    if 'name_normalized' in df.columns and 'dk_salary' in df.columns:
        click.echo("Format detected: pre-processed (e.g. FantasyPros salary scrape)")
        parsed = df
        detected_year = int(parsed['year'].iloc[0]) if 'year' in parsed.columns and pd.notna(parsed['year'].iloc[0]) else year
        if detected_year is None:
            click.echo("ERROR: couldn't determine the season year from the file "
                       "or --year argument.", err=True)
            return
        click.echo(f"Year: {detected_year}")
    else:
        from combine_dk_salaries import detect_format, year_from_path, parse_dff, parse_rotowire

        fmt = detect_format(df)
        if fmt == 'unknown':
            click.echo(f"ERROR: unrecognised file format. Columns found: {list(df.columns)[:8]}...", err=True)
            click.echo("Expected either a DFF cheatsheet export, a RotoWire export, "
                       "or a pre-processed file with name_normalized/dk_salary columns.", err=True)
            return

        detected_year = year_from_path(csv_path, year)
        if detected_year is None:
            click.echo("ERROR: couldn't determine the season year from the filename "
                       "or folder. Pass it explicitly with --year.", err=True)
            return

        click.echo(f"Format detected: {fmt}")
        click.echo(f"Year: {detected_year}")

        parsed = parse_dff(df, detected_year) if fmt == 'dff' else parse_rotowire(df, detected_year)

    if parsed.empty:
        click.echo("ERROR: no valid player rows parsed from this file.", err=True)
        return

    week = int(parsed['week'].iloc[0])
    click.echo(f"Week: {week}")
    click.echo(f"Players parsed: {len(parsed)}")

    conn = _connect()
    cur  = _cursor(conn)
    inserted = skipped = 0

    for _, p in parsed.iterrows():
        try:
            # parse_dff/parse_rotowire's dk_salary maps to our `salary`
            # column; projected_pts maps directly; ownership_pct too.
            row_data = (
                week, detected_year, p['name'], p['position'], p['team'], p['opponent'],
                int(p['dk_salary']) if pd.notna(p['dk_salary']) else 0,
                float(p['projected_pts']) if pd.notna(p['projected_pts']) else None,
                float(p['ownership_pct']) if pd.notna(p['ownership_pct']) else None,
            )
            if _is_postgres():
                cur.execute("""
                    INSERT INTO players
                        (week, year, name, position, team, opponent, salary, projected_pts, ownership_pct)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (week, year, name) DO UPDATE SET
                        position      = EXCLUDED.position,
                        team          = EXCLUDED.team,
                        opponent      = EXCLUDED.opponent,
                        salary        = EXCLUDED.salary,
                        projected_pts = EXCLUDED.projected_pts,
                        ownership_pct = EXCLUDED.ownership_pct
                """, row_data)
            else:
                cur.execute("""
                    INSERT INTO players
                        (week, year, name, position, team, opponent, salary, projected_pts, ownership_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(week, year, name) DO UPDATE SET
                        position      = excluded.position,
                        team          = excluded.team,
                        opponent      = excluded.opponent,
                        salary        = excluded.salary,
                        projected_pts = excluded.projected_pts,
                        ownership_pct = excluded.ownership_pct
                """, row_data)
            inserted += 1
        except Exception as e:
            click.echo(f"  Skipped {p.get('name', '?')}: {e}")
            skipped += 1

    conn.commit()
    cur.close()
    conn.close()
    click.echo(f"Done. {inserted} players upserted, {skipped} skipped.")
    click.echo(f"Slate page will now show Week {week}, {detected_year}.")


@app.cli.command("load-schedule")
@click.option("--year", required=True, type=int)
def load_schedule_command(year):
    """
    Load kickoff times for one season into game_schedule — required for
    the per-game lineup lock: once a specific game's kickoff time has
    passed, players in that game can no longer be newly selected, and
    if already in a submitted lineup, can't be swapped out.

    Reads data/schedules/{year}_schedule_df.csv (same format as the
    historical schedule files: year, week, team_1, team_2, date, time,
    location, boxscore_url). team_2 is the home team, team_1 is away
    (matching the convention already established in scrape_pfr.py's
    load_schedule()). Both teams get a row with the same kickoff
    datetime, keyed by their own team so a simple (year, week, team)
    lookup works for any player regardless of which side of the
    matchup they're on.

    Team names are resolved to canonical abbreviations via
    team_mapping.normalize_team() — reusing the same normalizer DST
    matching already depends on, so this stays consistent with the
    rest of the system.
    """
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scrapers'))
    from team_mapping import normalize_team

    EASTERN = ZoneInfo("America/New_York")

    path = os.path.join('data', 'schedules', f'{year}_schedule_df.csv')
    if not os.path.exists(path):
        click.echo(f"ERROR: schedule file not found: {path}", err=True)
        click.echo(f"Place a {year}_schedule_df.csv in data/schedules/ "
                   f"(same format as historical years) before running this.", err=True)
        return

    df = pd.read_csv(path)
    df = df[df['team_2'] != 'BYE']
    df = df[df['week'].apply(lambda w: str(w).isdigit() or isinstance(w, int))]
    df['week'] = df['week'].astype(int)
    df = df[df['week'] <= 18]

    conn = _connect()
    cur  = _cursor(conn)
    inserted = skipped = 0

    for _, row in df.iterrows():
        try:
            date_str = str(row['date']).strip()
            time_str = str(row.get('time', '') or '').strip()
            if not date_str or date_str.lower() == 'nan':
                skipped += 1
                continue

            # Combine date + time into one datetime. Handle a missing/
            # unparseable time gracefully by defaulting to midnight —
            # better to have an approximate kickoff than none at all,
            # though this will be slightly wrong for that one game.
            dt = pd.to_datetime(date_str, errors='coerce')
            if pd.isna(dt):
                skipped += 1
                continue

            if time_str and time_str.lower() != 'nan':
                time_parsed = pd.to_datetime(time_str, errors='coerce')
                if pd.notna(time_parsed):
                    dt = dt.replace(hour=time_parsed.hour, minute=time_parsed.minute)

            # NFL kickoff times in the schedule CSV are US Eastern
            # (e.g. "8:20PM" means 8:20PM ET, not UTC). Localize
            # properly — using zoneinfo rather than a fixed UTC offset
            # so Daylight Saving transitions (EDT in Sept, EST in Dec)
            # are handled correctly — then convert to UTC for storage,
            # so the slate route's datetime.utcnow() comparison is
            # always correct regardless of what timezone is "now".
            dt_eastern = dt.to_pydatetime().replace(tzinfo=EASTERN)
            dt_utc     = dt_eastern.astimezone(ZoneInfo("UTC"))
            kickoff    = dt_utc.strftime('%Y-%m-%d %H:%M:%S')

            # team_2 = home, team_1 = away (matches scrape_pfr.py's load_schedule())
            home_team = normalize_team(str(row['team_2']))
            away_team = normalize_team(str(row['team_1']))

            if not home_team or not away_team:
                click.echo(f"  WARNING: couldn't normalize team(s) in row: "
                          f"{row['team_1']} @ {row['team_2']}")
                skipped += 1
                continue

            for team, opponent, home_away in [(home_team, away_team, 'h'), (away_team, home_team, 'a')]:
                ph = _ph(6)
                if _is_postgres():
                    cur.execute(f"""
                        INSERT INTO game_schedule (year, week, team, opponent, home_away, kickoff)
                        VALUES ({ph})
                        ON CONFLICT (year, week, team) DO UPDATE SET
                            opponent  = EXCLUDED.opponent,
                            home_away = EXCLUDED.home_away,
                            kickoff   = EXCLUDED.kickoff
                    """, (year, int(row['week']), team, opponent, home_away, kickoff))
                else:
                    cur.execute(f"""
                        INSERT INTO game_schedule (year, week, team, opponent, home_away, kickoff)
                        VALUES ({ph})
                        ON CONFLICT(year, week, team) DO UPDATE SET
                            opponent  = excluded.opponent,
                            home_away = excluded.home_away,
                            kickoff   = excluded.kickoff
                    """, (year, int(row['week']), team, opponent, home_away, kickoff))
                inserted += 1

        except Exception as e:
            click.echo(f"  Skipped row ({row.get('team_1','?')} @ {row.get('team_2','?')}): {e}")
            skipped += 1

    conn.commit()
    cur.close()
    conn.close()
    click.echo(f"Done. {inserted} team-week kickoff rows loaded, {skipped} skipped.")


@app.cli.command("load-history")
@click.option("--data-dir", default="data",
              help="Folder containing the .csv.gz files produced by the scrapers.")
@click.option("--salaries-only", is_flag=True, help="Only load salary files, skip everything else.")
@click.option("--stats-only",    is_flag=True, help="Only load player stats, skip everything else.")
@click.option("--weather-only",  is_flag=True,
              help="Only load the weather file, skip everything else — for frequent "
                   "re-runs as forecasts firm up or actual conditions come in, without "
                   "waiting on the much slower full load.")
@click.option("--batch-size", default=1000, type=int, help="Rows per bulk-insert batch.")
def load_history_command(data_dir, salaries_only, stats_only, weather_only, batch_size):
    """
    Load the historical .csv.gz files produced by the scrapers into
    hist_dfs_salaries and hist_player_stats.

    Reads (whichever of these exist):
        data/rotoguru_dk_2014_2021.csv.gz     -> hist_dfs_salaries
        data/rotowire_dk_2022_2025.csv.gz     -> hist_dfs_salaries
        data/dk_salaries_2022_2025.csv.gz     -> hist_dfs_salaries
        data/pfr_player_stats_2014_2025.csv.gz -> hist_player_stats

    Safe to re-run: uses ON CONFLICT upsert on each table's unique key,
    so re-running after a fresh scrape just refreshes existing rows and
    adds new ones — nothing is duplicated.

    To load into a remote Render Postgres DB instead of local SQLite,
    set DATABASE_URL to Render's "External Database URL" before running:
        DATABASE_URL=postgres://...  flask load-history        (Mac/Linux)
        $env:DATABASE_URL="postgres://..."; flask load-history  (PowerShell)
    """
    import pandas as pd

    SALARY_FILES = [
        "rotoguru_dk_2014_2021.csv.gz",
        "rotowire_dk_2022_2025.csv.gz",
        "dk_salaries_2022_2025.csv.gz",
    ]
    STATS_FILE = "pfr_player_stats_2014_2025.csv.gz"
    DST_FILE   = "hist_dst_stats.csv.gz"
    WEATHER_FILE   = "weekly_weather.csv.gz"
    GAME_INFO_FILE = "pfr_game_info_2014_2025.csv.gz"

    # True only when no "-only" flag was passed — an unscoped run loads
    # everything, same as before these flags existed. Any "-only" flag
    # narrows to just its own section(s).
    run_all = not (salaries_only or stats_only or weather_only)

    conn = _connect()
    cur  = _cursor(conn)

    def upsert_salaries(df: pd.DataFrame, source_label: str):
        ph = _ph(13)
        if _is_postgres():
            sql = f"""
                INSERT INTO hist_dfs_salaries
                    (week, year, name, name_normalized, position, team, opponent,
                     home_away, dk_salary, dk_pts_scored, projected_pts, ownership_pct, source)
                VALUES ({ph})
                ON CONFLICT (week, year, name_normalized, source) DO UPDATE SET
                    position      = EXCLUDED.position,
                    team          = EXCLUDED.team,
                    opponent      = EXCLUDED.opponent,
                    home_away     = EXCLUDED.home_away,
                    dk_salary     = EXCLUDED.dk_salary,
                    dk_pts_scored = EXCLUDED.dk_pts_scored,
                    projected_pts = EXCLUDED.projected_pts,
                    ownership_pct = EXCLUDED.ownership_pct
            """
        else:
            sql = f"""
                INSERT INTO hist_dfs_salaries
                    (week, year, name, name_normalized, position, team, opponent,
                     home_away, dk_salary, dk_pts_scored, projected_pts, ownership_pct, source)
                VALUES ({ph})
                ON CONFLICT(week, year, name_normalized, source) DO UPDATE SET
                    position      = excluded.position,
                    team          = excluded.team,
                    opponent      = excluded.opponent,
                    home_away     = excluded.home_away,
                    dk_salary     = excluded.dk_salary,
                    dk_pts_scored = excluded.dk_pts_scored,
                    projected_pts = excluded.projected_pts,
                    ownership_pct = excluded.ownership_pct
            """

        inserted = 0
        batch = []
        for _, r in df.iterrows():
            batch.append((
                int(r.get('week')), int(r.get('year')),
                str(r.get('name', '')), str(r.get('name_normalized', '')),
                _none_if_nan(r.get('position')), _none_if_nan(r.get('team')),
                _none_if_nan(r.get('opponent')), _none_if_nan(r.get('home_away')),
                _int_or_none(r.get('dk_salary')), _float_or_none(r.get('dk_pts_scored')),
                _float_or_none(r.get('projected_pts')), _float_or_none(r.get('ownership_pct')),
                source_label,
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                click.echo(f"    ...{inserted:,} rows loaded")
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    def upsert_stats(df: pd.DataFrame):
        if _is_postgres():
            sql = f"""
                INSERT INTO hist_player_stats
                    (pfr_id, name, name_normalized, year, week, game_date, team, opponent, home_away,
                     position, dk_pts, dk_pts_pfr_reported, pass_cmp, pass_att, pass_yds, pass_td, pass_int,
                     rush_att, rush_yds, rush_td, rec_tgt, rec, rec_yds, rec_td, fumbles_lost, fumbles_rec_td,
                     kick_ret, kick_ret_yds, kick_ret_td, punt_ret, punt_ret_yds, punt_ret_td, snap_pct)
                VALUES ({_ph(33)})
                ON CONFLICT (pfr_id, year, week) DO UPDATE SET
                    name         = EXCLUDED.name,
                    game_date    = EXCLUDED.game_date,
                    team         = EXCLUDED.team,
                    opponent     = EXCLUDED.opponent,
                    home_away    = EXCLUDED.home_away,
                    position     = EXCLUDED.position,
                    dk_pts       = EXCLUDED.dk_pts,
                    dk_pts_pfr_reported = EXCLUDED.dk_pts_pfr_reported,
                    pass_cmp     = EXCLUDED.pass_cmp,
                    pass_att     = EXCLUDED.pass_att,
                    pass_yds     = EXCLUDED.pass_yds,
                    pass_td      = EXCLUDED.pass_td,
                    pass_int     = EXCLUDED.pass_int,
                    rush_att     = EXCLUDED.rush_att,
                    rush_yds     = EXCLUDED.rush_yds,
                    rush_td      = EXCLUDED.rush_td,
                    rec_tgt      = EXCLUDED.rec_tgt,
                    rec          = EXCLUDED.rec,
                    rec_yds      = EXCLUDED.rec_yds,
                    rec_td       = EXCLUDED.rec_td,
                    fumbles_lost = EXCLUDED.fumbles_lost,
                    fumbles_rec_td = EXCLUDED.fumbles_rec_td,
                    kick_ret     = EXCLUDED.kick_ret,
                    kick_ret_yds = EXCLUDED.kick_ret_yds,
                    kick_ret_td  = EXCLUDED.kick_ret_td,
                    punt_ret     = EXCLUDED.punt_ret,
                    punt_ret_yds = EXCLUDED.punt_ret_yds,
                    punt_ret_td  = EXCLUDED.punt_ret_td,
                    snap_pct     = EXCLUDED.snap_pct
            """
        else:
            sql = f"""
                INSERT INTO hist_player_stats
                    (pfr_id, name, name_normalized, year, week, game_date, team, opponent, home_away,
                     position, dk_pts, dk_pts_pfr_reported, pass_cmp, pass_att, pass_yds, pass_td, pass_int,
                     rush_att, rush_yds, rush_td, rec_tgt, rec, rec_yds, rec_td, fumbles_lost, fumbles_rec_td,
                     kick_ret, kick_ret_yds, kick_ret_td, punt_ret, punt_ret_yds, punt_ret_td, snap_pct)
                VALUES ({_ph(33)})
                ON CONFLICT(pfr_id, year, week) DO UPDATE SET
                    name         = excluded.name,
                    game_date    = excluded.game_date,
                    team         = excluded.team,
                    opponent     = excluded.opponent,
                    home_away    = excluded.home_away,
                    position     = excluded.position,
                    dk_pts       = excluded.dk_pts,
                    dk_pts_pfr_reported = excluded.dk_pts_pfr_reported,
                    pass_cmp     = excluded.pass_cmp,
                    pass_att     = excluded.pass_att,
                    pass_yds     = excluded.pass_yds,
                    pass_td      = excluded.pass_td,
                    pass_int     = excluded.pass_int,
                    rush_att     = excluded.rush_att,
                    rush_yds     = excluded.rush_yds,
                    rush_td      = excluded.rush_td,
                    rec_tgt      = excluded.rec_tgt,
                    rec          = excluded.rec,
                    rec_yds      = excluded.rec_yds,
                    rec_td       = excluded.rec_td,
                    fumbles_lost = excluded.fumbles_lost,
                    fumbles_rec_td = excluded.fumbles_rec_td,
                    kick_ret     = excluded.kick_ret,
                    kick_ret_yds = excluded.kick_ret_yds,
                    kick_ret_td  = excluded.kick_ret_td,
                    punt_ret     = excluded.punt_ret,
                    punt_ret_yds = excluded.punt_ret_yds,
                    punt_ret_td  = excluded.punt_ret_td,
                    snap_pct     = excluded.snap_pct
            """

        inserted = 0
        batch = []
        for _, r in df.iterrows():
            batch.append((
                str(r.get('pfr_id', '')), str(r.get('name', '')), str(r.get('name_normalized', '')),
                int(r.get('year')), int(r.get('week')), _none_if_nan(r.get('game_date')),
                _none_if_nan(r.get('team')), _none_if_nan(r.get('opponent')),
                _none_if_nan(r.get('home_away')), _none_if_nan(r.get('position')),
                _float_or_none(r.get('dk_pts')),
                _float_or_none(r.get('dk_pts_pfr_reported')),
                _int_or_none(r.get('pass_cmp')), _int_or_none(r.get('pass_att')),
                _int_or_none(r.get('pass_yds')), _int_or_none(r.get('pass_td')),
                _int_or_none(r.get('pass_int')),
                _int_or_none(r.get('rush_att')), _int_or_none(r.get('rush_yds')),
                _int_or_none(r.get('rush_td')),
                _int_or_none(r.get('rec_tgt')), _int_or_none(r.get('rec')),
                _int_or_none(r.get('rec_yds')), _int_or_none(r.get('rec_td')),
                _int_or_none(r.get('fumbles_lost')),
                _int_or_none(r.get('fumbles_rec_td')),
                _int_or_none(r.get('kick_ret')), _int_or_none(r.get('kick_ret_yds')),
                _int_or_none(r.get('kick_ret_td')),
                _int_or_none(r.get('punt_ret')), _int_or_none(r.get('punt_ret_yds')),
                _int_or_none(r.get('punt_ret_td')),
                _float_or_none(r.get('snap_pct')),
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                click.echo(f"    ...{inserted:,} rows loaded")
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    def upsert_dst_stats(df: pd.DataFrame):
        if _is_postgres():
            sql = f"""
                INSERT INTO hist_dst_stats
                    (year, week, team, opponent, points_allowed, dk_pts,
                     sack, interception, fumble_rec, forced_fumble,
                     def_td, safety, special_teams_td)
                VALUES ({_ph(13)})
                ON CONFLICT (year, week, team) DO UPDATE SET
                    opponent         = EXCLUDED.opponent,
                    points_allowed   = EXCLUDED.points_allowed,
                    dk_pts           = EXCLUDED.dk_pts,
                    sack             = EXCLUDED.sack,
                    interception     = EXCLUDED.interception,
                    fumble_rec       = EXCLUDED.fumble_rec,
                    forced_fumble    = EXCLUDED.forced_fumble,
                    def_td           = EXCLUDED.def_td,
                    safety           = EXCLUDED.safety,
                    special_teams_td = EXCLUDED.special_teams_td
            """
        else:
            sql = f"""
                INSERT INTO hist_dst_stats
                    (year, week, team, opponent, points_allowed, dk_pts,
                     sack, interception, fumble_rec, forced_fumble,
                     def_td, safety, special_teams_td)
                VALUES ({_ph(13)})
                ON CONFLICT(year, week, team) DO UPDATE SET
                    opponent         = excluded.opponent,
                    points_allowed   = excluded.points_allowed,
                    dk_pts           = excluded.dk_pts,
                    sack             = excluded.sack,
                    interception     = excluded.interception,
                    fumble_rec       = excluded.fumble_rec,
                    forced_fumble    = excluded.forced_fumble,
                    def_td           = excluded.def_td,
                    safety           = excluded.safety,
                    special_teams_td = excluded.special_teams_td
            """

        inserted = 0
        batch = []
        for _, r in df.iterrows():
            batch.append((
                int(r.get('year')), int(r.get('week')), str(r.get('team', '')),
                _none_if_nan(r.get('opponent')),
                _int_or_none(r.get('points_allowed')),
                _float_or_none(r.get('dk_pts')),
                _int_or_none(r.get('sack')), _int_or_none(r.get('interception')),
                _int_or_none(r.get('fumble_rec')), _int_or_none(r.get('forced_fumble')),
                _int_or_none(r.get('def_td')), _int_or_none(r.get('safety')),
                _int_or_none(r.get('special_teams_td')),
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                click.echo(f"    ...{inserted:,} rows loaded")
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    def upsert_fantasy_points_against(df: pd.DataFrame):
        if _is_postgres():
            sql = f"""
                INSERT INTO hist_fantasy_points_against
                    (year, position, team, games, fantasy_pts, dk_pts, fd_pts,
                     fantasy_pts_per_game, dk_pts_per_game, fd_pts_per_game)
                VALUES ({_ph(10)})
                ON CONFLICT (year, position, team) DO UPDATE SET
                    games                = EXCLUDED.games,
                    fantasy_pts          = EXCLUDED.fantasy_pts,
                    dk_pts               = EXCLUDED.dk_pts,
                    fd_pts               = EXCLUDED.fd_pts,
                    fantasy_pts_per_game = EXCLUDED.fantasy_pts_per_game,
                    dk_pts_per_game      = EXCLUDED.dk_pts_per_game,
                    fd_pts_per_game      = EXCLUDED.fd_pts_per_game
            """
        else:
            sql = f"""
                INSERT INTO hist_fantasy_points_against
                    (year, position, team, games, fantasy_pts, dk_pts, fd_pts,
                     fantasy_pts_per_game, dk_pts_per_game, fd_pts_per_game)
                VALUES ({_ph(10)})
                ON CONFLICT(year, position, team) DO UPDATE SET
                    games                = excluded.games,
                    fantasy_pts          = excluded.fantasy_pts,
                    dk_pts               = excluded.dk_pts,
                    fd_pts               = excluded.fd_pts,
                    fantasy_pts_per_game = excluded.fantasy_pts_per_game,
                    dk_pts_per_game      = excluded.dk_pts_per_game,
                    fd_pts_per_game      = excluded.fd_pts_per_game
            """

        inserted = 0
        batch = []
        for _, r in df.iterrows():
            batch.append((
                int(r.get('year')), str(r.get('position', '')), str(r.get('team', '')),
                _int_or_none(r.get('games')),
                _float_or_none(r.get('fantasy_pts')), _float_or_none(r.get('dk_pts')),
                _float_or_none(r.get('fd_pts')),
                _float_or_none(r.get('fantasy_pts_per_game')),
                _float_or_none(r.get('dk_pts_per_game')),
                _float_or_none(r.get('fd_pts_per_game')),
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                click.echo(f"    ...{inserted:,} rows loaded")
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    def upsert_team_points(df: pd.DataFrame):
        if _is_postgres():
            sql = f"""
                INSERT INTO hist_team_points
                    (year, week, team, opponent, home_away, points_scored, points_allowed)
                VALUES ({_ph(7)})
                ON CONFLICT (year, week, team) DO UPDATE SET
                    opponent       = EXCLUDED.opponent,
                    home_away      = EXCLUDED.home_away,
                    points_scored  = EXCLUDED.points_scored,
                    points_allowed = EXCLUDED.points_allowed
            """
        else:
            sql = f"""
                INSERT INTO hist_team_points
                    (year, week, team, opponent, home_away, points_scored, points_allowed)
                VALUES ({_ph(7)})
                ON CONFLICT(year, week, team) DO UPDATE SET
                    opponent       = excluded.opponent,
                    home_away      = excluded.home_away,
                    points_scored  = excluded.points_scored,
                    points_allowed = excluded.points_allowed
            """

        inserted = 0
        batch = []
        for _, r in df.iterrows():
            batch.append((
                int(r.get('year')), int(r.get('week')), str(r.get('team', '')),
                _none_if_nan(r.get('opponent')), _none_if_nan(r.get('home_away')),
                _int_or_none(r.get('points_scored')), _int_or_none(r.get('points_allowed')),
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                click.echo(f"    ...{inserted:,} rows loaded")
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    def replace_depth_charts(df: pd.DataFrame):
        """
        Full replace, not upsert — a player dropped from a team's
        depth chart between scrapes shouldn't linger in the table
        forever. This is a live/current-state table (like `players`),
        not a historical one, so DELETE-then-INSERT is the correct
        semantic here, unlike every other loader in this function.
        """
        cur.execute("DELETE FROM depth_charts")
        conn.commit()

        sql = f"""
            INSERT INTO depth_charts
                (team, pos, string_rank, player_name, player_name_normalized, ourlads_player_id)
            VALUES ({_ph(6)})
        """
        inserted = 0
        batch = []
        for _, r in df.iterrows():
            name = str(r.get('player_name', ''))
            batch.append((
                str(r.get('team', '')), str(r.get('pos', '')), int(r.get('string_rank')),
                name, normalize_name(name), _none_if_nan(r.get('ourlads_player_id')),
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    def upsert_weather(df: pd.DataFrame):
        if _is_postgres():
            sql = f"""
                INSERT INTO hist_weather
                    (year, week, game_date, status, away_team, home_team,
                     away_score, home_score, temp_f, condition, wind_mph, wind_direction)
                VALUES ({_ph(12)})
                ON CONFLICT (year, week, away_team, home_team) DO UPDATE SET
                    game_date      = EXCLUDED.game_date,
                    status         = EXCLUDED.status,
                    away_score     = EXCLUDED.away_score,
                    home_score     = EXCLUDED.home_score,
                    temp_f         = EXCLUDED.temp_f,
                    condition      = EXCLUDED.condition,
                    wind_mph       = EXCLUDED.wind_mph,
                    wind_direction = EXCLUDED.wind_direction
            """
        else:
            sql = f"""
                INSERT INTO hist_weather
                    (year, week, game_date, status, away_team, home_team,
                     away_score, home_score, temp_f, condition, wind_mph, wind_direction)
                VALUES ({_ph(12)})
                ON CONFLICT(year, week, away_team, home_team) DO UPDATE SET
                    game_date      = excluded.game_date,
                    status         = excluded.status,
                    away_score     = excluded.away_score,
                    home_score     = excluded.home_score,
                    temp_f         = excluded.temp_f,
                    condition      = excluded.condition,
                    wind_mph       = excluded.wind_mph,
                    wind_direction = excluded.wind_direction
            """

        inserted = 0
        batch = []
        for _, r in df.iterrows():
            batch.append((
                int(r.get('year')), int(r.get('week')),
                _none_if_nan(r.get('game_date')), _none_if_nan(r.get('status')),
                str(r.get('away_team', '')), str(r.get('home_team', '')),
                _int_or_none(r.get('away_score')), _int_or_none(r.get('home_score')),
                _int_or_none(r.get('temp_f')), _none_if_nan(r.get('condition')),
                _int_or_none(r.get('wind_mph')), _none_if_nan(r.get('wind_direction')),
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                click.echo(f"    ...{inserted:,} rows loaded")
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    def upsert_game_info(df: pd.DataFrame):
        if _is_postgres():
            sql = f"""
                INSERT INTO hist_game_info
                    (boxscore_url, year, week, team_home, team_away, date, time, location,
                     won_toss, won_ot_toss, roof, surface, duration, attendance,
                     vegas_line, over_under, temp, humidity, wind)
                VALUES ({_ph(19)})
                ON CONFLICT (boxscore_url) DO UPDATE SET
                    year         = EXCLUDED.year,
                    week         = EXCLUDED.week,
                    team_home    = EXCLUDED.team_home,
                    team_away    = EXCLUDED.team_away,
                    date         = EXCLUDED.date,
                    time         = EXCLUDED.time,
                    location     = EXCLUDED.location,
                    won_toss     = EXCLUDED.won_toss,
                    won_ot_toss  = EXCLUDED.won_ot_toss,
                    roof         = EXCLUDED.roof,
                    surface      = EXCLUDED.surface,
                    duration     = EXCLUDED.duration,
                    attendance   = EXCLUDED.attendance,
                    vegas_line   = EXCLUDED.vegas_line,
                    over_under   = EXCLUDED.over_under,
                    temp         = EXCLUDED.temp,
                    humidity     = EXCLUDED.humidity,
                    wind         = EXCLUDED.wind
            """
        else:
            sql = f"""
                INSERT INTO hist_game_info
                    (boxscore_url, year, week, team_home, team_away, date, time, location,
                     won_toss, won_ot_toss, roof, surface, duration, attendance,
                     vegas_line, over_under, temp, humidity, wind)
                VALUES ({_ph(19)})
                ON CONFLICT(boxscore_url) DO UPDATE SET
                    year         = excluded.year,
                    week         = excluded.week,
                    team_home    = excluded.team_home,
                    team_away    = excluded.team_away,
                    date         = excluded.date,
                    time         = excluded.time,
                    location     = excluded.location,
                    won_toss     = excluded.won_toss,
                    won_ot_toss  = excluded.won_ot_toss,
                    roof         = excluded.roof,
                    surface      = excluded.surface,
                    duration     = excluded.duration,
                    attendance   = excluded.attendance,
                    vegas_line   = excluded.vegas_line,
                    over_under   = excluded.over_under,
                    temp         = excluded.temp,
                    humidity     = excluded.humidity,
                    wind         = excluded.wind
            """

        inserted = 0
        batch = []
        for _, r in df.iterrows():
            batch.append((
                str(r.get('boxscore_url', '')), int(r.get('year')), int(r.get('week')),
                _none_if_nan(r.get('team_home')), _none_if_nan(r.get('team_away')),
                _none_if_nan(r.get('date')), _none_if_nan(r.get('time')), _none_if_nan(r.get('location')),
                _none_if_nan(r.get('won_toss')), _none_if_nan(r.get('won_ot_toss')),
                _none_if_nan(r.get('roof')), _none_if_nan(r.get('surface')),
                _none_if_nan(r.get('duration')), _none_if_nan(r.get('attendance')),
                _none_if_nan(r.get('vegas_line')), _none_if_nan(r.get('over_under')),
                _none_if_nan(r.get('temp')), _none_if_nan(r.get('humidity')), _none_if_nan(r.get('wind')),
            ))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                click.echo(f"    ...{inserted:,} rows loaded")
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
        return inserted

    # --- Load salary files ---
    if run_all or salaries_only:
        for filename in SALARY_FILES:
            path = os.path.join(data_dir, filename)
            if not os.path.exists(path):
                click.echo(f"Skip (not found): {path}")
                continue
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            source_label = filename.replace('.csv.gz', '')
            count = upsert_salaries(df, source_label)
            click.echo(f"  Done: {count:,} rows from {filename}")

        # FantasyPros weekly salary scrapes — one file per week
        # (fp_dk_salaries_week{N}_{year}.csv.gz), unlike the bulk
        # historical files above. Glob for all of them rather than
        # hardcoding filenames, so new weeks get picked up automatically
        # as the season progresses without needing code changes here.
        import glob
        fp_salary_files = sorted(glob.glob(os.path.join(data_dir, "fp_dk_salaries_week*_*.csv.gz")))
        if not fp_salary_files:
            click.echo(f"Skip (not found): {os.path.join(data_dir, 'fp_dk_salaries_week*_*.csv.gz')}")
        for path in fp_salary_files:
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            count = upsert_salaries(df, 'fantasypros_dk_salary')
            click.echo(f"  Done: {count:,} rows from {os.path.basename(path)}")

    # --- Load player stats file ---
    if run_all or stats_only:
        path = os.path.join(data_dir, STATS_FILE)
        if not os.path.exists(path):
            click.echo(f"Skip (not found): {path}")
        else:
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            count = upsert_stats(df)
            click.echo(f"  Done: {count:,} rows from {STATS_FILE}")

    # --- Load weather file ---
    if run_all or weather_only:
        path = os.path.join(data_dir, WEATHER_FILE)
        if not os.path.exists(path):
            click.echo(f"Skip (not found): {path}")
        else:
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            count = upsert_weather(df)
            click.echo(f"  Done: {count:,} rows from {WEATHER_FILE}")

    # --- Load game info file ---
    if run_all:
        path = os.path.join(data_dir, GAME_INFO_FILE)
        if not os.path.exists(path):
            click.echo(f"Skip (not found): {path}")
        else:
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            count = upsert_game_info(df)
            click.echo(f"  Done: {count:,} rows from {GAME_INFO_FILE}")

    # --- Load DST (team defense) stats file ---
    if run_all:
        path = os.path.join(data_dir, DST_FILE)
        if not os.path.exists(path):
            click.echo(f"Skip (not found): {path}")
        else:
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            count = upsert_dst_stats(df)
            click.echo(f"  Done: {count:,} rows from {DST_FILE}")

    # --- Load fantasy-points-against files (one per year) ---
    if run_all:
        fpa_files = sorted(glob.glob(os.path.join(data_dir, "fantasy_points_against_*.csv.gz")))
        if not fpa_files:
            click.echo(f"Skip (not found): {os.path.join(data_dir, 'fantasy_points_against_*.csv.gz')}")
        for path in fpa_files:
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            count = upsert_fantasy_points_against(df)
            click.echo(f"  Done: {count:,} rows from {os.path.basename(path)}")

    # --- Load team points file (single file, all years combined) ---
    if run_all:
        team_points_path = os.path.join(data_dir, "team_points_by_week.csv.gz")
        if not os.path.exists(team_points_path):
            click.echo(f"Skip (not found): {team_points_path}")
        else:
            click.echo(f"Loading {team_points_path} ...")
            df = pd.read_csv(team_points_path)
            count = upsert_team_points(df)
            click.echo(f"  Done: {count:,} rows from {os.path.basename(team_points_path)}")

    # --- Load depth charts (single file, always reflects the latest
    # scrape — full replace, not a historical/multi-week accumulation) ---
    if run_all:
        depth_charts_path = os.path.join(data_dir, "ourlads_depth_charts.csv.gz")
        if not os.path.exists(depth_charts_path):
            click.echo(f"Skip (not found): {depth_charts_path}")
        else:
            click.echo(f"Loading {depth_charts_path} ...")
            df = pd.read_csv(depth_charts_path)
            count = replace_depth_charts(df)
            click.echo(f"  Done: {count:,} rows from {os.path.basename(depth_charts_path)} (full replace)")

    cur.close()
    conn.close()
    click.echo("load-history complete.")


def _none_if_nan(v):
    if v is None:
        return None
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass
    s = str(v).strip()
    return s if s and s.lower() != 'nan' else None


def _int_or_none(v):
    try:
        if v is None or (isinstance(v, float) and v != v):  # NaN check
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _float_or_none(v):
    try:
        if v is None or (isinstance(v, float) and v != v):  # NaN check
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("slate"))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "")

        row = db_fetchone(
            f"SELECT id, username, password FROM users WHERE username = {_ph()}",
            (username,)
        )
        if row and bcrypt.checkpw(password.encode(), row["password"].encode()):
            login_user(User(row["id"], row["username"]))
            return redirect(request.args.get("next") or url_for("slate"))
        else:
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Main routes
# ---------------------------------------------------------------------------

@app.route("/")
def slate():
    """Main page: player slate table + lineup builder (public)."""
    row = db_fetchone(
        "SELECT week, year FROM players ORDER BY year DESC, week DESC LIMIT 1"
    )

    if row is None:
        players      = []
        current_week = None
        current_year = None
    else:
        current_week = row["week"]
        current_year = row["year"]
        players = db_fetchall("""
            SELECT name, position, team, opponent, salary, projected_pts, ownership_pct
            FROM   players
            WHERE  week = %s AND year = %s
            ORDER  BY
                CASE position
                    WHEN 'QB'  THEN 1
                    WHEN 'RB'  THEN 2
                    WHEN 'WR'  THEN 3
                    WHEN 'TE'  THEN 4
                    WHEN 'DST' THEN 5
                    ELSE            6
                END,
                projected_pts DESC
        """ if _is_postgres() else """
            SELECT name, position, team, opponent, salary, projected_pts, ownership_pct
            FROM   players
            WHERE  week = ? AND year = ?
            ORDER  BY
                CASE position
                    WHEN 'QB'  THEN 1
                    WHEN 'RB'  THEN 2
                    WHEN 'WR'  THEN 3
                    WHEN 'TE'  THEN 4
                    WHEN 'DST' THEN 5
                    ELSE            6
                END,
                projected_pts DESC
        """, (current_week, current_year))

    existing_lineup = None
    if current_user.is_authenticated and current_week:
        ph  = _ph(3)
        row2 = db_fetchone(
            f"SELECT lineup_json, total_salary, submitted_at FROM lineups "
            f"WHERE week = {_ph()} AND year = {_ph()} AND submitter = {_ph()}",
            (current_week, current_year, current_user.username)
        )
        if row2:
            existing_lineup = {
                "players":      json.loads(row2["lineup_json"]),
                "total_salary": row2["total_salary"],
                "submitted_at": str(row2["submitted_at"]),
            }

    # Per-game lock: once a team's kickoff has passed, their players
    # can no longer be newly selected. Build a set of locked team
    # abbreviations for the current week so the template/JS can mark
    # individual rows rather than locking the whole slate at once.
    locked_teams = set()
    kickoff_by_team = {}   # team -> friendly display string, e.g. "Thu 8:20 PM ET"
    day_by_team = {}       # team -> day name, e.g. "Thursday"
    day_earliest_kickoff = {}   # day name -> earliest kickoff datetime that day (for ordering)
    total_teams_scheduled = 0
    if current_week:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        EASTERN = ZoneInfo("America/New_York")

        schedule_rows = db_fetchall(
            f"SELECT team, kickoff FROM game_schedule WHERE year = {_ph()} AND week = {_ph()}",
            (current_year, current_week)
        )
        total_teams_scheduled = len(schedule_rows)
        now = datetime.now(timezone.utc)
        for r in schedule_rows:
            kickoff = r["kickoff"]
            if isinstance(kickoff, str):
                try:
                    # Stored as UTC (naive string, 'YYYY-MM-DD HH:MM:SS')
                    kickoff = datetime.fromisoformat(kickoff).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            elif kickoff is not None and kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)

            if not kickoff:
                continue

            if now >= kickoff:
                locked_teams.add(r["team"])

            # Friendly display string for every team, locked or not, so
            # players can plan around upcoming locks proactively.
            kickoff_eastern = kickoff.astimezone(EASTERN)
            kickoff_by_team[r["team"]] = kickoff_eastern.strftime("%a %I:%M %p ET").lstrip("0").replace(" 0", " ")

            day_name = kickoff_eastern.strftime("%A")   # "Thursday", "Sunday", etc.
            day_by_team[r["team"]] = day_name
            if day_name not in day_earliest_kickoff or kickoff < day_earliest_kickoff[day_name]:
                day_earliest_kickoff[day_name] = kickoff

    # Group players by day (chronological order, based on that day's
    # earliest kickoff) while preserving the existing position/proj-pts
    # ordering WITHIN each day — same single-table approach as before,
    # just with day-separator rows the template renders between groups.
    days_in_order = sorted(day_earliest_kickoff.keys(), key=lambda d: day_earliest_kickoff[d])
    players_by_day = []   # list of (day_name, [players]) in chronological order
    if players and days_in_order:
        buckets = {d: [] for d in days_in_order}
        no_schedule_bucket = []   # players whose team has no game_schedule row at all (rare/edge case)
        for p in players:
            d = day_by_team.get(p["team"])
            if d:
                buckets[d].append(p)
            else:
                no_schedule_bucket.append(p)
        for d in days_in_order:
            if buckets[d]:
                players_by_day.append((d, buckets[d]))
        if no_schedule_bucket:
            players_by_day.append(("Date TBD", no_schedule_bucket))
    elif players:
        # No schedule loaded at all yet — fall back to one unlabeled group
        # so the page still works, just without day separators.
        players_by_day = [(None, players)]

    depth_chart_strings = {}   # name -> "1st"/"2nd"/"3rd"/"—" display string
    if players:
        STRING_LABELS = {1: "1st", 2: "2nd", 3: "3rd"}
        names_normalized = {normalize_name(p["name"]) for p in players}
        lookup = _get_depth_chart_lookup(names_normalized)
        for p in players:
            string_rank, _pos = lookup.get(normalize_name(p["name"]), (None, None))
            depth_chart_strings[p["name"]] = STRING_LABELS.get(string_rank, "—")

    return render_template("slate.html",
                           players=players,
                           players_by_day=players_by_day,
                           week=current_week,
                           year=current_year,
                           salary_cap=SALARY_CAP,
                           existing_lineup=existing_lineup,
                           locked_teams=sorted(locked_teams),
                           total_teams_scheduled=total_teams_scheduled,
                           kickoff_by_team=kickoff_by_team,
                           depth_chart_strings=depth_chart_strings)


@app.route("/history")
def history():
    """
    Weekly historical browser: DFS salary + real box-score stats for any
    past year/week, joined on (year, week, name_normalized).

    hist_dfs_salaries is the "master" list of who was in a given week's
    slate (from RotoGuru/RotoWire/DFF); hist_player_stats supplies the
    real results, left-joined in since not every salaried player
    necessarily has a matched stats row (name mismatches, etc.).
    """
    year_week_rows = db_fetchall(
        "SELECT DISTINCT year, week FROM hist_dfs_salaries ORDER BY year DESC, week DESC"
    )
    available = [(r["year"], r["week"]) for r in year_week_rows]

    if not available:
        return render_template("history.html",
                               players=[], year=None, week=None,
                               available_years=[], available_weeks_by_year={})

    req_year = request.args.get("year", type=int)
    req_week = request.args.get("week", type=int)

    if req_year is None or req_week is None or (req_year, req_week) not in available:
        sel_year, sel_week = available[0]   # most recent by default
    else:
        sel_year, sel_week = req_year, req_week

    available_years = sorted({y for y, w in available}, reverse=True)
    available_weeks_by_year = {}
    for y, w in available:
        available_weeks_by_year.setdefault(y, []).append(w)
    for y in available_weeks_by_year:
        available_weeks_by_year[y].sort()
        available_weeks_by_year[y].append("season")   # special option, always last

    req_year = request.args.get("year", type=int)
    req_week_raw = request.args.get("week")   # not type=int — "season" is a valid value too

    if req_week_raw == "season" and req_year in available_years:
        sel_year, sel_week = req_year, "season"
    else:
        req_week = request.args.get("week", type=int)
        if req_year is None or req_week is None or (req_year, req_week) not in available:
            sel_year, sel_week = available[0]   # most recent by default
        else:
            sel_year, sel_week = req_year, req_week

    if sel_week == "season":
        # Season totals, aggregated from our own already-DK-validated
        # weekly hist_player_stats — not a fresh scrape of PFR's own
        # season fantasy page. We deliberately don't re-scrape a
        # separate source here: our weekly dk_pts_pfr_reported values
        # were already extensively validated against PFR's own numbers
        # earlier in this project, so summing them ourselves is at
        # least as accurate and stays consistent with the rest of the
        # site. Grouped by team too (not just player), so a
        # mid-season trade shows as two separate season lines — an
        # honest reflection of "stats while on team A" vs "team B"
        # rather than merging them into one misleading row.
        ph = _ph()
        players = db_fetchall(f"""
            SELECT
                pfr_id, name, position, team,
                COUNT(*) AS games,
                SUM(pass_yds) AS pass_yds, SUM(pass_td) AS pass_td, SUM(pass_int) AS pass_int,
                SUM(rush_att) AS rush_att, SUM(rush_yds) AS rush_yds, SUM(rush_td) AS rush_td,
                SUM(rec) AS rec, SUM(rec_yds) AS rec_yds, SUM(rec_td) AS rec_td,
                SUM(COALESCE(dk_pts_pfr_reported, dk_pts)) AS total_dk_pts,
                AVG(COALESCE(dk_pts_pfr_reported, dk_pts)) AS avg_dk_pts
            FROM hist_player_stats
            WHERE year = {ph}
            GROUP BY pfr_id, name, position, team
            ORDER BY total_dk_pts DESC
        """, (sel_year,))

        return render_template("history.html",
                               players=players, is_season=True,
                               year=sel_year, week=sel_week,
                               available_years=available_years,
                               available_weeks_by_year=available_weeks_by_year)

    ph = _ph()
    players = db_fetchall(f"""
        SELECT
            s.name            AS name,
            s.position        AS position,
            s.team            AS team,
            s.opponent        AS opponent,
            s.dk_salary       AS dk_salary,
            s.projected_pts   AS projected_pts,
            s.ownership_pct   AS ownership_pct,
            hp.pfr_id              AS pfr_id,
            hp.dk_pts             AS dk_pts_computed,
            hp.dk_pts_pfr_reported AS dk_pts_pfr_reported,
            hp.pass_yds AS pass_yds, hp.pass_td AS pass_td, hp.pass_int AS pass_int,
            hp.rush_att AS rush_att, hp.rush_yds AS rush_yds, hp.rush_td AS rush_td,
            hp.rec      AS rec,      hp.rec_yds  AS rec_yds,  hp.rec_td  AS rec_td
        FROM hist_dfs_salaries s
        LEFT JOIN hist_player_stats hp
            ON  hp.year = s.year AND hp.week = s.week
            AND hp.name_normalized = s.name_normalized
        WHERE s.year = {ph} AND s.week = {ph}
        ORDER BY
            CASE s.position
                WHEN 'QB'  THEN 1
                WHEN 'RB'  THEN 2
                WHEN 'WR'  THEN 3
                WHEN 'TE'  THEN 4
                WHEN 'DST' THEN 5
                ELSE 6
            END,
            s.dk_salary DESC
    """, (sel_year, sel_week))

    return render_template("history.html",
                           players=players, is_season=False,
                           year=sel_year, week=sel_week,
                           available_years=available_years,
                           available_weeks_by_year=available_weeks_by_year)


@app.route("/players/<pfr_id>")
def player_career(pfr_id):
    """
    Full career page for one player: every year/week of real stats we
    have, with salary + value context left-joined in from
    hist_dfs_salaries wherever a match exists (same name_normalized
    join used on /history — salary coverage may be incomplete for
    some years, stats will still show either way).
    """
    ph = _ph()

    # Basic player identity (name, positions played — some players
    # switch position over a career, e.g. a WR moving to a return role)
    info = db_fetchone(f"""
        SELECT name, pfr_id
        FROM hist_player_stats
        WHERE pfr_id = {ph}
        ORDER BY year DESC, week DESC
        LIMIT 1
    """, (pfr_id,))

    if info is None:
        return render_template("player.html", found=False, pfr_id=pfr_id,
                               name=None, games=[], career_totals=None)

    games = db_fetchall(f"""
        SELECT
            hp.year AS year, hp.week AS week, hp.game_date AS game_date,
            hp.team AS team, hp.opponent AS opponent, hp.home_away AS home_away,
            hp.position AS position,
            hp.dk_pts AS dk_pts_computed, hp.dk_pts_pfr_reported AS dk_pts_pfr_reported,
            hp.pass_cmp AS pass_cmp, hp.pass_att AS pass_att, hp.pass_yds AS pass_yds,
            hp.pass_td AS pass_td, hp.pass_int AS pass_int,
            hp.rush_att AS rush_att, hp.rush_yds AS rush_yds, hp.rush_td AS rush_td,
            hp.rec_tgt AS rec_tgt, hp.rec AS rec, hp.rec_yds AS rec_yds, hp.rec_td AS rec_td,
            s.dk_salary AS dk_salary
        FROM hist_player_stats hp
        LEFT JOIN hist_dfs_salaries s
            ON  s.year = hp.year AND s.week = hp.week
            AND s.name_normalized = hp.name_normalized
        WHERE hp.pfr_id = {ph}
        ORDER BY hp.year DESC, hp.week DESC
    """, (pfr_id,))

    # Career-level summary stats (best-effort — uses PFR-reported dk_pts
    # where available, falls back to computed)
    totals = db_fetchone(f"""
        SELECT
            COUNT(*) AS games_played,
            SUM(COALESCE(dk_pts_pfr_reported, dk_pts)) AS total_dk_pts,
            AVG(COALESCE(dk_pts_pfr_reported, dk_pts)) AS avg_dk_pts,
            SUM(pass_yds) AS total_pass_yds, SUM(pass_td) AS total_pass_td,
            SUM(rush_yds) AS total_rush_yds, SUM(rush_td) AS total_rush_td,
            SUM(rec)      AS total_rec,      SUM(rec_yds) AS total_rec_yds,
            SUM(rec_td)   AS total_rec_td
        FROM hist_player_stats
        WHERE pfr_id = {ph}
    """, (pfr_id,))

    return render_template("player.html",
                           found=True, pfr_id=pfr_id, name=info["name"],
                           games=games, career_totals=totals)


@app.route("/fantasy-points-against")
def fantasy_points_against():
    """
    Which teams are weak/strong matchups by position — season-level
    fantasy points allowed, sourced from PFR's fantasy-points-against
    pages. Filterable by year and position, sortable by DK pts/game
    allowed (the primary metric, since this whole project is DK-focused).
    """
    available_years_rows = db_fetchall(
        "SELECT DISTINCT year FROM hist_fantasy_points_against ORDER BY year DESC"
    )
    available_years = [r["year"] for r in available_years_rows]

    if not available_years:
        return render_template("fantasy_points_against.html", rows=[],
                               year=None, available_years=[], position="ALL")

    req_year = request.args.get("year", type=int)
    sel_year = req_year if req_year in available_years else available_years[0]
    sel_position = request.args.get("position", "ALL")

    ph = _ph()
    if sel_position != "ALL":
        rows = db_fetchall(f"""
            SELECT position, team, games, fantasy_pts, dk_pts, fd_pts,
                   fantasy_pts_per_game, dk_pts_per_game, fd_pts_per_game
            FROM hist_fantasy_points_against
            WHERE year = {ph} AND position = {ph}
            ORDER BY dk_pts_per_game DESC
        """, (sel_year, sel_position))
    else:
        rows = db_fetchall(f"""
            SELECT position, team, games, fantasy_pts, dk_pts, fd_pts,
                   fantasy_pts_per_game, dk_pts_per_game, fd_pts_per_game
            FROM hist_fantasy_points_against
            WHERE year = {ph}
            ORDER BY position, dk_pts_per_game DESC
        """, (sel_year,))

    return render_template("fantasy_points_against.html",
                           rows=rows, year=sel_year, available_years=available_years,
                           position=sel_position)


@app.route("/team-points")
def team_points():
    """
    Points scored/allowed by team, per week — same year/week selector
    pattern as History/Weather.
    """
    year_week_rows = db_fetchall(
        "SELECT DISTINCT year, week FROM hist_team_points ORDER BY year DESC, week DESC"
    )
    available = [(r["year"], r["week"]) for r in year_week_rows]

    if not available:
        return render_template("team_points.html", rows=[], year=None, week=None,
                               available_years=[], available_weeks_by_year={})

    req_year = request.args.get("year", type=int)
    req_week = request.args.get("week", type=int)

    if req_year is None or req_week is None or (req_year, req_week) not in available:
        sel_year, sel_week = available[0]
    else:
        sel_year, sel_week = req_year, req_week

    available_years = sorted({y for y, w in available}, reverse=True)
    available_weeks_by_year = {}
    for y, w in available:
        available_weeks_by_year.setdefault(y, []).append(w)
    for y in available_weeks_by_year:
        available_weeks_by_year[y].sort()
        available_weeks_by_year[y].append("season")

    req_year = request.args.get("year", type=int)
    req_week_raw = request.args.get("week")

    if req_week_raw == "season" and req_year in available_years:
        sel_year, sel_week = req_year, "season"
    else:
        req_week = request.args.get("week", type=int)
        if req_year is None or req_week is None or (req_year, req_week) not in available:
            sel_year, sel_week = available[0]
        else:
            sel_year, sel_week = req_year, req_week

    if sel_week == "season":
        ph = _ph()
        rows = db_fetchall(f"""
            SELECT team,
                   COUNT(*) AS games,
                   SUM(points_scored)  AS total_scored,
                   SUM(points_allowed) AS total_allowed,
                   AVG(points_scored)  AS avg_scored,
                   AVG(points_allowed) AS avg_allowed
            FROM hist_team_points
            WHERE year = {ph}
            GROUP BY team
            ORDER BY avg_scored DESC
        """, (sel_year,))

        return render_template("team_points.html",
                               rows=rows, is_season=True,
                               year=sel_year, week=sel_week,
                               available_years=available_years,
                               available_weeks_by_year=available_weeks_by_year)

    ph = _ph()
    rows = db_fetchall(f"""
        SELECT team, opponent, home_away, points_scored, points_allowed
        FROM hist_team_points
        WHERE year = {ph} AND week = {ph}
        ORDER BY points_scored DESC
    """, (sel_year, sel_week))

    return render_template("team_points.html",
                           rows=rows, is_season=False,
                           year=sel_year, week=sel_week,
                           available_years=available_years,
                           available_weeks_by_year=available_weeks_by_year)


def _trailing_average(history_sorted_desc: list, n: int = 10):
    """
    history_sorted_desc: list of (year, week, value) already sorted
    most-recent-first. Returns (avg, count) using up to the last n —
    averages whatever's available if fewer than n exist, rather than
    requiring a full window.
    """
    window = history_sorted_desc[:n]
    if not window:
        return None, 0
    values = [v for (_, _, v) in window]
    return sum(values) / len(values), len(values)


def _get_depth_chart_lookup(names_normalized: set) -> dict:
    """
    Shared by the Slate page (show each player's string) and Best
    Matchups (filter out anyone not on a depth chart at all) — one
    lookup, so both stay consistent with each other rather than
    re-implementing the same join twice.

    Matches purely by normalized player name, not name+position: a
    given name should only appear once across the whole depth chart
    table regardless of which of ourlads' specific position labels
    it's filed under (e.g. a WR is under LWR/RWR/SWR, never a bare
    "WR"), so we don't need to know a slate player's exact WR slot in
    advance — just look up their name and take whatever string_rank
    comes back.

    Returns {name_normalized: (string_rank, pos)}.
    """
    if not names_normalized:
        return {}
    ph = _ph()
    placeholders = ", ".join([ph] * len(names_normalized))
    rows = db_fetchall(f"""
        SELECT player_name_normalized, string_rank, pos
        FROM depth_charts
        WHERE player_name_normalized IN ({placeholders})
    """, tuple(names_normalized))
    return {r["player_name_normalized"]: (r["string_rank"], r["pos"]) for r in rows}


def _compute_best_matchups(sel_year: int, sel_week: int, sel_position: str) -> list:
    """
    Shared computation used by both the /best-matchups page and its
    CSV download, so the two can never drift out of sync. See the
    /best-matchups route's docstring for the full explanation of the
    trailing-10-game, season-boundary-crossing approach.
    """
    ph = _ph()
    if sel_position != "ALL":
        slate = db_fetchall(f"""
            SELECT name, position, team, opponent, salary, projected_pts, ownership_pct
            FROM players
            WHERE week = {ph} AND year = {ph} AND position = {ph}
        """, (sel_week, sel_year, sel_position))
    else:
        slate = db_fetchall(f"""
            SELECT name, position, team, opponent, salary, projected_pts, ownership_pct
            FROM players
            WHERE week = {ph} AND year = {ph}
              AND position IN ('QB', 'RB', 'WR', 'TE')
        """, (sel_week, sel_year))

    if not slate:
        return []

    # Filter to only players who are actually on a depth chart this
    # week — removes recently-cut/inactive players who might still
    # linger in salary data but aren't real options. Matches purely by
    # normalized name (see _get_depth_chart_lookup's docstring for why
    # that's sufficient here).
    slate_names_normalized_pre_filter = {normalize_name(p["name"]) for p in slate}
    depth_chart_lookup = _get_depth_chart_lookup(slate_names_normalized_pre_filter)
    slate = [p for p in slate if normalize_name(p["name"]) in depth_chart_lookup]

    if not slate:
        return []

    defense_rows = db_fetchall(f"""
        SELECT year, week, opponent AS team, position,
               SUM(COALESCE(dk_pts_pfr_reported, dk_pts)) AS pts_allowed
        FROM hist_player_stats
        WHERE position IN ('QB', 'RB', 'WR', 'TE')
          AND (year < {ph} OR (year = {ph} AND week < {ph}))
        GROUP BY year, week, opponent, position
    """, (sel_year, sel_year, sel_week))

    defense_history = {}
    for r in defense_rows:
        key = (r["team"], r["position"])
        defense_history.setdefault(key, []).append((r["year"], r["week"], r["pts_allowed"]))
    for key in defense_history:
        defense_history[key].sort(key=lambda x: (x[0], x[1]), reverse=True)

    slate_names_normalized = {normalize_name(p["name"]) for p in slate}
    if slate_names_normalized:
        placeholders = ", ".join([ph] * len(slate_names_normalized))
        player_rows = db_fetchall(f"""
            SELECT name_normalized, year, week, COALESCE(dk_pts_pfr_reported, dk_pts) AS dk_pts
            FROM hist_player_stats
            WHERE name_normalized IN ({placeholders})
              AND (year < {ph} OR (year = {ph} AND week < {ph}))
        """, tuple(slate_names_normalized) + (sel_year, sel_year, sel_week))
    else:
        player_rows = []

    player_history = {}
    for r in player_rows:
        player_history.setdefault(r["name_normalized"], []).append((r["year"], r["week"], r["dk_pts"]))
    for key in player_history:
        player_history[key].sort(key=lambda x: (x[0], x[1]), reverse=True)

    STRING_LABELS = {1: "1st", 2: "2nd", 3: "3rd"}

    rows = []
    for p in slate:
        name_norm = normalize_name(p["name"])
        opp_key = (p["opponent"], p["position"])
        opp_avg, opp_games = _trailing_average(defense_history.get(opp_key, []))
        player_avg, player_games = _trailing_average(player_history.get(name_norm, []))

        string_rank, _dc_pos = depth_chart_lookup.get(name_norm, (None, None))

        rows.append({
            "name": p["name"], "position": p["position"], "team": p["team"],
            "opponent": p["opponent"], "salary": p["salary"],
            "projected_pts": p["projected_pts"], "ownership_pct": p["ownership_pct"],
            "opp_avg_pts_allowed": opp_avg, "opp_games_count": opp_games,
            "player_avg_dk_pts": player_avg, "player_games_count": player_games,
            "depth_chart_string": STRING_LABELS.get(string_rank, str(string_rank) if string_rank else "—"),
        })

    rows.sort(key=lambda r: (
        r["opp_avg_pts_allowed"] if r["opp_avg_pts_allowed"] is not None else -1,
        r["player_avg_dk_pts"] if r["player_avg_dk_pts"] is not None else -1,
    ), reverse=True)

    return rows


@app.route("/best-matchups")
def best_matchups():
    """
    For a given slate (year/week from the live `players` table), shows
    every player alongside two TRAILING 10-game averages (crossing
    season boundaries when needed, e.g. Week 2 of a new season pulls
    from the end of the prior season to fill out the window):
      - the opponent's average fantasy points allowed to that position,
        derived directly from hist_player_stats (summing every
        opposing-position player's DK points each game they faced this
        team) rather than the season-total hist_fantasy_points_against
        table, which has no per-game granularity and so can't support
        a trailing window at all
      - the player's own average DK points over their last 10 games

    hist_fantasy_points_against intentionally isn't used here at all —
    it was the right tool for the season-level Fantasy Points Against
    page, but the wrong one for this trailing-window calculation.

    Sorted by opponent points allowed (descending — most exploitable
    matchups first), then by the player's own trailing average
    (descending) as the tiebreaker. Every column stays independently
    sortable via history.js for anyone who wants a different view.
    """
    year_week_rows = db_fetchall(
        "SELECT DISTINCT year, week FROM players ORDER BY year DESC, week DESC"
    )
    available = [(r["year"], r["week"]) for r in year_week_rows]

    if not available:
        return render_template("best_matchups.html", rows=[], year=None, week=None,
                               available_years=[], available_weeks_by_year={}, position="ALL",
                               team_colors=TEAM_ROW_COLORS)

    req_year = request.args.get("year", type=int)
    req_week = request.args.get("week", type=int)

    if req_year is None or req_week is None or (req_year, req_week) not in available:
        sel_year, sel_week = available[0]
    else:
        sel_year, sel_week = req_year, req_week

    sel_position = request.args.get("position", "ALL")

    available_years = sorted({y for y, w in available}, reverse=True)
    available_weeks_by_year = {}
    for y, w in available:
        available_weeks_by_year.setdefault(y, []).append(w)
    for y in available_weeks_by_year:
        available_weeks_by_year[y].sort()

    rows = _compute_best_matchups(sel_year, sel_week, sel_position)

    return render_template("best_matchups.html",
                           rows=rows, year=sel_year, week=sel_week,
                           available_years=available_years,
                           available_weeks_by_year=available_weeks_by_year,
                           position=sel_position,
                           team_colors=TEAM_ROW_COLORS)


@app.route("/depth-charts")
def depth_charts():
    """
    Standalone browse of the full depth chart data — grouped by team,
    QB/RB/WR(as its 3 ourlads slots: LWR/RWR/SWR)/TE, 1st-3rd string.
    Always reflects the latest scrape (this table is a full-replace,
    not historical — see replace_depth_charts()).
    """
    rows = db_fetchall("""
        SELECT team, pos, string_rank, player_name
        FROM depth_charts
        ORDER BY team, pos, string_rank
    """)

    if not rows:
        return render_template("depth_charts.html", positions=[], selected_team=None,
                               available_teams=[])

    available_teams = sorted({r["team"] for r in rows})
    req_team = request.args.get("team")
    sel_team = req_team if req_team in available_teams else available_teams[0]

    STRING_LABELS = {1: "1st", 2: "2nd", 3: "3rd"}

    # Group this team's rows by position, ordered QB/RB/LWR/RWR/SWR/TE
    # (matching ourlads' own Offense-table row order) rather than
    # alphabetically, since that's the order fantasy users expect.
    POSITION_ORDER = ['QB', 'RB', 'LWR', 'RWR', 'SWR', 'TE']
    by_position = {}
    for r in rows:
        if r["team"] != sel_team:
            continue
        by_position.setdefault(r["pos"], []).append({
            "string_label": STRING_LABELS.get(r["string_rank"], str(r["string_rank"])),
            "player_name": r["player_name"],
        })

    ordered_positions = [(pos, by_position[pos]) for pos in POSITION_ORDER if pos in by_position]

    return render_template("depth_charts.html",
                           positions=ordered_positions, selected_team=sel_team,
                           available_teams=available_teams)


def _score_props_for_week(year: int, week: int) -> dict:
    """
    Compares each prop's line against the real result, once that
    week's stats are loaded (via flask load-history) — no manual
    result entry needed, since every prop maps to a real
    hist_player_stats column (enforced at add-props time).

    Returns {prop_bet_id: {'actual': float_or_None, 'result':
    'over'/'under'/'push'/None}}. actual/result are None when that
    week's real stats for that player haven't loaded yet (pending).
    """
    ph = _ph()
    props = db_fetchall(f"""
        SELECT id, player_name_normalized, stat_field, line
        FROM prop_bets
        WHERE year = {ph} AND week = {ph}
    """, (year, week))

    if not props:
        return {}

    result = {}
    by_field = {}
    for p in props:
        by_field.setdefault(p["stat_field"], []).append(p)

    for stat_field, field_props in by_field.items():
        if stat_field not in VALID_PROP_STAT_FIELDS:
            # Defense in depth — add-props already validates this at
            # write time, but don't trust it blindly at read time too.
            for p in field_props:
                result[p["id"]] = {"actual": None, "result": None}
            continue

        names = {p["player_name_normalized"] for p in field_props}
        placeholders = ", ".join([ph] * len(names))
        # Composite fields (e.g. pass_rush_yds) need a SUM expression,
        # not a bare column name — everything else is a direct column.
        sql_expr = COMPOSITE_PROP_STAT_FIELDS.get(stat_field, stat_field)
        stat_rows = db_fetchall(f"""
            SELECT name_normalized, {sql_expr} AS actual_value
            FROM hist_player_stats
            WHERE year = {ph} AND week = {ph} AND name_normalized IN ({placeholders})
        """, (year, week) + tuple(names))
        actual_by_name = {r["name_normalized"]: r["actual_value"] for r in stat_rows}

        for p in field_props:
            actual = actual_by_name.get(p["player_name_normalized"])
            if actual is None:
                result[p["id"]] = {"actual": None, "result": None}
            elif actual > p["line"]:
                result[p["id"]] = {"actual": actual, "result": "over"}
            elif actual < p["line"]:
                result[p["id"]] = {"actual": actual, "result": "under"}
            else:
                result[p["id"]] = {"actual": actual, "result": "push"}   # rare with .5 lines

    return result


@app.route("/props")
def props():
    """
    NOTE — no lock mechanism yet: picks can be changed anytime,
    including after games have started. Lineups have per-game locking
    (see game_schedule); props don't yet. Worth adding the same
    protection here if this becomes a real fairness concern in
    practice.
    """
    year_week_rows = db_fetchall(
        "SELECT DISTINCT year, week FROM prop_bets ORDER BY year DESC, week DESC"
    )
    available = [(r["year"], r["week"]) for r in year_week_rows]

    if not available:
        return render_template("props.html", prop_rows=[], year=None, week=None,
                               available_years=[], available_weeks_by_year={},
                               existing_picks={}, scores={})

    req_year = request.args.get("year", type=int)
    req_week = request.args.get("week", type=int)
    if req_year is None or req_week is None or (req_year, req_week) not in available:
        sel_year, sel_week = available[0]
    else:
        sel_year, sel_week = req_year, req_week

    available_years = sorted({y for y, w in available}, reverse=True)
    available_weeks_by_year = {}
    for y, w in available:
        available_weeks_by_year.setdefault(y, []).append(w)
    for y in available_weeks_by_year:
        available_weeks_by_year[y].sort()

    ph = _ph()
    prop_rows = db_fetchall(f"""
        SELECT id, player_name, stat_field, line
        FROM prop_bets
        WHERE year = {ph} AND week = {ph}
        ORDER BY player_name
    """, (sel_year, sel_week))

    scores = _score_props_for_week(sel_year, sel_week)

    existing_picks = {}
    if current_user.is_authenticated:
        pick_rows = db_fetchall(f"""
            SELECT prop_bet_id, pick FROM prop_picks
            WHERE year = {ph} AND week = {ph} AND submitter = {ph}
        """, (sel_year, sel_week, current_user.username))
        existing_picks = {r["prop_bet_id"]: r["pick"] for r in pick_rows}

    return render_template("props.html",
                           prop_rows=prop_rows, year=sel_year, week=sel_week,
                           available_years=available_years,
                           available_weeks_by_year=available_weeks_by_year,
                           existing_picks=existing_picks, scores=scores)


@app.route("/submit-props", methods=["POST"])
@login_required
def submit_props():
    data = request.get_json(force=True)
    year = data.get("year")
    week = data.get("week")
    picks = data.get("picks")

    if not isinstance(picks, list) or len(picks) != 5:
        return jsonify(error="You must pick exactly 5 props."), 400

    prop_bet_ids = [p.get("prop_bet_id") for p in picks]
    if len(set(prop_bet_ids)) != 5:
        return jsonify(error="Duplicate prop selected."), 400

    for p in picks:
        if p.get("pick") not in ("over", "under"):
            return jsonify(error="Each pick must be 'over' or 'under'."), 400

    ph = _ph()
    placeholders = ", ".join([ph] * len(prop_bet_ids))
    valid_rows = db_fetchall(f"""
        SELECT id FROM prop_bets
        WHERE year = {ph} AND week = {ph} AND id IN ({placeholders})
    """, (year, week) + tuple(prop_bet_ids))
    valid_ids = {r["id"] for r in valid_rows}
    if len(valid_ids) != 5:
        return jsonify(error="One or more selected props are invalid for this week."), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM prop_picks WHERE year = {ph} AND week = {ph} AND submitter = {ph}",
                (year, week, current_user.username))
    for p in picks:
        cur.execute(f"""
            INSERT INTO prop_picks (year, week, submitter, prop_bet_id, pick)
            VALUES ({_ph(5)})
        """, (year, week, current_user.username, p["prop_bet_id"], p["pick"]))
    conn.commit()
    cur.close()

    return jsonify(success=True)


@app.route("/schedule")
def schedule():
    """
    Full-season schedule — every week, every game, for a given year.
    One row per game (home_away='h' side only), so this shows a real
    "Away @ Home" list rather than the two-rows-per-game structure
    game_schedule stores internally (one row per team, so per-player
    kickoff lookups are a simple (year, week, team) query elsewhere).
    """
    available_years_rows = db_fetchall(
        "SELECT DISTINCT year FROM game_schedule ORDER BY year DESC"
    )
    available_years = [r["year"] for r in available_years_rows]

    if not available_years:
        return render_template("schedule.html", games_by_week={}, weeks=[],
                               year=None, available_years=[])

    req_year = request.args.get("year", type=int)
    sel_year = req_year if req_year in available_years else available_years[0]

    ph = _ph()
    rows = db_fetchall(f"""
        SELECT week, team AS home_team, opponent AS away_team, kickoff
        FROM game_schedule
        WHERE year = {ph} AND home_away = 'h'
        ORDER BY week, kickoff
    """, (sel_year,))

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")

    # games_by_week[week] is an ordered dict of {day_name: [games]}.
    # Since rows are already sorted chronologically by kickoff (the SQL
    # query above orders by week, kickoff), inserting into a plain dict
    # here naturally produces day-groups in correct chronological order
    # (Thu, then Sun, then Mon, etc.) without needing to hardcode a
    # day-of-week ordering — this also correctly handles the occasional
    # Wed/Fri/Sat game, which just slots into wherever it falls
    # chronologically relative to the rest of that week's games.
    games_by_week = {}
    for r in rows:
        kickoff = r["kickoff"]
        display = "TBD"
        day_name = "Date TBD"
        if kickoff:
            if isinstance(kickoff, str):
                try:
                    kickoff = datetime.fromisoformat(kickoff).replace(tzinfo=timezone.utc)
                except ValueError:
                    kickoff = None
            elif kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            if kickoff:
                kickoff_eastern = kickoff.astimezone(EASTERN)
                display = kickoff_eastern.strftime("%a %m/%d %I:%M %p ET").lstrip("0").replace(" 0", " ")
                day_name = kickoff_eastern.strftime("%A")   # "Thursday", "Sunday", etc.

        week_dict = games_by_week.setdefault(r["week"], {})
        week_dict.setdefault(day_name, []).append({
            "away_team": r["away_team"],
            "home_team": r["home_team"],
            "kickoff_display": display,
        })

    weeks = sorted(games_by_week.keys())

    return render_template("schedule.html",
                           games_by_week=games_by_week, weeks=weeks,
                           year=sel_year, available_years=available_years)


@app.route("/weather")
def weather():
    """
    Weekly weather browser — past and upcoming game conditions, same
    year/week selector pattern as /history.
    """
    year_week_rows = db_fetchall(
        "SELECT DISTINCT year, week FROM hist_weather ORDER BY year DESC, week DESC"
    )
    available = [(r["year"], r["week"]) for r in year_week_rows]

    if not available:
        return render_template("weather.html", games=[], year=None, week=None,
                               available_years=[], available_weeks_by_year={})

    req_year = request.args.get("year", type=int)
    req_week = request.args.get("week", type=int)

    if req_year is None or req_week is None or (req_year, req_week) not in available:
        sel_year, sel_week = available[0]
    else:
        sel_year, sel_week = req_year, req_week

    available_years = sorted({y for y, w in available}, reverse=True)
    available_weeks_by_year = {}
    for y, w in available:
        available_weeks_by_year.setdefault(y, []).append(w)
    for y in available_weeks_by_year:
        available_weeks_by_year[y].sort()

    ph = _ph()
    games = db_fetchall(f"""
        SELECT game_date, status, away_team, home_team, away_score, home_score,
               temp_f, condition, wind_mph, wind_direction
        FROM hist_weather
        WHERE year = {ph} AND week = {ph}
        ORDER BY away_team
    """, (sel_year, sel_week))

    return render_template("weather.html",
                           games=games, year=sel_year, week=sel_week,
                           available_years=available_years,
                           available_weeks_by_year=available_weeks_by_year)


@app.route("/gameinfo")
def gameinfo():
    """
    Weekly game info browser — roof, surface, weather, vegas lines,
    attendance, etc., same year/week selector pattern as /history.
    """
    year_week_rows = db_fetchall(
        "SELECT DISTINCT year, week FROM hist_game_info ORDER BY year DESC, week DESC"
    )
    available = [(r["year"], r["week"]) for r in year_week_rows]

    if not available:
        return render_template("gameinfo.html", games=[], year=None, week=None,
                               available_years=[], available_weeks_by_year={})

    req_year = request.args.get("year", type=int)
    req_week = request.args.get("week", type=int)

    if req_year is None or req_week is None or (req_year, req_week) not in available:
        sel_year, sel_week = available[0]
    else:
        sel_year, sel_week = req_year, req_week

    available_years = sorted({y for y, w in available}, reverse=True)
    available_weeks_by_year = {}
    for y, w in available:
        available_weeks_by_year.setdefault(y, []).append(w)
    for y in available_weeks_by_year:
        available_weeks_by_year[y].sort()

    ph = _ph()
    games = db_fetchall(f"""
        SELECT team_home, team_away, date, time, location, won_toss, won_ot_toss,
               roof, surface, duration, attendance, vegas_line, over_under,
               temp, humidity, wind
        FROM hist_game_info
        WHERE year = {ph} AND week = {ph}
        ORDER BY team_home
    """, (sel_year, sel_week))

    return render_template("gameinfo.html",
                           games=games, year=sel_year, week=sel_week,
                           available_years=available_years,
                           available_weeks_by_year=available_weeks_by_year)


@app.route("/download/<data_type>")
def download_csv(data_type):
    """
    CSV export for the History, Slate, Weather, and Game Info tabs.

    If ?year=&week= are both given, respects that filter — matching
    exactly what the page itself is showing for that week.

    If neither is given (history/weather/gameinfo only), returns the
    FULL dataset across every year/week — for downloading everything at
    once instead of one week at a time.
    """
    import csv
    import io

    year = request.args.get("year", type=int)
    week = request.args.get("week", type=int)
    ph = _ph(2)

    if data_type == "slate":
        rows = db_fetchall("""
            SELECT week, year, name, position, team, opponent, salary, projected_pts, ownership_pct
            FROM players WHERE week = %s AND year = %s
        """ if _is_postgres() else """
            SELECT week, year, name, position, team, opponent, salary, projected_pts, ownership_pct
            FROM players WHERE week = ? AND year = ?
        """, (week, year))
        filename = f"slate_week{week}_{year}.csv"

    elif data_type == "history":
        if year is not None and week is not None:
            rows = db_fetchall(f"""
                SELECT s.year, s.week, s.name, s.position, s.team, s.opponent,
                       s.dk_salary, s.projected_pts, s.ownership_pct,
                       hp.dk_pts AS dk_pts_computed, hp.dk_pts_pfr_reported,
                       hp.pass_yds, hp.pass_td, hp.pass_int,
                       hp.rush_att, hp.rush_yds, hp.rush_td,
                       hp.rec, hp.rec_yds, hp.rec_td
                FROM hist_dfs_salaries s
                LEFT JOIN hist_player_stats hp
                    ON hp.year = s.year AND hp.week = s.week AND hp.name_normalized = s.name_normalized
                WHERE s.year = {_ph()} AND s.week = {_ph()}
            """, (year, week))
            filename = f"history_week{week}_{year}.csv"
        else:
            # No filter given — full dataset across every year/week we have.
            rows = db_fetchall("""
                SELECT s.year, s.week, s.name, s.position, s.team, s.opponent,
                       s.dk_salary, s.projected_pts, s.ownership_pct,
                       hp.dk_pts AS dk_pts_computed, hp.dk_pts_pfr_reported,
                       hp.pass_yds, hp.pass_td, hp.pass_int,
                       hp.rush_att, hp.rush_yds, hp.rush_td,
                       hp.rec, hp.rec_yds, hp.rec_td
                FROM hist_dfs_salaries s
                LEFT JOIN hist_player_stats hp
                    ON hp.year = s.year AND hp.week = s.week AND hp.name_normalized = s.name_normalized
                ORDER BY s.year, s.week
            """)
            filename = "history_all.csv"

    elif data_type == "weather":
        if year is not None and week is not None:
            rows = db_fetchall(f"""
                SELECT year, week, game_date, status, away_team, home_team,
                       away_score, home_score, temp_f, condition, wind_mph, wind_direction
                FROM hist_weather WHERE year = {_ph()} AND week = {_ph()}
            """, (year, week))
            filename = f"weather_week{week}_{year}.csv"
        else:
            rows = db_fetchall("""
                SELECT year, week, game_date, status, away_team, home_team,
                       away_score, home_score, temp_f, condition, wind_mph, wind_direction
                FROM hist_weather ORDER BY year, week
            """)
            filename = "weather_all.csv"

    elif data_type == "gameinfo":
        if year is not None and week is not None:
            rows = db_fetchall(f"""
                SELECT year, week, team_home, team_away, date, time, location,
                       won_toss, won_ot_toss, roof, surface, duration, attendance,
                       vegas_line, over_under, temp, humidity, wind
                FROM hist_game_info WHERE year = {_ph()} AND week = {_ph()}
            """, (year, week))
            filename = f"gameinfo_week{week}_{year}.csv"
        else:
            rows = db_fetchall("""
                SELECT year, week, team_home, team_away, date, time, location,
                       won_toss, won_ot_toss, roof, surface, duration, attendance,
                       vegas_line, over_under, temp, humidity, wind
                FROM hist_game_info ORDER BY year, week
            """)
            filename = "gameinfo_all.csv"

    elif data_type == "player":
        pfr_id = request.args.get("pfr_id", "")
        if not pfr_id:
            return jsonify(error="Missing pfr_id"), 400
        rows = db_fetchall(f"""
            SELECT hp.year, hp.week, hp.team, hp.opponent,
                   hp.dk_pts, hp.dk_pts_pfr_reported,
                   hp.pass_cmp, hp.pass_att, hp.pass_yds, hp.pass_td, hp.pass_int,
                   hp.rush_att, hp.rush_yds, hp.rush_td,
                   hp.rec_tgt, hp.rec, hp.rec_yds, hp.rec_td,
                   s.dk_salary
            FROM hist_player_stats hp
            LEFT JOIN hist_dfs_salaries s
                ON s.year = hp.year AND s.week = hp.week AND s.name_normalized = hp.name_normalized
            WHERE hp.pfr_id = {_ph()}
            ORDER BY hp.year DESC, hp.week DESC
        """, (pfr_id,))
        filename = f"player_{pfr_id}.csv"

    elif data_type == "schedule":
        rows = db_fetchall(f"""
            SELECT week, team AS home_team, opponent AS away_team, kickoff
            FROM game_schedule
            WHERE year = {_ph()} AND home_away = 'h'
            ORDER BY week, kickoff
        """, (year,))
        filename = f"schedule_{year}.csv"

    elif data_type == "fantasy-points-against":
        position = request.args.get("position", "ALL")
        if year is not None:
            if position != "ALL":
                rows = db_fetchall(f"""
                    SELECT year, position, team, games, fantasy_pts, dk_pts, fd_pts,
                           fantasy_pts_per_game, dk_pts_per_game, fd_pts_per_game
                    FROM hist_fantasy_points_against
                    WHERE year = {_ph()} AND position = {_ph()}
                    ORDER BY dk_pts_per_game DESC
                """, (year, position))
                filename = f"fantasy_points_against_{position}_{year}.csv"
            else:
                rows = db_fetchall(f"""
                    SELECT year, position, team, games, fantasy_pts, dk_pts, fd_pts,
                           fantasy_pts_per_game, dk_pts_per_game, fd_pts_per_game
                    FROM hist_fantasy_points_against
                    WHERE year = {_ph()}
                    ORDER BY position, dk_pts_per_game DESC
                """, (year,))
                filename = f"fantasy_points_against_{year}.csv"
        else:
            # No year filter — full dataset across every year we have.
            rows = db_fetchall("""
                SELECT year, position, team, games, fantasy_pts, dk_pts, fd_pts,
                       fantasy_pts_per_game, dk_pts_per_game, fd_pts_per_game
                FROM hist_fantasy_points_against
                ORDER BY year, position, dk_pts_per_game DESC
            """)
            filename = "fantasy_points_against_all.csv"

    elif data_type == "team-points":
        if year is not None and week is not None:
            rows = db_fetchall(f"""
                SELECT year, week, team, opponent, home_away, points_scored, points_allowed
                FROM hist_team_points
                WHERE year = {_ph()} AND week = {_ph()}
                ORDER BY points_scored DESC
            """, (year, week))
            filename = f"team_points_week{week}_{year}.csv"
        else:
            rows = db_fetchall("""
                SELECT year, week, team, opponent, home_away, points_scored, points_allowed
                FROM hist_team_points
                ORDER BY year, week, points_scored DESC
            """)
            filename = "team_points_all.csv"

    elif data_type == "best-matchups":
        position = request.args.get("position", "ALL")
        rows = _compute_best_matchups(year, week, position)
        filename = f"best_matchups_week{week}_{year}.csv"

    else:
        return jsonify(error="Unknown data type"), 404

    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))

    return app.response_class(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/standings")
def standings():
    """
    Season-long leaderboard: cumulative points across all submitted
    lineups for a season, with each participant's single lowest-scoring
    week dropped (per the original challenge rules).

    Scoring: each lineup's 9 players are matched against real results —
    offensive players against hist_player_stats by (year, week,
    name_normalized), and the DST pick against hist_dst_stats by
    (year, week, team), since DST names come in inconsistent formats
    across salary sources ("HOU DST", "Seahawks", "SEA", ...) and are
    resolved via team_mapping.normalize_team() rather than plain name
    matching. A week only counts toward standings once ALL 9 of a
    lineup's players (8 offense + 1 DST) have a matched result;
    otherwise that week is marked "pending" and excluded from the
    totals rather than showing a misleadingly low partial score.
    """
    ph = _ph()

    # Years come from BOTH lineups and prop picks — these are two
    # independent challenges (per the Props feature's design), so a
    # year with props submitted but no DFS lineups yet (or vice versa)
    # should still render correctly rather than showing "no lineups
    # submitted" and hiding everything, props included.
    lineup_years = db_fetchall("SELECT DISTINCT year FROM lineups")
    prop_years = db_fetchall("SELECT DISTINCT year FROM prop_picks")
    years = sorted({r["year"] for r in lineup_years} | {r["year"] for r in prop_years}, reverse=True)

    if not years:
        return render_template("standings.html", year=None, years=[],
                               standings=[], weeks=[],
                               standings_no_drop=[], weekly_top_scorers=[],
                               prop_standings=[])

    req_year = request.args.get("year", type=int)
    sel_year = req_year if req_year in years else years[0]

    lineup_rows = db_fetchall(f"""
        SELECT submitter, week, lineup_json
        FROM lineups
        WHERE year = {ph}
        ORDER BY submitter, week
    """, (sel_year,))

    # Collect every distinct name we need to score, split by whether it's
    # a DST pick (matched by team abbreviation) or an offensive player
    # (matched by normalized name) — DST names come in wildly different
    # formats depending on which salary source was used ("HOU DST",
    # "Seahawks", "SEA", ...), so they need team_mapping's normalizer
    # rather than the plain name join offense players use.
    all_names = set()
    all_teams = set()
    parsed_lineups = []   # (submitter, week, [player dicts])
    for row in lineup_rows:
        players = json.loads(row["lineup_json"])
        parsed_lineups.append((row["submitter"], row["week"], players))
        for p in players:
            if (p.get("slot") or "").upper() == "DST":
                team = normalize_team(p["name"])
                if team:
                    all_teams.add(team)
            else:
                all_names.add(normalize_name(p["name"]))

    # Pull actual scores for every (week, name_normalized) this season
    # in one query, then look them up in memory below.
    scores_by_week_name = {}   # (week, name_normalized) -> actual dk_pts
    if all_names:
        stats_rows = db_fetchall(f"""
            SELECT week, name_normalized, COALESCE(dk_pts_pfr_reported, dk_pts) AS actual_pts
            FROM hist_player_stats
            WHERE year = {ph}
        """, (sel_year,))
        for r in stats_rows:
            scores_by_week_name[(r["week"], r["name_normalized"])] = r["actual_pts"]

    # Same for DST picks, keyed by (week, team) instead of name
    scores_by_week_team = {}   # (week, team) -> dk_pts
    if all_teams:
        dst_rows = db_fetchall(f"""
            SELECT week, team, dk_pts
            FROM hist_dst_stats
            WHERE year = {ph}
        """, (sel_year,))
        for r in dst_rows:
            scores_by_week_team[(r["week"], r["team"])] = r["dk_pts"]

    # Score every lineup; a week only counts if all 9 players matched
    by_submitter = {}   # submitter -> {week: score}
    weeks_seen = set()
    for submitter, week, players in parsed_lineups:
        weeks_seen.add(week)
        total = 0.0
        matched = 0
        for p in players:
            if (p.get("slot") or "").upper() == "DST":
                team = normalize_team(p["name"])
                pts  = scores_by_week_team.get((week, team)) if team else None
            else:
                key = (week, normalize_name(p["name"]))
                pts = scores_by_week_name.get(key)

            if pts is not None:
                total += pts
                matched += 1

        by_submitter.setdefault(submitter, {})
        if matched == 9:
            by_submitter[submitter][week] = round(total, 2)
        else:
            by_submitter[submitter][week] = None   # pending / incomplete

    weeks = sorted(weeks_seen)

    # Build the leaderboard: drop each participant's single lowest
    # *fully-scored* week, sum the rest, rank descending.
    leaderboard = []
    for submitter, week_scores in by_submitter.items():
        scored_weeks = {w: s for w, s in week_scores.items() if s is not None}
        if scored_weeks:
            dropped_week = min(scored_weeks, key=lambda w: scored_weeks[w])
        else:
            dropped_week = None
        total = sum(s for w, s in scored_weeks.items() if w != dropped_week)

        leaderboard.append({
            "submitter":     submitter,
            "week_scores":   week_scores,      # includes None for pending weeks
            "dropped_week":  dropped_week,
            "total":         round(total, 2),
            "weeks_scored":  len(scored_weeks),
        })

    leaderboard.sort(key=lambda r: r["total"], reverse=True)

    # Season standings WITHOUT dropping the lowest week — same
    # scored-weeks data as above, just summed differently. Kept as a
    # separate ranking rather than folded into the existing
    # leaderboard, since the two can disagree on ordering (a
    # consistently-good-but-never-great player might rank higher with
    # no drop than someone who has one huge week and one terrible one).
    leaderboard_no_drop = []
    for submitter, week_scores in by_submitter.items():
        scored_weeks = {w: s for w, s in week_scores.items() if s is not None}
        total_no_drop = sum(scored_weeks.values())
        leaderboard_no_drop.append({
            "submitter":    submitter,
            "week_scores":  week_scores,
            "total":        round(total_no_drop, 2),
            "weeks_scored": len(scored_weeks),
        })
    leaderboard_no_drop.sort(key=lambda r: r["total"], reverse=True)

    # Weekly high scorer — for each week, whoever had the highest
    # FULLY-SCORED total that week (pending/incomplete weeks excluded,
    # same rule as everywhere else on this page). Shows every
    # participant tied for the top score that week, rather than
    # arbitrarily picking one.
    weekly_top_scorers = []
    for w in weeks:
        scores_this_week = {
            submitter: week_scores[w]
            for submitter, week_scores in by_submitter.items()
            if week_scores.get(w) is not None
        }
        if not scores_this_week:
            weekly_top_scorers.append({"week": w, "top_score": None, "winners": []})
            continue
        top_score = max(scores_this_week.values())
        winners = sorted([s for s, pts in scores_this_week.items() if pts == top_score])
        weekly_top_scorers.append({"week": w, "top_score": top_score, "winners": winners})

    # Prop Bet Challenge standings — season-long, ranked by total
    # correct picks (not accuracy %), matching "pick as many correct
    # as possible over the course of the season" as stated. Pending
    # (not-yet-scored) and push results are excluded from both the
    # numerator and denominator rather than counted as wrong.
    prop_week_rows = db_fetchall(f"""
        SELECT DISTINCT week FROM prop_picks WHERE year = {ph}
    """, (sel_year,))
    prop_weeks = sorted(r["week"] for r in prop_week_rows)

    prop_standings_by_submitter = {}   # submitter -> {"correct": n, "total_scored": n}
    for w in prop_weeks:
        week_scores = _score_props_for_week(sel_year, w)
        picks_this_week = db_fetchall(f"""
            SELECT submitter, prop_bet_id, pick FROM prop_picks
            WHERE year = {ph} AND week = {ph}
        """, (sel_year, w))
        for p in picks_this_week:
            outcome = week_scores.get(p["prop_bet_id"], {}).get("result")
            if outcome is None or outcome == "push":
                continue   # pending or push — not counted either way
            entry = prop_standings_by_submitter.setdefault(p["submitter"], {"correct": 0, "total_scored": 0})
            entry["total_scored"] += 1
            if outcome == p["pick"]:
                entry["correct"] += 1

    prop_standings = []
    for submitter, stats in prop_standings_by_submitter.items():
        prop_standings.append({
            "submitter": submitter,
            "correct": stats["correct"],
            "total_scored": stats["total_scored"],
            "accuracy_pct": round(100 * stats["correct"] / stats["total_scored"], 1) if stats["total_scored"] else None,
        })
    prop_standings.sort(key=lambda r: r["correct"], reverse=True)

    return render_template("standings.html",
                           year=sel_year, years=years,
                           standings=leaderboard, weeks=weeks,
                           standings_no_drop=leaderboard_no_drop,
                           weekly_top_scorers=weekly_top_scorers,
                           prop_standings=prop_standings)


@app.route("/submit-lineup", methods=["POST"])
@login_required
def submit_lineup():
    data      = request.get_json(force=True)
    week      = data.get("week")
    year      = data.get("year")
    players   = data.get("players", [])
    submitter = current_user.username

    if not week or not year:
        return jsonify(ok=False, error="Missing week/year.")
    if len(players) != 9:
        return jsonify(ok=False, error=f"Lineup must have exactly 9 players (got {len(players)}).")

    total_salary = sum(int(p.get("salary", 0)) for p in players)
    if total_salary > SALARY_CAP:
        return jsonify(ok=False, error=f"Salary ${total_salary:,} exceeds the ${SALARY_CAP:,} cap.")

    slot_counts = {}
    for p in players:
        slot = p.get("slot", "")
        slot_counts[slot] = slot_counts.get(slot, 0) + 1
        allowed_positions, _ = ROSTER_SLOTS.get(slot, ([], 0))
        pos = (p.get("position") or "").upper()
        if pos not in [x.upper() for x in allowed_positions]:
            return jsonify(ok=False, error=f"{p['name']} ({pos}) cannot fill the {slot} slot.")

    for slot, (_, max_count) in ROSTER_SLOTS.items():
        if slot_counts.get(slot, 0) != max_count:
            return jsonify(ok=False, error=f"Need exactly {max_count} {slot} slot(s) filled.")

    # --- Per-game lock enforcement (the real security boundary — the
    # client-side lock in slate.js is just a UX convenience; someone
    # could bypass it entirely by calling this endpoint directly, so
    # every check here is re-verified from scratch server-side). ---
    from datetime import datetime, timezone

    schedule_rows = db_fetchall(
        f"SELECT team, kickoff FROM game_schedule WHERE year = {_ph()} AND week = {_ph()}",
        (year, week)
    )
    now = datetime.now(timezone.utc)
    locked_teams = set()
    for r in schedule_rows:
        kickoff = r["kickoff"]
        if isinstance(kickoff, str):
            try:
                kickoff = datetime.fromisoformat(kickoff).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        elif kickoff is not None and kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        if kickoff and now >= kickoff:
            locked_teams.add(r["team"])

    # A player already in the submitter's PREVIOUS lineup for this
    # week is allowed to stay even if their game has since started —
    # only a NEW selection of an already-locked player is rejected.
    previous_names = set()
    if locked_teams:   # skip the lookup entirely if nothing's locked yet
        prev_row = db_fetchone(
            f"SELECT lineup_json FROM lineups WHERE week = {_ph()} AND year = {_ph()} AND submitter = {_ph()}",
            (week, year, submitter)
        )
        if prev_row:
            previous_names = {p["name"] for p in json.loads(prev_row["lineup_json"])}

    if locked_teams:
        # Look up each offensive player's team from the live slate table
        team_rows = db_fetchall(
            f"SELECT name, team FROM players WHERE week = {_ph()} AND year = {_ph()}",
            (week, year)
        )
        team_by_name = {r["name"]: r["team"] for r in team_rows}

        for p in players:
            name = p["name"]
            if name in previous_names:
                continue   # already locked in from a prior submission — allowed

            if (p.get("slot") or "").upper() == "DST":
                team = normalize_team(name)
            else:
                team = team_by_name.get(name)

            if team and team in locked_teams:
                return jsonify(ok=False, error=(
                    f"{name}'s game has already started — you can't select "
                    f"a new player from a game that's already kicked off."
                ))

    try:
        if _is_postgres():
            db_execute("""
                INSERT INTO lineups (week, year, submitter, lineup_json, total_salary)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (week, year, submitter) DO UPDATE SET
                    lineup_json  = EXCLUDED.lineup_json,
                    total_salary = EXCLUDED.total_salary,
                    submitted_at = NOW()
            """, (week, year, submitter, json.dumps(players), total_salary))
        else:
            db_execute("""
                INSERT INTO lineups (week, year, submitter, lineup_json, total_salary)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(week, year, submitter) DO UPDATE SET
                    lineup_json  = excluded.lineup_json,
                    total_salary = excluded.total_salary,
                    submitted_at = datetime('now')
            """, (week, year, submitter, json.dumps(players), total_salary))
    except Exception as e:
        return jsonify(ok=False, error=f"Database error: {e}")

    return jsonify(ok=True, message=f"Lineup submitted for Week {week}!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
