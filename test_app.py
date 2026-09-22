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

# A full 9-slot lineup with known points, so standings totals are
# arithmetic we can actually assert rather than "didn't crash".
# Week 1 sums to 89.0, week 2 to 50.0 (each player worth half).
LINEUP = [("QB",   "Patrick Mahomes", "QB",  20.0),
          ("RB",   "Raheem Mostert",  "RB",  10.0),
          ("RB",   "James Cook",      "RB",   5.0),
          ("WR",   "Stefon Diggs",    "WR",  12.0),
          ("WR",   "Tyreek Hill",     "WR",  15.0),
          ("WR",   "Jaylen Waddle",   "WR",   3.0),
          ("TE",   "Travis Kelce",    "TE",   7.0),
          ("FLEX", "Rashee Rice",     "WR",   8.0),
          ("DST",  "Buffalo Bills",   "DST",  9.0)]   # scored via hist_dst_stats
WEEK1_TOTAL = 89.0
WEEK2_TOTAL = 44.5    # half of each offensive player, DST included

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

    # Per-GAME tables, so the merge-friendly reshape has something to
    # reshape. Two weeks, so "All Data" is provably broader than the
    # week-scoped export rather than coincidentally equal.
    for wk, (away, home, ascore, hscore) in [(1, ("den", "kan", 17, 24)),
                                             (2, ("mia", "buf", 20, 13))]:
        cur.execute(
            "INSERT INTO hist_weather (year, week, game_date, status, away_team, "
            "home_team, away_score, home_score, temp_f, condition, wind_mph, "
            "wind_direction) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (YEAR, wk, "2026-09-1%d" % wk, "Final", away, home, ascore, hscore,
             72, "Clear", 5, "SW"))
        cur.execute(
            "INSERT INTO hist_game_info (boxscore_url, year, week, team_home, "
            "team_away, date, roof, surface) VALUES (?,?,?,?,?,?,?,?)",
            ("/boxscores/2026%02d.htm" % wk, YEAR, wk, home, away,
             "2026-09-1%d" % wk, "outdoors", "grass"))

    # Two scored weeks of one submitted lineup, so standings can be
    # checked as real arithmetic (including the drop-lowest-week rule).
    import json as _json
    for wk, factor in [(1, 1.0), (2, 0.5)]:
        players = [{"slot": slot, "name": name, "position": pos, "salary": 5000}
                   for slot, name, pos, _ in LINEUP]
        cur.execute("INSERT INTO lineups (week, year, submitter, lineup_json, total_salary) "
                    "VALUES (?,?,?,?,?)",
                    (wk, YEAR, "tester", _json.dumps(players), 45000))
        for slot, name, pos, pts in LINEUP:
            if slot == "DST":
                cur.execute("INSERT INTO hist_dst_stats (year, week, team, dk_pts) "
                            "VALUES (?,?,?,?)",
                            (YEAR, wk, flaskapp.normalize_team(name), pts * factor))
            else:
                cur.execute(
                    "INSERT INTO hist_player_stats (pfr_id, name, name_normalized, year, "
                    "week, team, position, dk_pts) VALUES (?,?,?,?,?,?,?,?)",
                    ("%s%02d" % (flaskapp.normalize_name(name)[:6].replace(" ", ""), wk),
                     name, flaskapp.normalize_name(name), YEAR, wk, "buf", pos,
                     pts * factor))
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


def test_non_counting_week():
    section("non-counting week: shown, left out of season totals")
    import csv as _csv
    import io as _io
    saved_weeks, saved_scorer = flaskapp.NON_COUNTING_WEEKS, flaskapp._score_props_for_week
    flaskapp.NON_COUNTING_WEEKS = {YEAR: {1}}
    # One correct week-1 prop pick, so the prop table has something to leave out.
    seed_existing_picks([("Josh Allen", "over")])
    flaskapp._score_props_for_week = lambda y, w: {prop_ids["Josh Allen"]: {"result": "over"}} if w == 1 else {}
    try:
        html = client.get("/standings").get_data(as_text=True)
        no_drop = html.split('id="standings-no-drop-table"', 1)[1].split("</table>", 1)[0]
        props = html.split('id="prop-standings-table"', 1)[1].split("</table>", 1)[0]
        check("week 1 score still shown (%.1f)" % WEEK1_TOTAL, "%.1f" % WEEK1_TOTAL in html)
        check("week 1 header marked with *", "Wk1*" in html)
        check("no-drop total is week 2 only (%.1f)" % WEEK2_TOTAL,
              "%.1f" % WEEK2_TOTAL in no_drop and "%.1f" % (WEEK1_TOTAL + WEEK2_TOTAL) not in no_drop)
        check("weekly high scorer still lists week 1, marked not counted", "not counted" in html)
        check("week 1 prop result still shown (1/1)", "1/1" in props)
        check("week 1 prop left out of season Correct", "<strong>0</strong>" in props)

        rows = list(_csv.DictReader(_io.StringIO(
            client.get("/download/standings?year=%d" % YEAR).get_data(as_text=True))))
        wk = {r["week"]: r for r in rows if r["submitter"] == "tester"}
        check("CSV marks week 1 counts_toward_season False",
              wk.get("1", {}).get("counts_toward_season") == "False")
        check("CSV never drops the non-counting week",
              wk.get("1", {}).get("is_dropped_week") == "False")
        check("only one counting week, so nothing is dropped",
              wk.get("2", {}).get("is_dropped_week") == "False")
        drop_table = html.split('id="standings-table"', 1)[1].split("</table>", 1)[0]
        check("drop-lowest total keeps the lone counting week (%.1f, not 0.0)" % WEEK2_TOTAL,
              'total-col">%.1f' % WEEK2_TOTAL in drop_table)
    finally:
        flaskapp.NON_COUNTING_WEEKS, flaskapp._score_props_for_week = saved_weeks, saved_scorer
        seed_existing_picks([])


def test_standings_scoring():
    section("standings scoring (_score_lineups_for_year)")
    flaskapp.NON_COUNTING_WEEKS = {}   # plain season math; the rule has its own test
    with flaskapp.app.app_context():
        by_submitter, weeks = flaskapp._score_lineups_for_year(YEAR)
    check("both weeks present", weeks == [1, 2])
    check("week 1 totals %.1f (got %s)" % (WEEK1_TOTAL, by_submitter["tester"].get(1)),
          by_submitter["tester"].get(1) == WEEK1_TOTAL)
    check("week 2 totals %.1f (got %s)" % (WEEK2_TOTAL, by_submitter["tester"].get(2)),
          by_submitter["tester"].get(2) == WEEK2_TOTAL)

    html = client.get("/standings").get_data(as_text=True)
    check("page shows the no-drop total (%.1f)" % (WEEK1_TOTAL + WEEK2_TOTAL),
          "%.1f" % (WEEK1_TOTAL + WEEK2_TOTAL) in html)
    check("page shows the drop-lowest total (%.1f)" % WEEK1_TOTAL,
          "%.1f" % WEEK1_TOTAL in html)

    # A lineup missing even one real result must not score at all.
    conn = flaskapp._connect()
    conn.execute("DELETE FROM hist_player_stats WHERE week = 2 AND name_normalized = ?",
                 (flaskapp.normalize_name("Travis Kelce"),))
    conn.commit()
    conn.close()
    with flaskapp.app.app_context():
        by_submitter, _ = flaskapp._score_lineups_for_year(YEAR)
    check("a week with an unmatched player scores None (pending)",
          by_submitter["tester"].get(2) is None)


