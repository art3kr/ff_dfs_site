# CLAUDE.md

Context for working on this codebase. This complements `README.md`
(which covers day-to-day operational commands — scraping, loading,
the weekly workflow) rather than duplicating it — read that too for
anything workflow-related.

**Keep this file current — that's part of the job, not optional.**
Whenever a session changes something this file describes (a new page,
a fixed bug, a new gotcha) or moves the season forward (a week scored,
a week prepped), update the relevant section before finishing,
especially **Project status** near the bottom. Date every status
entry with the real calendar date. What belongs where: *how* to run
things goes in `README.md`; *where things stand* and *what to watch
out for* goes here.

## What this is

A season-long NFL DFS (daily fantasy sports) challenge site for a
small private group (~10-20 participants). Flask + Python, dual
SQLite (local dev) / PostgreSQL (production, hosted on Render), plain
HTML/CSS/JS (no frontend framework, no build step). GitHub:
`https://github.com/art3kr/ff_dfs_site` (private).

Two parallel challenges live on the site: a weekly salary-cap lineup
builder (DraftKings rules) and a separate "pick 5 props" over/under
challenge. Both have live standings, history, and a "my past
submissions" view.

Single-file Flask app (`app.py`, 5,000+ lines as of this writing) —
routes, DB schema, CLI commands, and helper functions all live there.
Templates are one `.html` file per page in `templates/`, sharing
`_info_modal.html` (an on-site help panel included on every page —
check this before adding a new concept to the site, since it's the
right place to document what something means for participants).

## Architecture principles (the important, non-obvious stuff)

**Scores are never stored — always computed live.** `lineups` and
`prop_picks` store only the original submission (which players, which
picks). Every score shown anywhere (Standings, My Lineups, My Props)
is computed fresh each request by joining that submission against
`hist_player_stats`/`hist_dst_stats`. This means `lineups`, `prop_bets`,
and `prop_picks` are the **only three tables with no external source to
rebuild from** if the database is ever lost — everything else can be
re-scraped. `flask export-critical-data` backs these three up
independently of whatever backup policy the hosting provider has;
run it regularly and keep the output somewhere else entirely.

**"Current NFL week" is a real, previously-buggy concept — use
`_get_current_nfl_week()`.** Don't infer it from "the latest (year,
week) in some other table" — that was a real, repeatedly-hit bug
(Weather/Depth Charts defaulted to a stale or wrong week more than
once before this existed). The correct definition, now centralized:
the *earliest* week whose games haven't all concluded yet (last
kickoff + 24h buffer still in the future) — not "the most recently
*started* week." That distinction specifically matters in the
pre-season gap, where a new season's Week 1 needs to count as current
before it's even kicked off. `_get_locked_teams(year, week)` is the
sibling helper for "has this specific team's game started yet" (used
for Slate's per-player lock, and My Lineups/My Props' locked-row
display for a given past week).

**Full-replace vs. upsert vs. historical accumulation — know which
each table is:**
- *Live/current-only, full-replace on every load:* `depth_charts`,
  `game_odds`, `scoresandodds_props`, `player_injuries`,
  `firstdown_studio_rankings`. These reflect "what's true right now,"
  not history — a full `DELETE` + re-insert on every load is correct,
  not wasteful.
- *Historical, accumulates forever, never wiped:* `hist_player_stats`,
  `hist_weather`, `hist_game_info`, `hist_dst_stats`,
  `hist_fantasy_points_against`, `hist_team_points`, `game_schedule`,
  `hist_player_usage`.
- *User-submitted, append/upsert by natural key:* `lineups`,
  `prop_picks`, `prop_bets`.

