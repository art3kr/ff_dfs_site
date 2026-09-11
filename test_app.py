"""End-to-end tests for app.py. Run with plain Python, no pytest:

    venv\\Scripts\\python.exe test_app.py        (Windows)
    python test_app.py                          (venv activated)

Exits non-zero if anything fails, so it works as a pre-push check.

-----------------------------------------------------------------------
READ THIS BEFORE ADDING A TEST
-----------------------------------------------------------------------
`.env` sets DATABASE_URL to the production Render Postgres, and app.py
calls load_dotenv() at import time. So importing app.py with no
precautions connects to PRODUCTION and runs _auto_init() against it.

The two lines that prevent that are the os.environ assignment and the
_is_postgres() assertion below, and they only work in that order:
python-dotenv defaults to override=False, so an already-set
DATABASE_URL wins over the one in .env. Nothing may import app --
directly or transitively -- above those lines.

Testing on SQLite is also a feature, not just a safety measure: it's
the only engine that reproduces the timestamp-comparison class of bug
(see test_timestamp_format), since Postgres casts strings to real
timestamps and papers over the difference.
-----------------------------------------------------------------------
"""
import datetime
import os
import sqlite3
import tempfile

DB_PATH = os.path.join(tempfile.mkdtemp(prefix="ffdfs_test_"), "test.db")
os.environ["DATABASE_URL"] = DB_PATH          # MUST precede the app import

import bcrypt                                  # noqa: E402
import app as flaskapp                          # noqa: E402

assert not flaskapp._is_postgres(), (
    "DATABASE_URL override failed -- refusing to run against Postgres"
)