def test_name_suffixes():
    section("name suffixes (Jr./Sr./II-V) don't break the join key")
    n = flaskapp.normalize_name
    for raw, want in [("James Cook III", "james cook"), ("Brian Thomas Jr.", "brian thomas"),
                      ("Patrick Mahomes II", "patrick mahomes"), ("Stetson Bennett IV", "stetson bennett"),
                      ("Deebo Samuel Sr.", "deebo samuel"), ("Kenneth  Walker III", "kenneth walker"),
                      ("Josh Allen", "josh allen"), ("Tre V", "tre v")]:
        check("normalize_name(%r) == %r (got %r)" % (raw, want, n(raw)), n(raw) == want)

    k = flaskapp._name_key
    check("_name_key keeps a stored key over a 'Last, First' display name",
          k("peyton manning", "Manning, Peyton") == "peyton manning")
    check("_name_key drops a suffix from a stored key",
          k("robert griffin iii", "Griffin III, Robert") == "robert griffin")
    check("_name_key falls back to the name when the stored key is NaN",
          k(float("nan"), "James Cook III") == "james cook")

    # Salary-style names in the lineup vs PFR-style names in the stats,
    # suffixes mismatched in both directions. All 9 must match to score.
    import json as _json
    wk, total = 3, 72.0
    pairs = [("QB",   "Patrick Mahomes II", "Patrick Mahomes",     20.0),
             ("RB",   "James Cook III",     "James Cook",          10.0),
             ("RB",   "Kenneth Walker",     "Kenneth Walker III",   9.0),
             ("WR",   "Brian Thomas Jr.",   "Brian Thomas",         8.0),
             ("WR",   "Marvin Harrison",    "Marvin Harrison Jr.",  7.0),
             ("WR",   "Deebo Samuel Sr.",   "Deebo Samuel",         6.0),
             ("TE",   "Harold Fannin Jr.",  "Harold Fannin",        5.0),
             ("FLEX", "Luther Burden III",  "Luther Burden",        4.0)]
    conn = flaskapp._connect()
    lineup = [{"slot": s, "name": ln, "position": s, "salary": 5000} for s, ln, _, _ in pairs]
    lineup.append({"slot": "DST", "name": "Buffalo Bills", "position": "DST", "salary": 3000})
    conn.execute("INSERT INTO lineups (week, year, submitter, lineup_json, total_salary) "
                 "VALUES (?,?,?,?,?)", (wk, YEAR, "suffixes", _json.dumps(lineup), 43000))
    for i, (_, _, stats_name, pts) in enumerate(pairs):
        pfr_key = stats_name.lower().replace(".", "")      # the key as PFR's CSV carries it
        conn.execute("INSERT INTO hist_player_stats (pfr_id, name, name_normalized, year, week, "
                     "team, dk_pts) VALUES (?,?,?,?,?,?,?)",
                     ("SUFX%02d" % i, stats_name, k(pfr_key, stats_name), YEAR, wk, "buf", pts))
    conn.execute("INSERT INTO hist_dst_stats (year, week, team, dk_pts) VALUES (?,?,?,?)",
                 (YEAR, wk, "buf", 3.0))
    conn.commit()
    conn.close()
    with flaskapp.app.app_context():
        by_submitter, _ = flaskapp._score_lineups_for_year(YEAR)
    got = by_submitter.get("suffixes", {}).get(wk)
    check("lineup with mismatched suffixes fully scores %.1f (got %s)" % (total, got), got == total)

    # renormalize-names rewrites old keys, skips a UNIQUE collision, and
    # a dry run writes nothing.
    conn = flaskapp._connect()
    conn.executemany("INSERT INTO hist_dfs_salaries (week, year, name, name_normalized, source) "
                     "VALUES (?,?,?,?,?)",
                     [(9, YEAR, "James Cook III", "james cook iii", "t"),
                      (9, YEAR, "Foo Bar Jr.", "foo bar jr", "t"),
                      (9, YEAR, "Foo Bar", "foo bar", "t")])
    conn.execute("INSERT INTO prop_bets (year, week, player_name, player_name_normalized, "
                 "stat_field, line) VALUES (?,?,?,?,?,?)",
                 (YEAR, 9, "Brian Thomas Jr.", "brian thomas jr", "rec_yds", 55.5))
    conn.commit()
    conn.close()

    def keys():
        c = flaskapp._connect()
        sal = [r[0] for r in c.execute(
            "SELECT name_normalized FROM hist_dfs_salaries WHERE week = 9 ORDER BY id")]
        prop = c.execute("SELECT player_name_normalized FROM prop_bets WHERE week = 9").fetchone()[0]
        c.close()
        return sal, prop

    runner = flaskapp.app.test_cli_runner()
    dry = runner.invoke(args=["renormalize-names", "--dry-run"])
    check("renormalize-names --dry-run exits cleanly", dry.exit_code == 0)
    check("dry run writes nothing",
          keys() == (["james cook iii", "foo bar jr", "foo bar"], "brian thomas jr"))
    real = runner.invoke(args=["renormalize-names"])
    sal, prop = keys()
    check("renormalize-names exits cleanly", real.exit_code == 0)
    check("salary key loses its suffix (got %r)" % sal[0], sal[0] == "james cook")
    check("colliding key left alone and reported",
          sal[1] == "foo bar jr" and "already exists" in real.output)
    check("prop_bets key loses its suffix (got %r)" % prop, prop == "brian thomas")

    conn = flaskapp._connect()
    conn.execute("DELETE FROM lineups WHERE submitter = 'suffixes'")
    conn.execute("DELETE FROM hist_player_stats WHERE week = ?", (wk,))
    conn.execute("DELETE FROM hist_dst_stats WHERE week = ?", (wk,))
    conn.execute("DELETE FROM hist_dfs_salaries WHERE week = 9")
    conn.execute("DELETE FROM prop_bets WHERE week = 9")
    conn.commit()
    conn.close()