**Injury data has a two-source priority order, not a single source.**
Ourlads (scraped as part of the depth-chart pull, status read from the
badge in the player's own cell, e.g.
`<span class="badge badge-danger bad-ps">O</span>`, mapped through
`INJURY_STATUS_CODES` to out/doubtful/questionable/ir/etc. per the
page's own status key legend) loads first as a full replace. It used to
look for a `class="lc_red"` marker; that class disappeared from the
page and the scrape silently produced an empty injury file for at least
a week (found 2026-09-15). If Ourlads ever loads 0 rows again, suspect
the markup, not a quiet injury week. Draftedge loads second, but only fills gaps
(`ON CONFLICT DO NOTHING`) — it never overwrites a status Ourlads
already provided. If you need to touch injury loading, both
`replace_player_injuries()` and `fill_gap_player_injuries()` need to
be understood together; changing one without the other breaks the
priority order.

**Money-line-shaped props can't be safely converted to fantasy
points.** Implied Player Points deliberately excludes anytime-TD props
(priced as odds, not a line — can't de-vig without a "no" side price)
and the passing+rushing-yards combo prop (mixes two different scoring
rates). This is a real, permanent limitation, not a bug — it's why
FirstDown Studio's comparison column (`FDS Pts`) was added: their
number appears to include a TD estimate ours structurally can't
produce, so the two are meant to be read side by side, not as
duplicates.

## Hard-won gotchas — read before writing new SQL or scrapers

**SQLite is more permissive than Postgres in ways that hide real bugs
until production.** Two confirmed incidents:
1. `HAVING <alias> > 0` where `<alias>` is a `SELECT`-list alias —
   Postgres rejects this outright (`column "..." does not exist`);
   SQLite silently allows it. This crashed the Usage page in
   production while working fine locally. If a `HAVING`/`WHERE` needs
   to reference a computed value, either repeat the expression or
   restructure the query — don't rely on the alias being visible.
2. `psycopg2` returns `TIMESTAMP` columns as real Python `datetime`
   objects; SQLite returns them as plain strings. Slicing a
   `datetime` (`value[:16]`) crashes with `TypeError`. Always
   `str(...)` a timestamp value before it reaches a template that
   slices it — see how `slate()` handles `existing_lineup.submitted_at`
   for the established pattern, and make sure any new "submitted at"
   style field does the same.

   General rule: don't assume SQLite's behavior generalizes to
   Postgres for anything even slightly unusual. When in doubt, search
   for confirmation of Postgres's actual behavior rather than assume.

3. **The divergence also runs the OTHER way — Postgres forgiving,
   SQLite wrong.** A timestamp comparison built with `.isoformat()`
   produces a `T` separator (`2026-09-11T15:04:05`), but kickoffs are
   stored by `load-schedule` as `'%Y-%m-%d %H:%M:%S'` with a space.
   Postgres casts the string to a real timestamp and compares
   correctly; SQLite compares TEXT lexicographically, where `' '`
   (0x20) sorts *below* `'T'` (0x54) — so any kickoff on the same
   calendar date as the cutoff compared backwards. This was live in
   `_get_current_nfl_week()` and invisible precisely because prod is
   Postgres. Any string compared against a TIMESTAMP column must be
   formatted with `strftime('%Y-%m-%d %H:%M:%S')`, never `.isoformat()`.

**Plain `INSERT` into a table with a `UNIQUE` constraint will
eventually crash on a duplicate key from messy source data.** This
has happened twice with real scraped data (a "field"/generic betting
row parsed as a fake player in `scoresandodds_props`; a player listed
twice with conflicting statuses on `draftedge`'s injury page). Any
loader writing into a table with a `UNIQUE` constraint should use
`ON CONFLICT DO UPDATE` (or `DO NOTHING`, depending on the desired
semantics — see the injury priority section above) rather than a bare
`INSERT`, even if duplicates "shouldn't" happen. Check
`replace_firstdown_studio_rankings()` or `replace_player_injuries()`
for the established pattern.

**Legacy team codes must be normalized at ingestion time, not just in
the scraper.** `team_mapping.py`'s `normalize_team()` handles
`oak→lvr`, `sdg→lac`, `stl→lar`, but if `upsert_stats()` /
`upsert_team_points()` don't also call it at insert time, a stale CSV
sitting on disk can silently re-introduce old codes on a future
`load-history` run even after the scraper itself was fixed. This was
a real, confirmed bug — the fix has to be at the ingestion boundary,
not just the source.

**Name keys drop generational suffixes, and loaders must build them
with `_name_key()`.** Sources disagree on Jr./Sr./II–V in both
directions (FantasyPros "James Cook III" vs PFR "James Cook"; PFR
"Kenneth Walker III"), which left those players pending in Standings
until 2026-09-15. `normalize_name()` now drops a trailing suffix. Any
loader writing a `name_normalized`/`player_name_normalized` column must
go through `_name_key(stored_key, display_name)`, never copy the CSV's
key as-is and never rebuild from the display name alone: RotoGuru's
display names are "Manning, Peyton" (key "peyton manning"), so
rebuilding from the name would break ~51k historical rows. If
`normalize_name()` ever changes again, run `flask renormalize-names
--dry-run`, then without the flag, to rewrite keys already stored.

**Always use `_ph()` / `_ph(n)` for query placeholders, never a
hardcoded `?` or `%s`.** This is what makes every query dual-dialect
(SQLite uses `?`, Postgres uses `%s`). A hardcoded placeholder that
happens to work in dev will break in production.

**Every loader records itself in `data_loads` via `_record_load(cur,
source, path(s), row_count)`.** That row (source file mtime + load
time, naive UTC) is what each tab's "Data as of" line reads. A new
loader or `load-history` section that skips it leaves its tab silently
showing an old time, or none. Add the source key to
`DATA_SOURCE_LABELS`, and to `TAB_DATA_SOURCES` for each page it feeds.

## Conventions established through this project's development

**Test everything end-to-end before considering it done — not just
"looks right on inspection."** The established pattern throughout:
verify syntax first (`python -m py_compile`, and a Jinja2
`Environment.get_template()` load for any touched template), then
actually exercise the code with Flask's `test_client()` against a
real (if minimal) SQLite database, asserting on the actual rendered
output or actual computed values — not just that it didn't crash.
Several real bugs in this project were only caught this way (wrong
math, a crash that only appears in Postgres, a template variable that
was never actually wired to its route).

`test_app.py` is that harness, made permanent — run it with
`venv\Scripts\python.exe test_app.py` (no pytest). **Read its header
before adding to it.** The important part isn't the assertions, it's
the first three lines of setup: `.env` points DATABASE_URL at the
production Postgres and `app.py` calls `load_dotenv()` at import, so
importing app.py carelessly runs `_auto_init()` against **production**.
The file overrides DATABASE_URL to a temp SQLite path *before* the
import (python-dotenv defaults to `override=False`, so the already-set
value wins) and then asserts `not _is_postgres()`. Nothing may import
app above those lines. Reuse that harness rather than rebuilding it.

Testing on SQLite is also deliberate, not just safety: it's the only
engine that reproduces the timestamp-comparison bug class in gotcha 3
above, since Postgres silently casts the string and hides it.

**Every tab downloads, every download starts with the same join keys.**
Adding a new data page means adding a `/download/<data_type>` branch
too — `test_app.py::test_every_tab_has_a_download` fails otherwise.
Three rules hold the exports together:
- Buttons go through the `templates/_download_buttons.html` macro
  (`{% import '_download_buttons.html' as dl %}` then `{{ dl.bar([...]) }}`),
  never hand-written `<a class="download-btn">`. Before that macro
  existed there were nine different label spellings across fourteen
  buttons. Stick to the standard vocabulary in its header comment:
  This Week / This Season / This Team / All Data / Merge-Friendly.
- Run rows through `_prepend_keys(...)` so every CSV opens with
  `year`, `week`, `team`, `name_normalized` in a predictable place —
  `csv.DictWriter` takes column order from the first row's keys.
  Live full-replace tables with no week of their own (depth charts,
  game odds) get the current NFL week stamped on, or they can't be
  merged against anything.
- Player-grain exports get `_with_name_key(...)`. `name_normalized`
  is the documented join key; display names differ between sources and
  matching on them silently drops rows.

The grain/key table lives in `_info_modal.html` and `README.md` — keep
all three in sync when adding an export.

**Never build a new scraper without a diagnostic run against the real
target page first.** Every scraper in this project was built only
after a `diagnose_*.py` script dumped the actual HTML/JSON structure
and that structure was confirmed against real evidence — guessing at
selectors or JSON shape from memory has been avoided throughout, and
should stay that way. When adding a new data source, write the
diagnostic first, inspect its real output, then write the actual
scraper against confirmed structure.

**When a claim about external behavior isn't certain (a third-party
site's HTML meaning, a hosting provider's backup policy, a library's
current API), say so explicitly and check rather than assert
confidently.** This project has hit real gaps between assumption and
reality more than once (Ourlads' red-flag meaning was reported by the
user, not independently confirmed by the page itself; Render's backup
policy had genuinely conflicting sources). Flag the uncertainty in
comments/docs rather than presenting a guess as fact.

## Where to look for shared logic

- `_get_current_nfl_week()`, `_get_locked_teams(year, week)` — current
  week / per-team lock status, from real `game_schedule` kickoffs.
- `_get_player_teams(year, week, names)` — normalized player name ->
  team, tried against `players`, then `scoresandodds_props`, then
  `hist_player_stats`. Exists because `prop_bets` has no team column,
  so prop lock checks have nothing to join on directly. Used by both
  `props()` (for the LOCKED badge) and `submit_props()` (to enforce
  it), so display and enforcement can't disagree.
- `_compute_implied_points_table()`, `_compute_implied_points()` —
  shared by Slate, Best Matchups, and Implied Player Points.
- `_compute_implied_team_total(spread, over_under)` — shared by
  Implied Team Points, Best Matchups, and Game Overview.
- `DK_OFFENSE_LINES`, `DK_OFFENSE_BONUSES`, `DK_DST_LINES`,
  `DK_POINTS_ALLOWED_TIERS`, `_offense_breakdown()`, `_dst_breakdown()` —
  the per-stat scoring breakdown shown when a player row is clicked on My
  Lineups. They restate the rules in `scrape_pfr.calculate_dk_points()`
  and `combine_dst_scoring.calculate_dst_dk_points()`; change all three
  together (`test_scoring_breakdown` compares them). Any gap between the
  lines and the stored total (usually a 2-pt conversion, which our stats
  don't carry) becomes an "Other" line, so the breakdown always adds up.
- `_score_props_for_week(year, week)` — shared by Props, My Props,
  and Standings' prop section. A prop whose player has no stats row
  once his team's `hist_team_points` row is in is `void` (didn't play),
  and every caller treats `PROP_UNGRADED_RESULTS` (push, void) as
  counting neither for nor against. Lineups use 0 for the same case
  instead (see DNP below); the owner chose sportsbook-style void for props.
- `_lineup_player_rows(year, week, submitter)` — every submitted
  lineup's players with their real scored result, the per-player grain
  under both Standings and the My Lineups export.
  `_score_lineups_for_year(year)` rolls it up to
  `{submitter: {week: total or None}}`, applying the all-9-must-match
  rule. Standings, its CSV, and the My Lineups page all go through it
  (My Lineups used to carry its own copy of the matching logic).
  **Non-counting weeks:** `NON_COUNTING_WEEKS` (`{2026: {1}}`) weeks
  show everywhere but are left out of every season total: both lineup
  rankings, the dropped-week pick, and prop Correct/Accuracy. Check
  `_counts_toward_season(year, week)` in any new season-level math.
  The lowest week is only dropped once a participant has 2+ counting
  weeks (page and CSV both), so a lone week isn't zeroed out.
  **DNP rule:** a player with no stats row counts as 0 (`dnp: True`)
  once their team has a `hist_team_points` row for that week, since
  inactive/injured players never get a PFR row; before that they stay
  pending. Lineups store no team, so it comes from that week's
  `hist_dfs_salaries`/`players`.
- `_compute_usage_rows()`, `_compute_game_overview_rows()`,
  `_compute_implied_team_points_rows()` — page/CSV pairs, same
  one-source-of-truth reason as the above.
  `_compute_usage_rows(year, position, week=None)` joins
  `hist_player_stats` (G, Tgt/G, Touch/G) to `hist_player_usage`
  (snap %, aDOT, air yards share, WOPR, red zone) on
  (name_normalized, team); its docstring has each column's season math.
  Tgt % and Touch % use nflverse counts (targets; carries + receptions)
  for player and team whenever the team has usage rows, PFR otherwise:
  our older PFR seasons are missing players, so PFR team totals ran low
  and shares high (Brandon Marshall 2012: 53.2% on PFR, 40.2% on nflverse).
  `hist_player_usage` comes from `scrape_nflverse_usage.py` (nflverse
  GitHub releases, CC-BY, credited on the page; not PFR), loaded by
  `flask load-history --usage-only`. nflverse snap counts can miss a
  player who has stats, so Snap % falls back to PFR's
  `hist_player_stats.snap_pct` per player-week. Routes run can't be
  added in-season: nflverse publishes route participation only after
  the season.
- `_prepend_keys()` / `_with_name_key()` — every CSV export runs
  through these. See the download conventions below.
- `data_loads` table, `_record_load()`, `_data_as_of(*sources)` — one
  row per source key (salaries, player_stats, weather, game_info, dst,
  fantasy_points_against, team_points, depth_charts, injuries,
  game_odds, props_market, prop_bets, firstdown, schedule, usage). The
  `_inject_data_as_of` context processor maps `request.endpoint`
  through `TAB_DATA_SOURCES` and `templates/_data_as_of.html` renders
  the line under each tab's subtitle, in ET.
- `_get_game_odds_week()` — the real (year, week) of the loaded
  `game_odds`, by matching each row's team + `kickoff` to
  `game_schedule` within a day (most common week wins). Never the
  current week: odds can lag it. `_game_odds_rows_for_week()` returns
  no odds for a page showing a different week (Game Overview, Best
  Matchups), `_odds_week_note()` explains it on the page.
- `TEAM_ROW_COLORS` — static per-team brand-color tint, used for the
  background tint on Best Matchups, Team Points, Fantasy Points
  Against, Implied Team Points, and the Depth Charts team buttons.
- `TEAM_ROOF_TYPE` — static per-stadium dome/outdoor lookup (not
  scraped; a fixed fact per team).
- `_ph()` / `_ph(n)`, `_is_postgres()`, `db_fetchall()`/`db_fetchone()`/
  `db_execute()` — the dual-dialect DB layer everything else sits on.

## File structure

```
app.py                  Single Flask app: routes, schema, CLI commands, helpers
templates/*.html        One file per page, + _info_modal.html (shared help panel)
static/style.css        Shared styles across every page
static/*.js             Per-page interactivity (slate.js, props.js, history.js)
scrapers/scrape_*.py    One script per external data source
scrapers/diagnose_*.py  Structure-confirmation scripts, run before writing/changing a scraper
scrapers/find_*.py      Market analysis (arbitrage, value bets, outlier lines)
data/*.csv.gz           Scraper output, consumed by `flask load-history`
README.md               Operational workflow — read this for the actual commands
```

This list reflects what existed as of this file's writing — `ls` the
real directories rather than trusting this to stay perfectly current,
since new scrapers/pages get added over time.

## Project status

*Last updated: Tuesday 2026-09-15, ~4:00 AM local.* Update this
section whenever the season moves forward or an item opens/closes.

### Where the season is

- **2026 season. Week 1 is over** (Thu 9/10 – Mon 9/14); **Week 2 is
  current** (Thu 9/17 – Mon 9/21). `_get_current_nfl_week()` is the
  source of truth; this line is just orientation.
- Weekly rhythm: Tuesday morning = score the finished week
  (`weekly_after.bat`) + prep the next one (`weekly_before.bat`).
  Props usually can't be finalized Tuesday; see open items.

### Accomplished overall (as of 2026-09-15)

Built between 2026-08-19 and 2026-09-11 (see `git log` for detail):
- Core: Flask app, login auth with bootstrap users via env vars,
  auto-init DB, dual SQLite/Postgres, deployed on Render.
- Weekly DK salary-cap lineup builder (Slate), with per-player game
  lock, player search, hide-3rd-string/2nd-string/locked filters.
- Pick-5 props challenge with server-side lock enforcement.
- Standings (lineups + props), History, player career pages, My
  Lineups, My Props.
- Research tabs: Best Matchups, Implied Player Points (+ FDS Pts
  comparison), Implied Team Points, Game Overview, Team Points,
  Fantasy Points Against, Usage, Weather/Game Info, Depth Charts +
  injuries (Ourlads, then Draftedge gap-fill), Schedule.
- Historical data 2014–2025 loaded (PFR stats + game info, RotoGuru
  salaries, DST scoring, team points).
- Every tab downloads, with consistent join keys and Merge-Friendly
  variants; on-site info modal; `test_app.py` harness; weekly `.bat`
  scripts with a critical-data backup as step 1.
- Week 1 (2026) run end to end: salaries, props published, lineups
  and picks submitted.

### This week: 2026-09-15 (score Week 1, prep Week 2)

Done:
- Week 2 prep scrapes: schedule, DK salaries
  (`fp_dk_salaries_week2_2026.csv.gz`), weather forecasts, Ourlads
  depth charts, Draftedge injuries, FDS season projections, props
  candidates.
- Week 1 PFR player stats (after refreshing the PFR cookies) and game
  info for all 16 Week 1 games.
- **Fixed `scrape_pfr.py` saving unplayed games.** Game-info scraping
  now skips games dated today or later, won't save a boxscore page
  with no game info, and retries empty rows left by older runs. Before
  the fix, a run saved 4 empty Week 2 rows, which would have marked
  those games done forever. The rows were removed by hand; a
  pre-cleanup copy is at
  `data/pfr_game_info_2014_2025_backup_20260915.csv.gz` (safe to
  delete once Week 2 game info scrapes cleanly).
- **Fixed `scrape_pfr.py` skipping returning players mid-season.**
  Player stats used to mark `(pfr_id, year)` done as soon as a player
  had any row for that year, so from Week 2 on every returning player
  would have been skipped. Now a season only counts as done once its
  last scheduled regular-season game is past (`_season_finished()`);
  until then, players who already have the latest completed week
  (`_latest_completed_week()`) are skipped and everyone else is
  fetched, so a run interrupted by a PFR block resumes cheaply.
  `--full-refetch` re-fetches every player in the season (~35 min);
  re-fetched rows replace older ones, so that also picks up PFR stat
  corrections to earlier weeks.
- DEN @ KC (MNF) re-run ~10:20–11:05 AM once PFR posted it: all 32
  teams now in player stats, team points, DST scoring, game info, and
  fantasy points against for Week 1.
- **Name suffixes fixed** (Jr./Sr./II–V dropped from name keys;
  `flask renormalize-names` rewrote 2,003 stored keys in prod, 0
  collisions). Brian Thomas Jr. and James Cook III now score.
- **Players with 0 DK points are no longer skipped mid-season.**
  `scrape_pfr.py` only applies its `dk_pts_season > 0` filter to
  finished seasons; 6 Week 1 players (Pitts, Doubs, Loveland, Jennings,
  Stribling, Gilliam) had real games with targets/snaps but no row, so
  their lineups sat pending. Re-scraped ~12:00 PM.
- **DNP = 0** for players with no stats once their team's result is in
  (Bowers, Devontez Walker, DePaola: injured). See `_lineup_player_rows`.
- **load-history is faster:** loaders write through `_executemany()`
  (psycopg2 `execute_batch` on Postgres instead of one round trip per
  row), and `--year 2026` limits the historical files to one season.
  The weekly `.bat` scripts now pass `--year %YEAR%`. Measured against
  Render on 9/15: `--year 2026 --stats-only` 3.7 s; all 90,507 stats
  rows 236 s (vs ~20 min with executemany).
- After both fixes, all 7 Week 1 lineups score (no pending); DNP:
  Brock Bowers, Devontez Walker, Andrew DePaola. The DNP badge on My
  Lineups needs the deploy to show on the live site.
- Week 1 team points, DST stats, DST scoring, final weather, fantasy
  points against. Then `flask export-critical-data` (backups/
  `*_20260915_084224.csv`), `flask load-schedule --year 2026`,
  `flask load-weekly-salary ... week2` (800 players; Slate shows Week 2).

Evening (Week 2 prep, all loaded to prod):
- Week 2 game odds, props market (1,752 rows), FirstDown rankings (187),
  depth charts (454), injuries (318: Ourlads 18 + Draftedge gap-fill).
- **Fixed: Rams props were invisible.** ScoresAndOdds writes the Rams as
  bare "LA", which `normalize_team()` didn't map, so every Rams player
  had no team and their props would never have locked at kickoff. Added
  `'la': 'lar'` to `team_mapping.EXTRA_ALIASES` (both that site and
  nflverse use "LAC" for the Chargers, so "LA" is unambiguous). Also
  fixed FirstDown's team column and Ourlads' injury badge; see the two
  RESOLVED entries below.

Not yet done:
- Week 2 props not published. `data/props_week2_2026.csv` is generated
  (72 props, 12 categories, 15 games) but the `touchdowns` six are dead
  picks: `select_top_props_by_category.py` takes the site's first 6 per
  category with no ranking, and ScoresAndOdds orders anytime-TD props
  long-shots-first (Tanner Koziol +2300 at 0.000 projected TDs).
  Ranking that category by `site_projection` gives Henry/McCaffrey/
  Gibbs/Robinson instead. Decide before `flask add-props`; Thursday
  kickoff is 9/17 8:15 PM ET.
- `flask load-history` finished cleanly at ~5:21 AM (took ~38 min; it
  re-loads every historical file on each run). A second full load with
  the DEN @ KC data finished cleanly at 11:42 AM, so all 16 Week 1
  games are scored.

### Open items (confirmed, not yet resolved)

- **PFR can post Monday night results hours late.** On 9/15 DEN @ KC
  wasn't on PFR at 4:40 AM but was by ~10 AM. A very early Tuesday run
  can miss MNF; the fix is a later re-run of `scrape_pfr.py`,
  `scrape_team_points.py`, `combine_dst_scoring.py`,
  `scrape_fantasy_points_against.py`, then `flask load-history`.
- **Ask before any PFR request.** The site owner runs another app
  (GridIronGuesser, `scrape_historical_stats.py`) that also scrapes PFR,
  and two scrapers hitting PFR at once trigger 429 rate limits / 403s.
  Before running `scrape_pfr.py`, `scrape_team_points.py`,
  `scrape_fantasy_points_against.py`, or even a one-off test request,
  ask whether another PFR scrape is running. Non-PFR scrapers
  (ScoresAndOdds, FantasyPros, Ourlads, etc.) don't need this.
- **PFR cookies lasted ~6 hours on 9/15** (refreshed ~3 AM, 403 at
  10:58 AM). Expect a refresh before each Tuesday run and possibly
  mid-day.

- **Week 2 props and game odds are really Week 1 data.** Scraped
  Tuesday morning, ScoresAndOdds still showed Week 1 matchups (CIN–TAM,
  DET–NOR…). `data/props_week2_2026.csv` must NOT be published as-is.
  Re-run `scrape_scoresandodds_game_odds.py` and the three props steps
  once Week 2 lines are posted (likely Tue afternoon–Wed), review, then
  `flask add-props ... --week 2`. Consider moving the odds/props steps
  out of Tuesday-morning `weekly_before.bat` or adding a matchup-week
  sanity check.
- **Props scrape data quality:** about 80 rows across several categories
  had neither a line nor a price; the auto-selected slate included
  junk lines (Jerry Jeudy rush yds 0.5, Sione Vaki rush yds 0.5) and
  a blank team (Matthew Stafford). Review before publishing.
- **RESOLVED 2026-09-15 (evening): FirstDown Studio rankings.** The
  morning's "no `<table>` found" was just timing (Week 2 wasn't posted);
  187 rows scraped and loaded that evening. A separate real bug turned
  up while checking it: the scraper read the avatar span (the player's
  initials, "JA" for Josh Allen) as the team, so `team` was empty on
  nearly every row. It now reads the matchup span ("BUF vs DET") via
  `MATCHUP_RE`. Nothing on the site joined on that column (FDS Pts
  matches on `player_name_normalized`), so no page was wrong.
  Still true: the scraper writes nothing when it finds no table, so
  load-history silently re-loads the previous week's file.
- **RESOLVED 2026-09-15 (evening): Ourlads injuries.** Not a quiet
  injury week: the `lc_red` class is gone from their page entirely. Now
  read from the status badge (see the injury-priority note above), which
  is strictly better than the old binary flag since it carries the real
  code. First run after the fix: 18 rows (16 out, 1 inactive, 1
  questionable) across 13 teams, incl. Brock Bowers, vs 0 before.
- **PFR rate limiting stalls the weekly run.** After ~265 player pages
  + game pages, PFR returned 429 with `Retry-After: 2884` (~48 min).
  `pfr_get()` honors it silently, and its prints aren't flushed when
  output is redirected, so the run looks hung. `weekly_after.bat` then
  hits PFR again right away (team points, fantasy points against). Worth
  a visible "waiting N s until HH:MM" message and/or a pause between
  PFR-heavy steps.
- **PFR cookies expire within hours.** Expect to refresh
  `PFR_CF_CLEARANCE`/`PFR_CF_BM`/`PFR_USER_AGENT` in `.env` before most
  Tuesday runs; a 403 on the first request means refresh.
- **`CHANGELOG.md` is stale.** It stops at "[Step 4a] — Historical
  data scrapers" and predates props, standings, best matchups,
  implied points, depth charts, injuries, and most of the current
  site. `README.md` and this file are current; `CHANGELOG.md` is not,
  so don't use it to reason about what exists.

### Recently closed (kept as context, not as work)

- **2026-09-15: `scrape_pfr.py` no longer saves unplayed games, and
  re-fetches every player while a season is in progress** (see
  "This week" above).

- **Prop picks now DO have server-side lock enforcement.**
  `submit_props()` rejects adding a locked prop, flipping over/under
  on one, *and* dropping one already picked (that last case matters —
  without it a losing pick could just be swapped out). Team resolution
  goes through `_get_player_teams()`, since `prop_bets` has no team
  column of its own.
- **The day-header script is integrated and its scaffolding deleted.**
  `updateDayHeaderVisibility()` now lives in `slate.js` proper and its
  CSS in `style.css`; `static/slate_day_header_addition.js` and
  `static/style_day_header_addition.css` are gone.

## Quick orientation for a fresh session

0. Read **Project status** above — what week it is, what's done, and
   what's open. If it's stale relative to today's date, say so and
   update it as you learn the real state.
1. Read `README.md` for the current weekly workflow and known future
   ideas.
2. `grep -n "CREATE TABLE" app.py` for the current schema — don't
   trust a written-down schema snapshot, since it drifts.
3. `grep -n "@app.route" app.py` for the current route list.
4. Check `_info_modal.html` for how existing site concepts are
   explained to participants — matches that tone/depth for anything
   new.
