# CLAUDE.md

Context for working on this codebase. This complements `README.md`
(which covers day-to-day operational commands — scraping, loading,
the weekly workflow) rather than duplicating it — read that too for
anything workflow-related.

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
  `hist_fantasy_points_against`, `hist_team_points`, `game_schedule`.
- *User-submitted, append/upsert by natural key:* `lineups`,
  `prop_picks`, `prop_bets`.

**Injury data has a two-source priority order, not a single source.**
Ourlads (scraped as part of the depth-chart pull, injury status
inferred from a `class="lc_red"` marker — confirmed real via a
targeted check, but note it's a *binary* flag with no
Questionable/Doubtful/Out distinction visible in their HTML) loads
first as a full replace. Draftedge loads second, but only fills gaps
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

**Always use `_ph()` / `_ph(n)` for query placeholders, never a
hardcoded `?` or `%s`.** This is what makes every query dual-dialect
(SQLite uses `?`, Postgres uses `%s`). A hardcoded placeholder that
happens to work in dev will break in production.

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
- `_score_props_for_week(year, week)` — shared by Props, My Props,
  and Standings' prop section.
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

## Known open items (confirmed, not yet resolved)

- **`CHANGELOG.md` is stale.** It stops at "[Step 4a] — Historical
  data scrapers" and predates props, standings, best matchups,
  implied points, depth charts, injuries, and most of the current
  site. `README.md` and this file are current; `CHANGELOG.md` is not,
  so don't use it to reason about what exists.

### Recently closed (kept as context, not as work)

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

1. Read `README.md` for the current weekly workflow and known future
   ideas.
2. `grep -n "CREATE TABLE" app.py` for the current schema — don't
   trust a written-down schema snapshot, since it drifts.
3. `grep -n "@app.route" app.py` for the current route list.
4. Check `_info_modal.html` for how existing site concepts are
   explained to participants — matches that tone/depth for anything
   new.