def test_dnp_and_year_scope():
    section("DNP scoring and load-history --year")
    import json as _json
    wk = 5
    scored = [("QB", "Josh Allen", 20.0), ("RB", "James Cook", 10.0), ("RB", "Raheem Mostert", 9.0),
              ("WR", "Stefon Diggs", 8.0), ("WR", "Tyreek Hill", 7.0), ("WR", "Jaylen Waddle", 6.0),
              ("TE", "Dalton Kincaid", 5.0)]
    lineup = [{"slot": s, "name": n, "position": s, "salary": 5000} for s, n, _ in scored]
    lineup += [{"slot": "FLEX", "name": "Brock Bowers", "position": "TE", "salary": 5000},
               {"slot": "DST", "name": "Buffalo Bills", "position": "DST", "salary": 3000}]
    conn = flaskapp._connect()
    conn.execute("INSERT INTO lineups (week, year, submitter, lineup_json, total_salary) "
                 "VALUES (?,?,?,?,?)", (wk, YEAR, "dnptest", _json.dumps(lineup), 43000))
    for i, (_, name, pts) in enumerate(scored):
        conn.execute("INSERT INTO hist_player_stats (pfr_id, name, name_normalized, year, week, "
                     "team, dk_pts) VALUES (?,?,?,?,?,?,?)",
                     ("DNP%02d" % i, name, flaskapp.normalize_name(name), YEAR, wk, "buf", pts))
    conn.execute("INSERT INTO hist_dst_stats (year, week, team, dk_pts) VALUES (?,?,?,?)",
                 (YEAR, wk, "buf", 4.0))
    # Brock Bowers has no stats row; his team comes from that week's salaries.
    conn.execute("INSERT INTO hist_dfs_salaries (week, year, name, name_normalized, team, source) "
                 "VALUES (?,?,?,?,?,?)", (wk, YEAR, "Brock Bowers", "brock bowers", "lvr", "t"))
    conn.commit()
    conn.close()

    def week_total():
        with flaskapp.app.app_context():
            by_submitter, _ = flaskapp._score_lineups_for_year(YEAR)
        return by_submitter.get("dnptest", {}).get(wk)

    check("missing player stays pending before his team's result is in", week_total() is None)

    conn = flaskapp._connect()
    conn.execute("INSERT INTO hist_team_points (year, week, team, opponent, points_scored, "
                 "points_allowed) VALUES (?,?,?,?,?,?)", (YEAR, wk, "lvr", "kan", 10, 31))
    conn.commit()
    conn.close()
    got = week_total()
    check("DNP scores 0 once his team's result is in (got %s, want 69.0)" % got, got == 69.0)
    with flaskapp.app.app_context():
        bowers = [r for r in flaskapp._lineup_player_rows(YEAR, wk, "dnptest") if r["name"] == "Brock Bowers"]
    check("row is flagged dnp with 0.0", bool(bowers) and bowers[0]["dnp"] and bowers[0]["actual_pts"] == 0.0)
    html = client.get("/my-lineups?year=%d&week=%d&submitter=dnptest" % (YEAR, wk)).get_data(as_text=True)
    check("My Lineups shows the DNP badge", "dnp-badge" in html)
    check("My Lineups shows the week total 69.0", "69.0" in html)

    conn = flaskapp._connect()
    conn.execute("DELETE FROM lineups WHERE submitter = 'dnptest'")
    for table in ("hist_player_stats", "hist_dst_stats", "hist_dfs_salaries", "hist_team_points"):
        conn.execute("DELETE FROM %s WHERE year = ? AND week = ?" % table, (YEAR, wk))
    conn.commit()
    conn.close()

    # --year only loads that season's rows from a historical file.
    import pandas as pd
    data_dir = tempfile.mkdtemp(prefix="ffdfs_load_")
    pd.DataFrame([
        {"pfr_id": "YEAR25", "name": "Old Guy", "name_normalized": "old guy", "year": 2025, "week": 7,
         "team": "buf", "opponent": "mia", "position": "WR", "dk_pts": 1.0},
        {"pfr_id": "YEAR26", "name": "New Guy Jr.", "name_normalized": "new guy jr", "year": 2026, "week": 7,
         "team": "buf", "opponent": "mia", "position": "WR", "dk_pts": 2.0},
    ]).to_csv(os.path.join(data_dir, "pfr_player_stats_2014_2025.csv.gz"), index=False, compression="gzip")
    result = flaskapp.app.test_cli_runner().invoke(
        args=["load-history", "--stats-only", "--year", "2026", "--data-dir", data_dir])
    conn = flaskapp._connect()
    loaded = {r[0]: r[1] for r in conn.execute(
        "SELECT pfr_id, name_normalized FROM hist_player_stats WHERE pfr_id IN ('YEAR25', 'YEAR26')")}
    conn.execute("DELETE FROM hist_player_stats WHERE pfr_id IN ('YEAR25', 'YEAR26')")
    conn.commit()
    conn.close()
    check("load-history --year exits cleanly", result.exit_code == 0)
    check("--year 2026 loads the 2026 row", "YEAR26" in loaded)
    check("--year 2026 skips the 2025 row", "YEAR25" not in loaded)
    check("loader builds the key via _name_key (suffix dropped)", loaded.get("YEAR26") == "new guy")


def test_prop_void():
    section("prop for a player who didn't play is void")
    import csv as _csv
    import io as _io
    wk = 6
    conn = flaskapp._connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO prop_bets (year, week, player_name, player_name_normalized, stat_field, "
                "line) VALUES (?,?,?,?,?,?)", (YEAR, wk, "Lew Nichols", "lew nichols", "rush_yds", 20.5))
    bet_id = cur.lastrowid
    # Team resolves from that week's slate, where he's listed with a suffix.
    cur.execute("INSERT INTO players (week, year, name, position, team, opponent, salary) "
                "VALUES (?,?,?,?,?,?,?)", (wk, YEAR, "Lew Nichols III", "RB", "pit", "nwe", 4000))
    cur.execute("INSERT INTO prop_picks (year, week, submitter, prop_bet_id, pick) VALUES (?,?,?,?,?)",
                (YEAR, wk, "tester", bet_id, "under"))
    conn.commit()
    conn.close()

    def result():
        with flaskapp.app.app_context():
            return flaskapp._score_props_for_week(YEAR, wk).get(bet_id, {}).get("result")

    check("pending until his team's result is in", result() is None)
    conn = flaskapp._connect()
    conn.execute("INSERT INTO hist_team_points (year, week, team, opponent, points_scored, "
                 "points_allowed) VALUES (?,?,?,?,?,?)", (YEAR, wk, "pit", "nwe", 17, 20))
    conn.commit()
    conn.close()
    check("void once his team's result is in (got %r)" % result(), result() == "void")

    html = client.get("/my-props?year=%d&week=%d&submitter=tester" % (YEAR, wk)).get_data(as_text=True)
    check("My Props labels it void (DNP)", "void (DNP)" in html)
    check("My Props week is fully scored with 0 correct", "0 / 1" in html)
    rows = list(_csv.DictReader(_io.StringIO(
        client.get("/download/standings?year=%d" % YEAR).get_data(as_text=True))))
    row = next((r for r in rows if r["submitter"] == "tester" and r["week"] == str(wk)), {})
    check("standings CSV counts the void as neither scored nor pending",
          row.get("props_scored") == "0" and row.get("props_pending") == "0")

    conn = flaskapp._connect()
    conn.execute("DELETE FROM prop_picks WHERE week = ?", (wk,))
    conn.execute("DELETE FROM prop_bets WHERE week = ?", (wk,))
    conn.execute("DELETE FROM players WHERE week = ?", (wk,))
    conn.execute("DELETE FROM hist_team_points WHERE year = ? AND week = ?", (YEAR, wk))
    conn.commit()
    conn.close()


