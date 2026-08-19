import os
import click
from flask import Flask, render_template, g
import sqlite3

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE = os.environ.get("DATABASE_URL", "ff_dfs.db")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Open a database connection for the current request, reusing if already open."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row  # rows behave like dicts
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    db = sqlite3.connect(DATABASE)
    db.executescript("""
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
    """)
    db.commit()
    db.close()
    print("Database initialised.")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@app.cli.command("init-db")
def init_db_command():
    """Create the database tables. Run once on first deploy."""
    init_db()


@app.cli.command("ingest-slate")
@click.option("--week",  required=True, type=int, help="NFL week number (1-18)")
@click.option("--year",  default=2025,  type=int, help="NFL season year (default 2025)")
@click.option("--slate-id", default=None, type=int,
              help="RotoWire slate ID. If omitted, the scraper will search automatically.")
def ingest_slate_command(week, year, slate_id):
    """
    Pull the current week's DraftKings salary slate from RotoWire
    and store it in the database.

    Example:
        flask ingest-slate --week 1
        flask ingest-slate --week 1 --slate-id 9276
    """
    from scraper import fetch_slate_data, find_latest_slate

    click.echo(f"Ingesting slate for week {week}, year {year}...")

    if slate_id is None:
        click.echo("No slate-id provided — searching for latest Thu-Mon Classic slate...")
        slate_id = find_latest_slate()
        if slate_id is None:
            click.echo("ERROR: Could not find a valid slate. Pass --slate-id manually.", err=True)
            return

    click.echo(f"Using slate ID: {slate_id}")
    players = fetch_slate_data(slate_id, week, year)

    if not players:
        click.echo("ERROR: No player data returned from RotoWire.", err=True)
        return

    db = sqlite3.connect(DATABASE)
    inserted = 0
    skipped  = 0
    for p in players:
        try:
            db.execute("""
                INSERT INTO players (week, year, name, position, team, opponent, salary, projected_pts, ownership_pct)
                VALUES (:week, :year, :name, :position, :team, :opponent, :salary, :projected_pts, :ownership_pct)
                ON CONFLICT(week, year, name) DO UPDATE SET
                    position      = excluded.position,
                    team          = excluded.team,
                    opponent      = excluded.opponent,
                    salary        = excluded.salary,
                    projected_pts = excluded.projected_pts,
                    ownership_pct = excluded.ownership_pct
            """, p)
            inserted += 1
        except Exception as e:
            click.echo(f"  Skipped {p.get('name','?')}: {e}")
            skipped += 1

    db.commit()
    db.close()
    click.echo(f"Done. {inserted} players upserted, {skipped} skipped.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def slate():
    """Main page: show the current week's player slate."""
    db = get_db()

    # Find the latest (week, year) that has data
    row = db.execute(
        "SELECT week, year FROM players ORDER BY year DESC, week DESC LIMIT 1"
    ).fetchone()

    if row is None:
        players     = []
        current_week = None
        current_year = None
    else:
        current_week = row["week"]
        current_year = row["year"]
        players = db.execute("""
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
        """, (current_week, current_year)).fetchall()

    return render_template("slate.html",
                           players=players,
                           week=current_week,
                           year=current_year)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