YEAR, WEEK = 2026, 1
NOW = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
PAST = (NOW - datetime.timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')
FUTURE = (NOW + datetime.timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')

FAILURES = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def section(title):
    print("\n=== %s ===" % title)


# ---------------------------------------------------------------------
# Fixture: one kicked-off game (kan/den) and one still upcoming
# (buf/mia), so every lock rule has both a locked and an unlocked case.
# ---------------------------------------------------------------------

PROPS = [("Patrick Mahomes", "kan", "pass_yds", 275.5),   # locked
         ("Bo Nix",          "den", "pass_yds", 210.5),   # locked; team via props table only
         ("Josh Allen",      "buf", "pass_yds", 260.5),
         ("Stefon Diggs",    "buf", "rec_yds",   65.5),
         ("Tyreek Hill",     "mia", "rec_yds",   78.5),
         ("Tua Tagovailoa",  "mia", "pass_yds", 245.5),
         ("Raheem Mostert",  "mia", "rush_yds",  48.5)]

FREE = ["Josh Allen", "Stefon Diggs", "Tyreek Hill", "Tua Tagovailoa", "Raheem Mostert"]

# (name, team, position, depth-chart string rank or None)
SLATE = [("Patrick Mahomes", "kan", "QB", 1),      # locked team
         ("Josh Allen",      "buf", "QB", 1),
         ("Stefon Diggs",    "buf", "WR", 2),
         ("Tyreek Hill",     "mia", "WR", 3),
         ("Tua Tagovailoa",  "mia", "QB", 1),
         ("Raheem Mostert",  "mia", "RB", None)]   # no depth-chart entry

prop_ids = {}


def seed():
    conn = flaskapp._connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (username, password) VALUES (?,?)",
                ("tester", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()))
    for team, opp, ha, kick in [("kan", "den", "h", PAST), ("den", "kan", "a", PAST),
                                ("buf", "mia", "h", FUTURE), ("mia", "buf", "a", FUTURE)]:
        cur.execute("INSERT INTO game_schedule (year, week, team, opponent, home_away, kickoff) "
                    "VALUES (?,?,?,?,?,?)", (YEAR, WEEK, team, opp, ha, kick))

    for name, team, field, line in PROPS:
        cur.execute("INSERT INTO prop_bets (year, week, player_name, player_name_normalized, "
                    "stat_field, line) VALUES (?,?,?,?,?,?)",
                    (YEAR, WEEK, name, flaskapp.normalize_name(name), field, line))
        prop_ids[name] = cur.lastrowid

    # Bo Nix is deliberately absent from `players` so his team has to
    # resolve through the scoresandodds_props fallback instead.
    cur.execute("INSERT INTO scoresandodds_props (category, player_name, "
                "player_name_normalized, team) VALUES (?,?,?,?)",
                ("passing-yards", "Bo Nix", flaskapp.normalize_name("Bo Nix"), "den"))

    for name, team, pos, rank in SLATE:
        cur.execute("INSERT INTO players (week, year, name, position, team, opponent, salary) "
                    "VALUES (?,?,?,?,?,?,?)", (WEEK, YEAR, name, pos, team, "xxx", 7000))
        if rank is not None:
            cur.execute("INSERT INTO depth_charts (team, pos, string_rank, player_name, "
                        "player_name_normalized) VALUES (?,?,?,?,?)",
                        (team, pos, rank, name, flaskapp.normalize_name(name)))
    conn.commit()
    conn.close()


seed()
client = flaskapp.app.test_client()
client.post("/login", data={"username": "tester", "password": "pw"}, follow_redirects=True)


def submit(picks):
    r = client.post("/submit-props", json={"year": YEAR, "week": WEEK, "picks": picks})
    return r.status_code, r.get_json()


def pick(name, side="over"):
    return {"prop_bet_id": prop_ids[name], "pick": side}


def seed_existing_picks(pairs):
    conn = flaskapp._connect()
    conn.execute("DELETE FROM prop_picks")
    for name, side in pairs:
        conn.execute("INSERT INTO prop_picks (year, week, submitter, prop_bet_id, pick) "
                     "VALUES (?,?,?,?,?)", (YEAR, WEEK, "tester", prop_ids[name], side))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------

def test_player_team_resolution():
    section("_get_player_teams resolution order")
    with flaskapp.app.app_context():
        teams = flaskapp._get_player_teams(
            YEAR, WEEK, {flaskapp.normalize_name(n) for n, _, _, _ in PROPS})
    check("resolves from `players`", teams.get("patrick mahomes") == "kan")
    check("falls back to scoresandodds_props", teams.get("bo nix") == "den")
    check("unknown name is simply absent", "nobody at all" not in teams)


def test_props_lock():
    section("props per-game lock (submit_props)")
    code, body = submit([pick(n) for n in FREE])
    check("5 unlocked props submit cleanly", code == 200 and body.get("success"))

    code, body = submit([pick("Patrick Mahomes")] + [pick(n) for n in FREE[:4]])
    check("adding a locked prop is rejected",
          code == 400 and "already started" in (body.get("error") or ""))

    code, body = submit([pick("Bo Nix")] + [pick(n) for n in FREE[:4]])
    check("locked via props-table fallback is also rejected",
          code == 400 and "already started" in (body.get("error") or ""))

    seed_existing_picks([("Patrick Mahomes", "over")] + [(n, "over") for n in FREE[:4]])

    code, body = submit([pick("Patrick Mahomes", "over")] + [pick(n) for n in FREE[:4]])
    check("resubmitting an UNCHANGED locked pick is allowed",
          code == 200 and body.get("success"))

    seed_existing_picks([("Patrick Mahomes", "over")] + [(n, "over") for n in FREE[:4]])
    code, body = submit([pick("Patrick Mahomes", "under")] + [pick(n) for n in FREE[:4]])
    check("flipping over->under on a locked pick is rejected",
          code == 400 and "add or change" in (body.get("error") or ""))

    code, body = submit([pick(n) for n in FREE])
    check("dropping a locked pick is rejected",
          code == 400 and "remove a pick" in (body.get("error") or ""))

    # submitted_at goes straight back to the frontend for the "Picks
    # submitted" banner. An isoformat 'T' there made the banner read
    # differently before and after a page reload.
    seed_existing_picks([])
    _, body = submit([pick(n) for n in FREE])
    check("submitted_at has no 'T' separator",
          "T" not in (body or {}).get("submitted_at", "T"))


def test_props_page():
    section("props page renders the lock")
    html = client.get("/props").get_data(as_text=True)
    check("LOCKED badge present", "locked-badge" in html)
    check("locked card class present", "prop-locked" in html)
    check("locked buttons disabled", "disabled" in html)
    check("unlocked props still pickable", html.count('data-locked="false"') >= len(FREE))


def test_slate_filter_attributes():
    section("slate filter controls and row data contract")
    html = client.get("/").get_data(as_text=True)

    for ctrl in ["player-search", "hide-locked", "hide-2nd-string", "hide-3rd-string"]:
        check("control #%s present" % ctrl, ('id="%s"' % ctrl) in html)

    # applyFilters() reads these three dataset keys off every row.
    check("rows carry data-name", 'data-name="Josh Allen"' in html)
    check("rows carry data-position", 'data-position="QB"' in html)
    check("rows carry data-locked", 'data-locked="true"' in html and
                                    'data-locked="false"' in html)

    # The exact literals the JS compares against.
    for label in ["1st", "2nd", "3rd"]:
        check('data-string="%s" emitted' % label, ('data-string="%s"' % label) in html)

    # Row attributes span several lines, so these must be checked per
    # ROW BLOCK, not per line -- a line-based search silently passes
    # for the wrong reason, since data-name and data-locked never
    # appear on the same line.
    def row_for(player):
        for block in html.split("<tr ")[1:]:
            if ('data-name="%s"' % player) in block:
                return block.split("</tr>")[0]
        return None

    mostert = row_for("Raheem Mostert")
    check("no-depth-chart player rendered", mostert is not None)
    # Must not be caught by either depth filter -- unknown, not
    # known-to-be-deep.
    check("no-depth-chart player is neither 2nd nor 3rd",
          mostert is not None
          and 'data-string="2nd"' not in mostert
          and 'data-string="3rd"' not in mostert)

    # Locked rows must be distinguishable for the hide-locked filter.
    mahomes = row_for("Patrick Mahomes")
    check("player on a kicked-off game is data-locked=true",
          mahomes is not None and 'data-locked="true"' in mahomes)
    allen = row_for("Josh Allen")
    check("player on an upcoming game is data-locked=false",
          allen is not None and 'data-locked="false"' in allen)
    check("2nd stringer's row carries data-string=\"2nd\"",
          (row_for("Stefon Diggs") or "").count('data-string="2nd"') == 1)

    ncols = html.count('<th data-col=')
    check("day-header colspan matches %d columns" % ncols,
          ('colspan="%d"' % ncols) in html)


def test_routes_smoke():
    section("every GET route returns 200")
    for path in ["/", "/history", "/team-points", "/usage", "/props", "/my-props",
                 "/my-lineups", "/standings", "/schedule", "/weather", "/gameinfo",
                 "/depth-charts", "/implied-points", "/implied-team-points",
                 "/best-matchups", "/game-overview", "/fantasy-points-against"]:
        check("GET %-26s" % path, client.get(path).status_code == 200)


def test_timestamp_format():
    """Runs LAST -- it rewrites game_schedule."""
    section("timestamp format vs SQLite lexicographic comparison")
    probe = sqlite3.connect(":memory:")
    cutoff = NOW - datetime.timedelta(hours=24)
    same_day_kickoff = cutoff.strftime('%Y-%m-%d') + " 23:59:59"

    old_ok = probe.execute("SELECT ? >= ?",
                           (same_day_kickoff, cutoff.isoformat())).fetchone()[0]
    new_ok = probe.execute("SELECT ? >= ?",
                           (same_day_kickoff,
                            cutoff.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()[0]
    check("the old .isoformat() form compares WRONG (bug reproduced)", old_ok == 0)
    check("the strftime form compares correctly", new_ok == 1)

    conn = flaskapp._connect()
    conn.execute("DELETE FROM game_schedule")
    conn.execute("INSERT INTO game_schedule (year, week, team, opponent, home_away, kickoff) "
                 "VALUES (?,?,?,?,?,?)", (YEAR, 7, "kan", "den", "h", same_day_kickoff))
    conn.commit()
    conn.close()
    with flaskapp.app.app_context():
        got = flaskapp._get_current_nfl_week()
    check("_get_current_nfl_week finds a same-date week (got %s)" % (got,),
          got == (YEAR, 7))


if __name__ == "__main__":
    print("test database: %s" % DB_PATH)
    test_player_team_resolution()
    test_props_lock()
    test_props_page()
    test_slate_filter_attributes()
    test_routes_smoke()
    test_timestamp_format()          # must stay last; rewrites game_schedule

    print()
    if FAILURES:
        print("%d FAILED:" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
    else:
        print("ALL CHECKS PASSED")
    raise SystemExit(1 if FAILURES else 0)