def test_scoring_breakdown():
    section("My Lineups scoring breakdown")
    import json as _json
    import scrape_pfr            # scrapers/ is on sys.path via app.py
    import combine_dst_scoring

    # The breakdown's rules must match the functions that produce stored points.
    check("points-allowed tiers match combine_dst_scoring for 0-50",
          all(flaskapp._points_allowed_bonus(pa) == combine_dst_scoring.points_allowed_bonus(pa)
              for pa in range(51)))
    sample = dict(pass_yds=305, pass_td=2, pass_int=1, rush_yds=104, rush_td=1, rec=6, rec_yds=101,
                  rec_td=1, fumbles_lost=1, kick_ret_td=1, punt_ret_td=1, fumbles_rec_td=1)
    check("offense lines sum to scrape_pfr.calculate_dk_points",
          flaskapp._offense_breakdown(sample, None)["total"] == scrape_pfr.calculate_dk_points(**sample))
    check("_fmt_pts formats +11.2, -1, +0",
          (flaskapp._fmt_pts(11.2), flaskapp._fmt_pts(-1), flaskapp._fmt_pts(0)) == ("+11.2", "-1", "+0"))

    wk = 7
    lineup = [{"slot": "QB", "name": "Joe Burrow", "position": "QB", "salary": 6000},
              {"slot": "WR", "name": "Ja'Marr Chase", "position": "WR", "salary": 8000},
              {"slot": "RB", "name": "Chase Brown", "position": "RB", "salary": 6500},
              {"slot": "DST", "name": "Cincinnati Bengals", "position": "DST", "salary": 3000}]
    # (pfr_id, name, stats, stored dk_pts, PFR-reported dk_pts)
    stats = [("BD01", "Joe Burrow", dict(pass_yds=320, pass_td=2, pass_int=1, rush_yds=12), 24.0, None),
             ("BD02", "Ja'Marr Chase", dict(rec=7, rec_yds=112, rec_td=1, fumbles_lost=1), 26.2, None),
             # PFR's official total is 2 higher than our stats explain (a 2-pt conversion).
             ("BD03", "Chase Brown", dict(rush_yds=60, rush_td=1, rec=2, rec_yds=10), 15.0, 17.0)]
    conn = flaskapp._connect()
    conn.execute("INSERT INTO lineups (week, year, submitter, lineup_json, total_salary) VALUES (?,?,?,?,?)",
                 (wk, YEAR, "bdtest", _json.dumps(lineup), 23500))
    for pfr_id, name, s, dk, reported in stats:
        cols = ["pfr_id", "name", "name_normalized", "year", "week", "team", "dk_pts", "dk_pts_pfr_reported"] + list(s)
        conn.execute("INSERT INTO hist_player_stats (%s) VALUES (%s)" % (", ".join(cols), ",".join("?" * len(cols))),
                     [pfr_id, name, flaskapp.normalize_name(name), YEAR, wk, "cin", dk, reported] + list(s.values()))
    conn.execute("INSERT INTO hist_dst_stats (year, week, team, points_allowed, dk_pts, sack, interception, "
                 "fumble_rec, def_td, safety, special_teams_td) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (YEAR, wk, "cin", 17, 6.0, 3, 1, 0, 0, 0, 0))
    conn.commit()
    conn.close()

    html = client.get("/my-lineups?year=%d&week=%d&submitter=bdtest" % (YEAR, wk)).get_data(as_text=True)
    check("table opts out of sorting", "data-no-sort" in html)
    check("all 4 players are expandable (got %d)" % html.count('role="button"'), html.count('role="button"') == 4)
    check("QB lines: 320 x 0.04 = +12.8 and the 300+ bonus",
          "320 × 0.04" in html and "+12.8" in html and "300+ passing yards bonus" in html)
    check("WR lines: 112 x 0.1 = +11.2, 100+ bonus, fumble lost -1",
          "112 × 0.1" in html and "+11.2" in html and "100+ receiving yards bonus" in html and "Fumble lost" in html)
    check("Other line makes the RB add up to his official 17.00",
          "e.g. 2-pt conversions" in html and "17.00" in html)
    check("DST lines: sacks, interception, 17 allowed", "Sack" in html and "17 allowed" in html)
    check("totals shown: 24.00, 26.20, 6.00", all(t in html for t in ("24.00", "26.20", "6.00")))
    check("no Other line when stats explain the total", html.count("breakdown-other") == 1)

    conn = flaskapp._connect()
    conn.execute("DELETE FROM lineups WHERE submitter = 'bdtest'")
    conn.execute("DELETE FROM hist_player_stats WHERE year = ? AND week = ?", (YEAR, wk))
    conn.execute("DELETE FROM hist_dst_stats WHERE year = ? AND week = ?", (YEAR, wk))
    conn.commit()
    conn.close()


def test_current_week_default():
    section("My Lineups / My Props default to the current NFL week")
    saved_week_fn = flaskapp._get_current_nfl_week
    # Week 3: nobody has submitted a lineup or a pick for it.
    flaskapp._get_current_nfl_week = lambda: (YEAR, 3)

    # my_props needs at least one pick row to have a submitter list at all.
    conn = flaskapp._connect()
    had_picks = conn.execute("SELECT COUNT(*) FROM prop_picks").fetchone()[0]
    if not had_picks:
        conn.execute("INSERT INTO prop_picks (year, week, submitter, prop_bet_id, pick) "
                     "VALUES (?,?,?,?,?)", (YEAR, WEEK, "tester", prop_ids["Josh Allen"], "over"))
        conn.commit()
    conn.close()

    try:
        html = client.get("/my-lineups").get_data(as_text=True)
        check("My Lineups defaults to the current week", "Week 3, %d lineup" % YEAR in html)
        check("My Lineups offers the current week in the dropdown", '<option value="3"' in html)
        check("My Lineups shows its empty state for a week with no lineup",
              "No lineup found" in html)

        html = client.get("/my-props").get_data(as_text=True)
        check("My Props defaults to the current week", "Week 3, %d prop picks" % YEAR in html)
        check("My Props offers the current week in the dropdown", '<option value="3"' in html)
        check("My Props shows its empty state for a week with no picks",
              "No prop picks found" in html)

        # An explicitly requested past week still wins over the default.
        html = client.get("/my-lineups?year=%d&week=1&submitter=tester" % YEAR).get_data(as_text=True)
        check("an explicitly requested past week still renders that lineup",
              "Week 1, %d lineup" % YEAR in html and "No lineup found" not in html)
    finally:
        flaskapp._get_current_nfl_week = saved_week_fn
        if not had_picks:
            conn = flaskapp._connect()
            conn.execute("DELETE FROM prop_picks")
            conn.commit()
            conn.close()


def test_schedule_current_week():
    section("Schedule highlights and scrolls to the current week")
    # The fixture's only scheduled week is week 1, so that's "current".
    html = client.get("/schedule").get_data(as_text=True)
    check("current week's section is marked", 'class="schedule-week current-week"' in html)
    check("current week's jump link is marked", "week-jump-link current" in html)
    check("current week carries a This week badge", "This week</span>" in html)
    check("page scrolls to the current week on load",
          'getElementById("week-1")' in html and "scrollIntoView" in html)
    check("an explicit #week link still wins", "window.location.hash" in html)

    saved = flaskapp._get_current_nfl_week
    flaskapp._get_current_nfl_week = lambda: (YEAR + 1, 5)   # viewing a season that isn't current
    try:
        html = client.get("/schedule?year=%d" % YEAR).get_data(as_text=True)
        check("no highlight when the shown season isn't the current one",
              "current-week" not in html and "scrollIntoView" not in html)
    finally:
        flaskapp._get_current_nfl_week = saved


def _usage_table(html):
    """{player name: {header: cell text}} from the Usage page's table."""
    import re
    import html as _html

    def text(s):
        return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    if 'id="history-table"' not in html:
        return {}
    table = html.split('id="history-table"', 1)[1].split("</table>", 1)[0]
    heads = [text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
    out = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.split("<tbody>", 1)[1], re.S):
        row = dict(zip(heads, [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]))
        out[row.get("Player")] = row
    return out


def test_usage():
    section("usage: nflverse loader, page (season and week), CSV")
    import csv as _csv
    import io as _io
    import pandas as pd

    def urow(year, week, name, key, team, pos, snaps, pct, tgt, air, air_share, wopr,
             carries=0, rz_t=0, rz_c=0, i10_c=0, i5_c=0, third=0, rec=0):
        return {"year": year, "week": week, "pfr_id": "X", "gsis_id": "00-X", "name": name,
                "name_normalized": key, "team": team, "position": pos,
                "offense_snaps": snaps, "offense_pct": pct, "targets": tgt, "target_share": 0.0,
                "air_yards": air, "air_yards_share": air_share,
                "adot": (air / tgt) if tgt else None, "wopr": wopr, "carries": carries,
                "receptions": rec,
                "rz_targets": rz_t, "rz_carries": rz_c, "i10_targets": 0, "i10_carries": i10_c,
                "i5_targets": 0, "i5_carries": i5_c, "third_down_targets": third}

    data_dir = tempfile.mkdtemp(prefix="ffdfs_usage_")
    pd.DataFrame([
        # Suffixed name and a legacy team code, both fixed at ingestion.
        urow(2026, 11, "Puka Nacua Jr.", "puka nacua jr", "stl", "WR", 60, 95.0, 10, 100.0, 60.0, 0.9,
             rz_t=2, third=3, rec=7),
        urow(2026, 12, "Puka Nacua Jr.", "puka nacua jr", "lar", "WR", 55, 85.0, 5, 50.0, 55.0, 0.8,
             rz_t=1, third=1, rec=4),
        # Week 11: nflverse missed his snaps, so PFR's 71.9 is used.
        urow(2026, 11, "Kyren Williams", "kyren williams", "lar", "RB", 0, None, 4, 5.0, 3.0, 0.3,
             carries=20, rz_c=5, i10_c=3, i5_c=2, rec=3),
        urow(2026, 12, "Kyren Williams", "kyren williams", "lar", "RB", 50, 68.1, 2, 7.0, 4.0, 0.2,
             carries=15, rz_c=3, i10_c=1, rec=2),
        urow(2025, 11, "Old Usage", "old usage", "lar", "WR", 10, 20.0, 1, 1.0, 1.0, 0.1),
    ]).to_csv(os.path.join(data_dir, "nflverse_usage_2026.csv.gz"), index=False, compression="gzip")

    result = flaskapp.app.test_cli_runner().invoke(
        args=["load-history", "--usage-only", "--year", "2026", "--data-dir", data_dir])
    check("load-history --usage-only exits cleanly", result.exit_code == 0)
    conn = flaskapp._connect()
    loaded = conn.execute("SELECT year, week, name_normalized, team, offense_pct "
                          "FROM hist_player_usage ORDER BY name_normalized, week").fetchall()
    load_row = conn.execute("SELECT row_count FROM data_loads WHERE source = 'usage'").fetchone()
    conn.close()
    check("4 rows loaded, 2025 row skipped by --year (got %d)" % len(loaded),
          len(loaded) == 4 and all(r[0] == 2026 for r in loaded))
    check("suffix dropped from the name key",
          {r[2] for r in loaded} == {"puka nacua", "kyren williams"})
    check("legacy team code normalized (stl -> lar)", {r[3] for r in loaded} == {"lar"})
    check("empty offense_pct stored as NULL", loaded[0][4] is None)
    check("data_loads has a usage row with 4 rows", load_row is not None and load_row[0] == 4)

    # Matching PFR stats rows. Tutu Atwell has no usage row.
    stats = [("USG1", "Puka Nacua", "WR", 11, 10, 7, 0, 90.0),
             ("USG1", "Puka Nacua", "WR", 12, 5, 4, 0, 80.0),
             ("USG2", "Kyren Williams", "RB", 11, 4, 3, 20, 71.9),
             ("USG2", "Kyren Williams", "RB", 12, 2, 2, 15, 60.0),
             ("USG3", "Tutu Atwell", "WR", 11, 3, 2, 0, None)]
    conn = flaskapp._connect()
    for pfr_id, name, pos, wk, tgt, rec, rush, snap in stats:
        conn.execute("INSERT INTO hist_player_stats (pfr_id, name, name_normalized, year, week, team, "
                     "position, rec_tgt, rec, rush_att, snap_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pfr_id, name, flaskapp.normalize_name(name), YEAR, wk, "lar", pos, tgt, rec, rush, snap))
    conn.commit()
    conn.close()

    html = client.get("/usage?year=%d" % YEAR).get_data(as_text=True)
    t = _usage_table(html)
    puka, kyren, tutu = t.get("Puka Nacua", {}), t.get("Kyren Williams", {}), t.get("Tutu Atwell", {})
    check("season subtitle says Season", "%d Season:" % YEAR in html)
    check("week selector lists weeks 11 and 12",
          '<option value="11"' in html and '<option value="12"' in html)
    check("nflverse credit line shown", "Snap, air yards and red zone data: nflverse" in html)
    check("old 'no snap counts' note is gone", "snap counts yet" not in html)
    check("season Snap %% averages nflverse weeks, 90.0%% (got %r)" % puka.get("Snap %"),
          puka.get("Snap %") == "90.0%")
    check("season Snap %% falls back to PFR for a week nflverse missed, 70.0%% (got %r)"
          % kyren.get("Snap %"), kyren.get("Snap %") == "70.0%")
    check("season aDOT 150/15 = 10.0 (got %r)" % puka.get("aDOT"), puka.get("aDOT") == "10.0")
    check("season Air Yds %% 150/162 = 92.6%% (got %r)" % puka.get("Air %"),
          puka.get("Air %") == "92.6%")
    check("season WOPR 1.5*15/21 + 0.7*150/162 = 1.72 (got %r)" % puka.get("WOPR"),
          puka.get("WOPR") == "1.72")
    check("season RZ Tgt sums to 3 (got %r)" % puka.get("RZ Tgt"), puka.get("RZ Tgt") == "3")
    check("season 3D Tgt sums to 4", puka.get("3D Tgt") == "4")
    check("season RZ Car / I10 / I5 = 8 / 4 / 2",
          (kyren.get("RZ Car"), kyren.get("I10 Car"), kyren.get("I5 Car")) == ("8", "4", "2"))
    check("usage table uses the compact layout", 'id="history-table" class="compact-table"' in html)
    check("All Data download offered", "All Data" in html)
    # Shares use nflverse counts for player and team when the team has usage
    # rows: team targets 10+5+4+2 = 21 (PFR's 24 includes Tutu Atwell's 3).
    check("season G is 2 (PFR) and Tgt %% uses nflverse, 15/21 = 71.4%% (got %r)" % puka.get("Tgt %"),
          puka.get("G") == "2" and puka.get("Tgt %") == "71.4%")
    check("season Touch %% uses nflverse carries + receptions, 11/51 = 21.6%% (got %r)"
          % puka.get("Touch %"), puka.get("Touch %") == "21.6%")
    check("player with no usage row: PFR targets over the nflverse team total, 3/21 = 14.3%% (got %r)"
          % tutu.get("Tgt %"), tutu.get("Tgt %") == "14.3%")
    check("player with no usage row shows '-' (got %r)" % tutu,
          bool(tutu) and all(tutu.get(c) == "-" for c in
                             ("Snap %", "aDOT", "Air %", "WOPR", "RZ Tgt", "3D Tgt")))
    check("default sort is target share, highest first",
          list(t).index("Puka Nacua") < list(t).index("Kyren Williams"))

    html = client.get("/usage?year=%d&week=11&position=ALL" % YEAR).get_data(as_text=True)
    t = _usage_table(html)
    puka, kyren = t.get("Puka Nacua", {}), t.get("Kyren Williams", {})
    check("week subtitle says Week 11", "Week 11, %d:" % YEAR in html)
    check("week view: Snap %% 95.0%% (got %r)" % puka.get("Snap %"), puka.get("Snap %") == "95.0%")
    check("week view: PFR snap fallback 71.9%% (got %r)" % kyren.get("Snap %"), kyren.get("Snap %") == "71.9%")
    check("week view: aDOT 10.0, RZ Tgt 2, Air Yds %% 60.0%%, WOPR 0.9",
          (puka.get("aDOT"), puka.get("RZ Tgt"), puka.get("Air %"), puka.get("WOPR"))
          == ("10.0", "2", "60.0%", "0.9"))
    check("week view: G 1 and Tgt %% from nflverse 10/14 = 71.4%% (got %r)" % puka.get("Tgt %"),
          puka.get("G") == "1" and puka.get("Tgt %") == "71.4%")
    check("week view: Touch %% 7/30 = 23.3%% (got %r)" % puka.get("Touch %"),
          puka.get("Touch %") == "23.3%")
    check("week view: position links keep the week", "week=11&position=RB" in html)
    check("week view: This Week and This Season downloads",
          "This Week" in html and "This Season" in html)

    body = client.get("/download/usage?year=%d" % YEAR).get_data(as_text=True)
    header = body.splitlines()[0].split(",")
    rows = {r["name_normalized"]: r for r in _csv.DictReader(_io.StringIO(body))}
    check("season CSV leads with year, team, name, name_normalized and has no week",
          header[:4] == ["year", "team", "name", "name_normalized"] and "week" not in header)
    p = rows.get("puka nacua", {})
    check("season CSV carries the new columns (snap_pct 90.0, adot 10.0, rz_targets 3)",
          (p.get("snap_pct"), p.get("adot"), p.get("rz_targets")) == ("90.0", "10.0", "3"))
    check("season CSV blank for a player with no usage row",
          rows.get("tutu atwell", {}).get("adot") == "")

    body = client.get("/download/usage").get_data(as_text=True)
    header = body.splitlines()[0].split(",")
    all_rows = list(_csv.DictReader(_io.StringIO(body)))
    check("All Data CSV leads with year, team, name, name_normalized and has no week",
          header[:4] == ["year", "team", "name", "name_normalized"] and "week" not in header)
    check("All Data CSV includes this season's usage values",
          any(r["name_normalized"] == "puka nacua" and r["year"] == str(YEAR) and r["adot"] == "10.0"
              for r in all_rows))

    body = client.get("/download/usage?year=%d&week=11" % YEAR).get_data(as_text=True)
    header = body.splitlines()[0].split(",")
    rows = {r["name_normalized"]: r for r in _csv.DictReader(_io.StringIO(body))}
    p = rows.get("puka nacua", {})
    check("week CSV leads with year, week, team, name, name_normalized",
          header[:5] == ["year", "week", "team", "name", "name_normalized"])
    check("week CSV is week 11 values (week 11, snap_pct 95.0, rz_targets 2)",
          (p.get("week"), p.get("snap_pct"), p.get("rz_targets")) == ("11", "95.0", "2"))

    conn = flaskapp._connect()
    conn.execute("DELETE FROM hist_player_usage")
    conn.execute("DELETE FROM hist_player_stats WHERE pfr_id IN ('USG1', 'USG2', 'USG3')")
    conn.execute("DELETE FROM data_loads WHERE source = 'usage'")
    conn.commit()
    conn.close()


# (data_type, querystring, expected leading key columns)
DOWNLOADS = [
    ("slate",                   "?year=2026&week=1", ["year", "week", "team", "name", "name_normalized"]),
    ("history",                 "?year=2026&week=1", None),
    ("history",                 "",                  None),
    ("weather",                 "?year=2026&week=1", ["year", "week"]),
    ("weather",                 "",                  ["year", "week"]),
    ("weather-by-team",         "?year=2026&week=1", ["year", "week", "team"]),
    ("weather-by-team",         "",                  ["year", "week", "team"]),
    ("gameinfo",                "?year=2026&week=1", ["year", "week"]),
    ("gameinfo-by-team",        "?year=2026&week=1", ["year", "week", "team"]),
    ("gameinfo-by-team",        "",                  ["year", "week", "team"]),
    ("player",                  "?pfr_id=nobody",    None),
    ("schedule",                "?year=2026",        None),
    ("fantasy-points-against",  "?year=2026",        None),
    ("team-points",             "?year=2026&week=1", None),
    ("best-matchups",           "?year=2026&week=1", None),
    ("depth-charts",            "",                  ["year", "week", "team", "name_normalized"]),
    ("implied-points",          "?year=2026&week=1", ["year", "week", "team", "name", "name_normalized"]),
    ("props",                   "",                  ["year", "week", "name_normalized"]),
    ("usage",                   "?year=2026",        ["year", "team", "name", "name_normalized"]),
    ("usage",                   "",                  ["year", "team", "name", "name_normalized"]),
    ("usage",                   "?year=2026&week=1", ["year", "week", "team", "name", "name_normalized"]),
    ("implied-team-points",     "",                  None),
    ("game-overview",           "",                  None),
    ("standings",               "?year=2026",        ["year", "week", "submitter"]),
    ("my-lineups",              "?year=2026",        ["year", "week", "submitter"]),
    ("my-lineups",              "?year=2026&week=1&submitter=tester", ["year", "week", "submitter"]),
    ("my-props",                "?year=2026",        ["year", "week", "submitter"]),
]


def test_downloads():
    section("every download type responds as CSV")
    for data_type, qs, expected_keys in DOWNLOADS:
        r = client.get("/download/%s%s" % (data_type, qs))
        label = "%s%s" % (data_type, qs)
        if r.status_code != 200:
            check("%-52s -> %d" % (label, r.status_code), False)
            continue
        is_csv = "text/csv" in r.headers.get("Content-Type", "")
        has_attachment = "attachment" in r.headers.get("Content-Disposition", "")
        body = r.get_data(as_text=True)
        header = body.splitlines()[0].split(",") if body.strip() else []

        ok = is_csv and has_attachment
        if expected_keys:
            # Column ORDER matters here: _prepend_keys exists so every
            # export opens with the same join keys in the same place.
            ok = ok and header[:len(expected_keys)] == expected_keys
        check("%-52s (%d cols)" % (label, len(header)), ok)

    r = client.get("/download/not-a-real-type")
    check("unknown data type returns 404", r.status_code == 404)


def _read_csv(path):
    import csv as _csv
    body = client.get(path).get_data(as_text=True)
    return list(_csv.DictReader(body.splitlines())) if body.strip() else []


def test_merge_friendly_reshape():
    section("merge-friendly (per-team) exports, both scopes")
    for dt in ("weather-by-team", "gameinfo-by-team"):
        wk_rows = _read_csv("/download/%s?year=2026&week=1" % dt)
        all_rows = _read_csv("/download/%s" % dt)

        check("%-18s week scope: 2 rows per game" % dt, len(wk_rows) == 2)
        # The whole point of the "All Data" button: it must reach weeks
        # other than the one currently on screen.
        check("%-18s all scope covers both weeks" % dt,
              {r["week"] for r in all_rows} == {"1", "2"})
        check("%-18s all scope is broader than week scope" % dt,
              len(all_rows) > len(wk_rows))
        check("%-18s one plain `team` column, no home/away split" % dt,
              bool(wk_rows) and "team" in wk_rows[0]
              and not {"home_team", "away_team", "team_home", "team_away"}
              & set(wk_rows[0]))
        check("%-18s home and away sides both present" % dt,
              {r["home_away"] for r in wk_rows} == {"h", "a"})
        # team/opponent must actually be each other's mirror.
        if len(wk_rows) == 2:
            a, h = sorted(wk_rows, key=lambda r: r["home_away"])
            check("%-18s team/opponent mirror correctly" % dt,
                  a["team"] == h["opponent"] and a["opponent"] == h["team"])

    # Scores are per-side, so they must flip with the row rather than
    # staying as away_score/home_score (they were dropped entirely
    # before this change).
    rows = _read_csv("/download/weather-by-team?year=2026&week=1")
    away = [r for r in rows if r["home_away"] == "a"][0]
    home = [r for r in rows if r["home_away"] == "h"][0]
    check("weather-by-team carries a per-side score",
          away.get("score") == "17" and home.get("score") == "24")
    check("weather-by-team opponent_score is the mirror",
          away.get("opponent_score") == "24" and home.get("opponent_score") == "17")


def test_every_tab_has_a_download():
    section("every data tab offers a download")
    import re
    nav = re.findall(r"url_for\('(\w+)'\)", open("templates/slate.html", encoding="utf-8").read())
    tabs = [t for t in dict.fromkeys(nav) if t not in ("static", "login", "logout", "slate")]
    tab_template = {
        "standings": "standings.html", "props": "props.html", "my_props": "my_props.html",
        "my_lineups": "my_lineups.html", "history": "history.html",
        "game_overview": "game_overview.html", "weather": "weather.html",
        "gameinfo": "gameinfo.html", "schedule": "schedule.html",
        "fantasy_points_against": "fantasy_points_against.html", "usage": "usage.html",
        "team_points": "team_points.html", "best_matchups": "best_matchups.html",
        "depth_charts": "depth_charts.html", "implied_points": "implied_points.html",
        "implied_team_points": "implied_team_points.html",
    }
    for tab in tabs:
        tpl = tab_template.get(tab)
        if not tpl:
            check("nav tab '%s' has a known template" % tab, False)
            continue
        src = open(os.path.join("templates", tpl), encoding="utf-8").read()
        check("%-24s uses dl.bar()" % tab, "dl.bar(" in src)
    check("slate.html uses dl.bar()",
          "dl.bar(" in open("templates/slate.html", encoding="utf-8").read())


def test_routes_smoke():
    section("every GET route returns 200")
    for path in ["/", "/history", "/team-points", "/usage", "/props", "/my-props",
                 "/my-lineups", "/standings", "/schedule", "/weather", "/gameinfo",
                 "/depth-charts", "/implied-points", "/implied-team-points",
                 "/best-matchups", "/game-overview", "/fantasy-points-against"]:
        check("GET %-26s" % path, client.get(path).status_code == 200)


TAB_PATHS = ["/", "/history", "/team-points", "/usage", "/props", "/my-props",
             "/my-lineups", "/standings", "/schedule", "/weather", "/gameinfo",
             "/depth-charts", "/implied-points", "/implied-team-points",
             "/best-matchups", "/game-overview", "/fantasy-points-against"]


def test_data_as_of_empty():
    """Runs before any loader has written to data_loads."""
    section("data as of: nothing loaded yet")
    conn = flaskapp._connect()
    n = conn.execute("SELECT COUNT(*) FROM data_loads").fetchone()[0]
    conn.close()
    check("data_loads starts empty", n == 0)
    for path in TAB_PATHS:
        r = client.get(path)
        # The help panel on every page says "Data as of" too, so look for
        # the line's own class, not the words.
        check("GET %-26s 200 with no data-as-of line" % path,
              r.status_code == 200 and 'class="data-as-of"' not in r.get_data(as_text=True))
    with flaskapp.app.test_request_context():
        check("_data_as_of skips a source never loaded", flaskapp._data_as_of("game_odds", "nope") == [])
    for endpoint in flaskapp.TAB_DATA_SOURCES:
        src = open(os.path.join("templates", endpoint + ".html"), encoding="utf-8").read()
        check("%-24s includes _data_as_of.html" % endpoint, "_data_as_of.html" in src)
    # psycopg2 hands back a naive datetime, SQLite a string; same result.
    fmt = flaskapp._format_eastern
    check("datetime from Postgres formats in ET",
          fmt(flaskapp._as_utc_datetime(datetime.datetime(2026, 9, 15, 17, 2))) == "Tue Sep 15, 1:02 PM ET")
    check("string from SQLite formats in ET",
          fmt(flaskapp._as_utc_datetime("2026-09-15 17:02:00")) == "Tue Sep 15, 1:02 PM ET")


def test_game_odds_week():
    """Before test_timestamp_format, which rewrites game_schedule."""
    section("data_loads rows and the game odds week from kickoff")
    import csv as _csv
    import io as _io
    import re
    import pandas as pd
    runner = flaskapp.app.test_cli_runner()
    data_dir = tempfile.mkdtemp(prefix="ffdfs_odds_")

    # The fixture's kan/den game is Week 1. Kickoff 5 minutes off the
    # schedule, in the scraper's ISO-Z form, so the match isn't exact.
    kick = (datetime.datetime.strptime(PAST, '%Y-%m-%d %H:%M:%S')
            + datetime.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path = os.path.join(data_dir, "scoresandodds_game_odds.csv.gz")
    pd.DataFrame([
        {"event_id": "e1", "kickoff": kick, "team": "kan", "opponent": "den", "spread": -3.0,
         "spread_odds": "-110", "over_under": "o42.5", "favorite": "kan"},
        {"event_id": "e1", "kickoff": kick, "team": "den", "opponent": "kan", "spread": 3.0,
         "spread_odds": "-110", "over_under": "o42.5", "favorite": "kan"},
    ]).to_csv(path, index=False, compression="gzip")
    scraped = datetime.datetime(2026, 9, 15, 17, 2, tzinfo=datetime.timezone.utc).timestamp()
    os.utime(path, (scraped, scraped))

    result = runner.invoke(args=["load-history", "--game-odds-only", "--data-dir", data_dir])
    check("load-history --game-odds-only exits cleanly", result.exit_code == 0)
    conn = flaskapp._connect()
    row = conn.execute("SELECT file_modified_at, loaded_at, row_count FROM data_loads "
                       "WHERE source = 'game_odds'").fetchone()
    kickoffs = [r[0] for r in conn.execute("SELECT kickoff FROM game_odds")]
    conn.close()
    check("data_loads has a game_odds row", row is not None)
    check("file_modified_at is the file's mtime in UTC (got %r)" % (row and row[0],),
          bool(row) and row[0] == "2026-09-15 17:02:00")
    check("loaded_at uses a space, not a 'T' (got %r)" % (row and row[1],),
          bool(row) and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", row[1] or "") is not None)
    check("row_count is 2", bool(row) and row[2] == 2)
    check("kickoff stored as '%%Y-%%m-%%d %%H:%%M:%%S' (got %r)" % kickoffs,
          len(kickoffs) == 2 and all(k and "T" not in k and not k.endswith("Z") for k in kickoffs))

    with flaskapp.app.app_context():
        check("odds week derived from kickoff is Week 1",
              flaskapp._get_game_odds_week() == (YEAR, 1))
        check("odds are used for their own week",
              len(flaskapp._game_odds_rows_for_week(YEAR, 1, "team")) == 2)
        check("odds are not used for another week",
              flaskapp._game_odds_rows_for_week(YEAR, 2, "team") == [])

    html = client.get("/implied-team-points").get_data(as_text=True)
    check("Implied Team Points subtitle says Week 1, %d" % YEAR,
          "Week 1, %d: expected points" % YEAR in html)
    check("Implied Team Points shows the data-as-of line",
          'class="data-as-of"' in html and "Odds Tue Sep 15, 1:02 PM ET" in html)
    check("no stale-week note when the odds are for the current week", "odds-week-note" not in html)

    saved = flaskapp._get_current_nfl_week
    flaskapp._get_current_nfl_week = lambda: (YEAR, 2)
    try:
        html = client.get("/implied-team-points").get_data(as_text=True)
        check("subtitle keeps the odds' own week, not the current week",
              "Week 1, %d: expected points" % YEAR in html and "Week 2, %d: expected" % YEAR not in html)
        import html as _html   # the apostrophe in "aren't" is autoescaped
        check("stale-week note shown",
              "These are Week 1 lines. Week 2 lines aren't loaded yet." in _html.unescape(html))
        rows = list(_csv.DictReader(_io.StringIO(
            client.get("/download/implied-team-points").get_data(as_text=True))))
        check("implied-team-points CSV stamps the odds' week (1)",
              bool(rows) and {r["week"] for r in rows} == {"1"})
        html = client.get("/game-overview").get_data(as_text=True)
        check("Game Overview says its odds are blank for Week 2",
              "Odds are blank: the loaded lines are for Week 1, not Week 2." in html)
    finally:
        flaskapp._get_current_nfl_week = saved

    # Rows with no kickoff can't be placed in a week: omit it, don't guess.
    conn = flaskapp._connect()
    conn.execute("UPDATE game_odds SET kickoff = NULL")
    conn.commit()
    conn.close()
    html = client.get("/implied-team-points").get_data(as_text=True)
    check("unknown odds week: subtitle has no week",
          "Expected points for each team" in html and "Week 1, %d: expected" % YEAR not in html)

    # add-props records its CSV too.
    props_csv = os.path.join(data_dir, "props.csv")
    with open(props_csv, "w", encoding="utf-8") as f:
        f.write("player_name,stat_field,line\nJosh Allen,pass_yds,250.5\n")
    result = runner.invoke(args=["add-props", props_csv, "--year", str(YEAR), "--week", "8"])
    conn = flaskapp._connect()
    prop_row = conn.execute("SELECT row_count FROM data_loads WHERE source = 'prop_bets'").fetchone()
    conn.execute("DELETE FROM prop_bets WHERE week = 8")
    conn.execute("DELETE FROM game_odds")
    conn.execute("DELETE FROM data_loads WHERE source IN ('game_odds', 'prop_bets')")
    conn.commit()
    conn.close()
    check("add-props exits cleanly and records prop_bets",
          result.exit_code == 0 and prop_row is not None and prop_row[0] == 1)


def test_week_rollover_boundary():
    """The NFL week rolls over Tuesday 4 AM ET, not N hours after the last
    kickoff (owner's call 2026-09-22: a flat 24h buffer left the finished
    week 'current' until 8:15 PM Tuesday)."""
    section("week rollover boundary (Tuesday 4 AM ET)")
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo("America/New_York")

    def cutoff_at(y, m, d, hh, mm=0):
        when = datetime.datetime(y, m, d, hh, mm, tzinfo=eastern)
        return flaskapp._week_rollover_cutoff(when.astimezone(datetime.timezone.utc))

    # Monday night, right after the last game of week N: still week N, so the
    # cutoff must still be LAST Tuesday.
    check("Monday 11 PM ET -> previous Tuesday's boundary",
          cutoff_at(2026, 9, 21, 23) == "2026-09-15 08:00:00")
    # Before 4 AM Tuesday the week hasn't rolled yet.
    check("Tuesday 3 AM ET -> still the previous boundary",
          cutoff_at(2026, 9, 22, 3) == "2026-09-15 08:00:00")
    check("Tuesday 5 AM ET -> that Tuesday's boundary",
          cutoff_at(2026, 9, 22, 5) == "2026-09-22 08:00:00")
    check("Sunday of the new week -> still that Tuesday's boundary",
          cutoff_at(2026, 9, 27, 13) == "2026-09-22 08:00:00")
    # Standard time: 4 AM EST is 09:00 UTC, not 08:00 — the reason this uses
    # zoneinfo rather than a fixed offset.
    check("after the DST change, 4 AM ET is 09:00 UTC",
          cutoff_at(2026, 11, 10, 9) == "2026-11-10 09:00:00")


def test_timestamp_format():
    """Runs LAST -- it rewrites game_schedule."""
    section("timestamp format vs SQLite lexicographic comparison")
    probe = sqlite3.connect(":memory:")
    cutoff = datetime.datetime.strptime(flaskapp._week_rollover_cutoff(), '%Y-%m-%d %H:%M:%S')
    same_day_kickoff = cutoff.strftime('%Y-%m-%d') + " 23:59:59"

    old_ok = probe.execute("SELECT ? >= ?",
                           (same_day_kickoff, cutoff.isoformat())).fetchone()[0]
    new_ok = probe.execute("SELECT ? >= ?",
                           (same_day_kickoff,
                            cutoff.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()[0]
    check("the old .isoformat() form compares WRONG (bug reproduced)", old_ok == 0)
    check("the strftime form compares correctly", new_ok == 1)

    # Two weeks, both AFTER the cutoff: the same-date one (week 7) and a
    # clearly-later one (week 8). If the comparison drops the same-date week,
    # the answer becomes week 8 — without week 8 the function would fall back
    # to "latest week in the table" and pass for the wrong reason.
    conn = flaskapp._connect()
    conn.execute("DELETE FROM game_schedule")
    conn.execute("INSERT INTO game_schedule (year, week, team, opponent, home_away, kickoff) "
                 "VALUES (?,?,?,?,?,?)", (YEAR, 7, "kan", "den", "h", same_day_kickoff))
    conn.execute("INSERT INTO game_schedule (year, week, team, opponent, home_away, kickoff) "
                 "VALUES (?,?,?,?,?,?)", (YEAR, 8, "buf", "mia", "h",
                                          (cutoff + datetime.timedelta(days=9)).strftime('%Y-%m-%d %H:%M:%S')))
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
    test_downloads()
    test_merge_friendly_reshape()
    test_every_tab_has_a_download()
    test_routes_smoke()
    test_data_as_of_empty()          # before any test runs a loader
    test_non_counting_week()        # before standings: needs week 2 fully scored
    test_standings_scoring()         # mutates hist_player_stats at the end
    test_name_suffixes()             # after standings: adds (then removes) a week 3
    test_dnp_and_year_scope()        # adds (then removes) a week 5 and two 2025/2026 rows
    test_prop_void()                 # adds (then removes) a week 6 prop and pick
    test_scoring_breakdown()         # adds (then removes) a week 7 lineup
    test_current_week_default()      # patches _get_current_nfl_week to an empty week
    test_schedule_current_week()
    test_usage()                     # after data_as_of_empty; adds (then removes) weeks 11-12
    test_game_odds_week()            # needs the fixture schedule; adds (then removes) odds
    test_week_rollover_boundary()
    test_timestamp_format()          # must stay last; rewrites game_schedule

    print()
    if FAILURES:
        print("%d FAILED:" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
    else:
        print("ALL CHECKS PASSED")
    raise SystemExit(1 if FAILURES else 0)
