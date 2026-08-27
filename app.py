import os
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

ROSTER_SLOTS = {
    "QB":   (["QB"],                     1),
    "RB":   (["RB"],                     2),
    "WR":   (["WR"],                     3),
    "TE":   (["TE"],                     1),
    "FLEX": (["RB", "WR", "TE"],         1),
    "DST":  (["DST", "D", "DEF"],        1),
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


def _migrate_hist_player_stats_pg(cur):
    """Add any missing hist_player_stats columns on Postgres."""
    for col, coltype in HIST_PLAYER_STATS_MIGRATIONS:
        cur.execute(f"ALTER TABLE hist_player_stats ADD COLUMN IF NOT EXISTS {col} {coltype}")


def _migrate_hist_player_stats_sqlite(conn):
    """Add any missing hist_player_stats columns on SQLite (no IF NOT
    EXISTS support for ADD COLUMN, so check existing columns first)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(hist_player_stats)")}
    for col, coltype in HIST_PLAYER_STATS_MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE hist_player_stats ADD COLUMN {col} {coltype}")


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
        """)
        _migrate_hist_player_stats_sqlite(conn)

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


@app.cli.command("load-history")
@click.option("--data-dir", default="data",
              help="Folder containing the .csv.gz files produced by the scrapers.")
@click.option("--salaries-only", is_flag=True, help="Only load salary files, skip player stats.")
@click.option("--stats-only",    is_flag=True, help="Only load player stats, skip salary files.")
@click.option("--batch-size", default=1000, type=int, help="Rows per bulk-insert batch.")
def load_history_command(data_dir, salaries_only, stats_only, batch_size):
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

    # --- Load salary files ---
    if not stats_only:
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

    # --- Load player stats file ---
    if not salaries_only:
        path = os.path.join(data_dir, STATS_FILE)
        if not os.path.exists(path):
            click.echo(f"Skip (not found): {path}")
        else:
            click.echo(f"Loading {path} ...")
            df = pd.read_csv(path)
            count = upsert_stats(df)
            click.echo(f"  Done: {count:,} rows from {STATS_FILE}")

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

    return render_template("slate.html",
                           players=players,
                           week=current_week,
                           year=current_year,
                           salary_cap=SALARY_CAP,
                           existing_lineup=existing_lineup)


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
                           players=players,
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


@app.route("/standings")
def standings():
    """
    Season-long leaderboard: cumulative points across all submitted
    lineups for a season, with each participant's single lowest-scoring
    week dropped (per the original challenge rules).

    Scoring: each lineup's 9 players are matched against
    hist_player_stats by (year, week, name_normalized) — the same join
    History/Player pages use. A week only counts toward standings once
    ALL 9 of a lineup's players have a matched stats row; otherwise
    that week is marked "pending" and excluded from the totals rather
    than showing a misleadingly low partial score (e.g. games not yet
    played, or this week's data not yet scraped).
    """
    ph = _ph()

    available_years = db_fetchall(
        "SELECT DISTINCT year FROM lineups ORDER BY year DESC"
    )
    years = [r["year"] for r in available_years]

    if not years:
        return render_template("standings.html", year=None, years=[],
                               standings=[], weeks=[])

    req_year = request.args.get("year", type=int)
    sel_year = req_year if req_year in years else years[0]

    lineup_rows = db_fetchall(f"""
        SELECT submitter, week, lineup_json
        FROM lineups
        WHERE year = {ph}
        ORDER BY submitter, week
    """, (sel_year,))

    # Collect every distinct name_normalized we need to score, in one
    # batch, rather than one query per player per lineup.
    all_names = set()
    parsed_lineups = []   # (submitter, week, [player dicts])
    for row in lineup_rows:
        players = json.loads(row["lineup_json"])
        parsed_lineups.append((row["submitter"], row["week"], players))
        for p in players:
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

    # Score every lineup; a week only counts if all 9 players matched
    by_submitter = {}   # submitter -> {week: score}
    weeks_seen = set()
    for submitter, week, players in parsed_lineups:
        weeks_seen.add(week)
        total = 0.0
        matched = 0
        for p in players:
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

    return render_template("standings.html",
                           year=sel_year, years=years,
                           standings=leaderboard, weeks=weeks)


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
