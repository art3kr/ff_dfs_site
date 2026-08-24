import os
import json
import click
from flask import Flask, render_template, g, request, jsonify
import sqlite3

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE = os.environ.get("DATABASE_URL", "ff_dfs.db")

# Roster rules: slot -> (eligible positions, max count)
ROSTER_SLOTS = {
    "QB":   (["QB"],                    1),
    "RB":   (["RB"],                    2),
    "WR":   (["WR"],                    3),
    "TE":   (["TE"],                    1),
    "FLEX": (["RB", "WR", "TE"],        1),
    "DST":  (["DST", "D", "DEF"],       1),
}
SALARY_CAP = 50_000

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
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
@click.option("--week",     required=True, type=int)
@click.option("--year",     default=2026,  type=int, help="NFL season year (default 2026)")
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

    db = sqlite3.connect(DATABASE)
    inserted = skipped = 0
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
    """Main page: player slate table + lineup builder."""
    db = get_db()

    row = db.execute(
        "SELECT week, year FROM players ORDER BY year DESC, week DESC LIMIT 1"
    ).fetchone()

    if row is None:
        players      = []
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
                           year=current_year,
                           salary_cap=SALARY_CAP)


@app.route("/submit-lineup", methods=["POST"])
def submit_lineup():
    """
    Receive a lineup submission as JSON.
    Body: { submitter, week, year, players: [{name, position, salary, slot}, ...] }
    Returns JSON { ok: true } or { ok: false, error: "..." }
    """
    data = request.get_json(force=True)

    submitter = (data.get("submitter") or "").strip()
    week      = data.get("week")
    year      = data.get("year")
    players   = data.get("players", [])

    # --- Basic validation ---
    if not submitter:
        return jsonify(ok=False, error="Name is required.")
    if not week or not year:
        return jsonify(ok=False, error="Missing week/year.")
    if len(players) != 9:
        return jsonify(ok=False, error=f"Lineup must have exactly 9 players (got {len(players)}).")

    total_salary = sum(int(p.get("salary", 0)) for p in players)
    if total_salary > SALARY_CAP:
        return jsonify(ok=False, error=f"Salary ${total_salary:,} exceeds cap ${SALARY_CAP:,}.")

    # --- Slot validation ---
    slot_counts = {}
    for p in players:
        slot = p.get("slot", "")
        slot_counts[slot] = slot_counts.get(slot, 0) + 1
        allowed_positions, _ = ROSTER_SLOTS.get(slot, ([], 0))
        pos = (p.get("position") or "").upper()
        if pos not in [x.upper() for x in allowed_positions]:
            return jsonify(ok=False, error=f"{p['name']} ({pos}) cannot fill {slot} slot.")

    for slot, (_, max_count) in ROSTER_SLOTS.items():
        if slot_counts.get(slot, 0) != max_count:
            return jsonify(ok=False, error=f"Need exactly {max_count} {slot} slot(s) filled.")

    # --- Persist ---
    db = get_db()
    try:
        db.execute("""
            INSERT INTO lineups (week, year, submitter, lineup_json, total_salary)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(week, year, submitter) DO UPDATE SET
                lineup_json  = excluded.lineup_json,
                total_salary = excluded.total_salary,
                submitted_at = datetime('now')
        """, (week, year, submitter, json.dumps(players), total_salary))
        db.commit()
    except Exception as e:
        return jsonify(ok=False, error=f"Database error: {e}")

    return jsonify(ok=True, message=f"Lineup submitted for {submitter}, Week {week}!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
