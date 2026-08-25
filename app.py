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
        """)

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
