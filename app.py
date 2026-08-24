import os
import json
import click
from flask import Flask, render_template, g, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import sqlite3
import bcrypt

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE   = os.environ.get("DATABASE_URL", "ff_dfs.db")
SALARY_CAP = 50_000

# Roster rules: slot -> (eligible positions, max count)
ROSTER_SLOTS = {
    "QB":   (["QB"],                     1),
    "RB":   (["RB"],                     2),
    "WR":   (["WR"],                     3),
    "TE":   (["TE"],                     1),
    "FLEX": (["RB", "WR", "TE"],         1),
    "DST":  (["DST", "D", "DEF"],        1),
}

# ---------------------------------------------------------------------------
# Flask-Login setup
# ---------------------------------------------------------------------------

login_manager = LoginManager(app)
login_manager.login_view      = "login"          # redirect here if @login_required fails
login_manager.login_message   = "Please log in to submit a lineup."
login_manager.login_message_category = "info"


class User(UserMixin):
    """Minimal user object Flask-Login needs. Loaded from the DB each request."""
    def __init__(self, id_, username):
        self.id       = id_
        self.username = username


@login_manager.user_loader
def load_user(user_id):
    db  = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    db.close()
    if row:
        return User(row["id"], row["username"])
    return None


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


@app.cli.command("create-user")
@click.option("--username", required=True, prompt=True)
@click.option("--password", required=True, prompt=True, hide_input=True,
              confirmation_prompt=True)
def create_user_command(username, password):
    """
    Create a participant account. Run once per participant.

    Example:
        flask create-user --username art3kr --password secret
        flask create-user          # will prompt interactively
    """
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = sqlite3.connect(DATABASE)
    try:
        db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
        db.commit()
        click.echo(f"User '{username}' created.")
    except sqlite3.IntegrityError:
        click.echo(f"ERROR: Username '{username}' already exists.", err=True)
    finally:
        db.close()


@app.cli.command("list-users")
def list_users_command():
    """List all registered participants."""
    db = sqlite3.connect(DATABASE)
    rows = db.execute("SELECT id, username FROM users ORDER BY username").fetchall()
    db.close()
    if not rows:
        click.echo("No users yet. Run: flask create-user")
    else:
        click.echo(f"{'ID':>4}  Username")
        click.echo("-" * 24)
        for r in rows:
            click.echo(f"{r[0]:>4}  {r[1]}")


@app.cli.command("delete-user")
@click.option("--username", required=True, prompt=True)
def delete_user_command(username):
    """Remove a participant account."""
    db = sqlite3.connect(DATABASE)
    cur = db.execute("DELETE FROM users WHERE username = ?", (username,))
    db.commit()
    db.close()
    if cur.rowcount:
        click.echo(f"User '{username}' deleted.")
    else:
        click.echo(f"No user named '{username}' found.", err=True)


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

        db  = get_db()
        row = db.execute(
            "SELECT id, username, password FROM users WHERE username = ?", (username,)
        ).fetchone()

        if row and bcrypt.checkpw(password.encode(), row["password"].encode()):
            user = User(row["id"], row["username"])
            login_user(user)
            # Redirect to page they were trying to reach, or home
            next_page = request.args.get("next") or url_for("slate")
            return redirect(next_page)
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

    # If logged in, check if they already submitted this week
    existing_lineup = None
    if current_user.is_authenticated and current_week:
        row2 = db.execute("""
            SELECT lineup_json, total_salary, submitted_at
            FROM   lineups
            WHERE  week = ? AND year = ? AND submitter = ?
        """, (current_week, current_year, current_user.username)).fetchone()
        if row2:
            existing_lineup = {
                "players":      json.loads(row2["lineup_json"]),
                "total_salary": row2["total_salary"],
                "submitted_at": row2["submitted_at"],
            }

    return render_template("slate.html",
                           players=players,
                           week=current_week,
                           year=current_year,
                           salary_cap=SALARY_CAP,
                           existing_lineup=existing_lineup)


@app.route("/submit-lineup", methods=["POST"])
@login_required
def submit_lineup():
    """
    Receive a lineup as JSON. Submitter comes from session, not request body.
    Returns JSON { ok: true } or { ok: false, error: "..." }
    """
    data      = request.get_json(force=True)
    week      = data.get("week")
    year      = data.get("year")
    players   = data.get("players", [])
    submitter = current_user.username   # always from session — never trust client

    if not week or not year:
        return jsonify(ok=False, error="Missing week/year.")
    if len(players) != 9:
        return jsonify(ok=False, error=f"Lineup must have exactly 9 players (got {len(players)}).")

    total_salary = sum(int(p.get("salary", 0)) for p in players)
    if total_salary > SALARY_CAP:
        return jsonify(ok=False, error=f"Salary ${total_salary:,} exceeds the ${SALARY_CAP:,} cap.")

    # Slot validation
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

    return jsonify(ok=True, message=f"Lineup submitted for Week {week}!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
