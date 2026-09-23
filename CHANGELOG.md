# CHANGELOG - FF DFS Challenge Site

What happened, in order. `README.md` is how to run things and `CLAUDE.md`
is where things stand and what to watch out for; this file is the history.

Steps 1 to 4a below are the original build log from August 2026, kept as
written. Everything after them is grouped by date, because the work stopped
arriving in neat numbered steps once the season started.

---

# Part 2 - September 2026: the season

## Sept 2-7 - the research tabs

Most of the site's pages arrived in one stretch, each one a scraper plus a
page plus a download:

- **Best Matchups**, built on fantasy points allowed by position, with a
  trailing-10-game window that crosses season boundaries.
- **Depth Charts** (Ourlads), with injury designations alongside.
- **Props**, the pick-5 over/under challenge, with the weekly slate coming
  from ScoresAndOdds.
- **Implied Team Points** and **Implied Player Points**, both from the
  Vegas spread and total, plus FirstDown Studio's own numbers as a
  side-by-side comparison column.
- **Weather / Game Info**, **Team Points**, **Fantasy Points Against**,
  **Schedule**, **Standings** and the player career pages.
- Every tab got a download, and the CSVs were made merge-friendly: same
  join keys in the same place, so two downloads line up in a spreadsheet.

## Sept 8-11 - challenges running, and the first real bugs

- Week 1 props published; standings tracking for both challenges.
- Slate gained player search and filters for third string, second string
  and locked players.
- Prop picks got **server-side lock enforcement**, not just a hidden
  button: the server rejects adding a locked prop, flipping a pick, and
  dropping one already made. That last case matters, since without it a
  losing pick could simply be swapped out.
- **A SQLite/Postgres divergence bit production.** `psycopg2` returns
  timestamps as real datetimes while SQLite returns strings, so a template
  slicing `value[:16]` crashed only in production.
- `test_app.py` became the permanent harness: it builds a throwaway SQLite
  database and asserts on real rendered output, never touching Render.
- `CLAUDE.md` written.

## Sept 15 - Week 1 scored, and five scrapers found lying

Scoring the first week exposed more than it fixed, which is why this date
has the longest list:

- **`scrape_pfr.py` was saving unplayed games**, writing empty rows for
  Week 2 that would have marked those games done forever.
- **It was also skipping returning players mid-season**, because a season
  counted as "done" for a player as soon as he had any row in it.
- **Players with 0 DK points were being dropped**, so six Week 1 players
  with real snaps and targets never scored.
- **Name keys kept generational suffixes**, so Brian Thomas Jr. and James
  Cook III sat unscored. `normalize_name()` now drops them and
  `flask renormalize-names` rewrote 2,003 stored keys.
- **Three scrapers were silently broken at once**: ScoresAndOdds writes the
  Rams as bare "LA" (so every Rams prop had no team and would never have
  locked), FirstDown's team column was reading the player's initials, and
  Ourlads' injury scrape had been returning nothing for a week because the
  CSS class it looked for no longer exists.
- **DNP now scores 0** once a team's result is in, since inactive players
  never get a stats row.
- Week 1 left out of season totals (`NON_COUNTING_WEEKS`), and a week is
  only dropped once a participant has two counting weeks.
- `load-history` got `--year` and batched inserts: a 90k-row load went from
  about 20 minutes to under 4.
- **`check_scraper_output.py` added**, because all of the above shared one
  shape: a scraper exiting 0 having written nothing useful.
- Usage tab added, built on nflverse snap counts, air yards and red zone
  data rather than PFR, whose older seasons are missing players.

## Sept 16-21 - betting research, and Week 2

Personal research, separate from the site, documented in
`scoresandodds_workflow.md`:

- **`find_ev_bets.py`**: prices DraftKings and Caesars against the no-vig
  consensus of every other book, converting line differences into
  probability with per-stat spreads fitted from 2018-2025 game logs.
- **`td_model.py` / `find_td_bets.py`**: an anytime-TD model from the Vegas
  implied team total and a player's recent opportunity share, fitted on
  2015-2023 and tested on 2024-2025 before being trusted.
- **`bet_tracker.py`**: archives every odds scrape, logs every flagged bet,
  and grades them afterwards with closing line value.
- **`find_stale_lines.py`** and **`market_context.py`**, both built from the
  Puka Nacua case: when he was ruled out, the other books moved his
  teammates within hours and DraftKings didn't, which the consensus method
  read as an edge at the *stale* book rather than news.
- Week 2 graded: the consensus method landed on expectation, while the TD
  model's picks scored 6 touchdowns against its own estimate of 12.8, so it
  runs hot and gets small stakes until proven.

## Sept 22 - a busy day

- **Week 2 scored.** The 2:25 AM run reached the Giants-Rams players before
  PFR had posted that game, so their rows never existed while the game's
  team points did, and the DNP rule scored Davante Adams' 195-yard, 2-TD
  night as a zero. Caught by checking row counts per team, fixed by a
  re-run.
- **The database moved from Render to Neon** before Render's free month
  expired. Every table verified by checksum, and the app's own scoring
  logic run against both databases to confirm identical answers. The trap:
  `COPY` maps columns **by position**, and three tables had grown columns
  via `ALTER TABLE`, so a naive restore would have silently loaded 94k rows
  of `hist_player_usage` into the wrong columns.
- **`db_backup.py`** added (dump / restore / verify), and the three tables
  no scraper can rebuild are now committed weekly.
- **The current-week rule changed** to the NFL's own boundary, Tuesday
  4 AM ET, after the old "24 hours past the last kickoff" left Weather,
  Schedule and Depth Charts showing the finished week until 8:15 PM Tuesday.
- **The market scrape got 5x faster.** The ScoresAndOdds API's player
  filter turns out to be optional, so one request returns a whole market:
  224 requests instead of 1,689, six minutes instead of thirty-four.
  Verified with a diagnostic across all 18 categories before switching.
- Week 3 props published, which caught a name collision: Draftedge lists a
  Cleveland linebacker named Justin Jefferson, and matching on name alone
  flagged the Vikings receiver as out.

## Sept 23 - the laptop stops being required

- **`.github/workflows/refresh-live-data.yml`**: depth charts, injuries,
  game odds, props, the per-book market scrape and FirstDown rankings, five
  times a day, loaded straight into Neon, with the odds snapshot committed
  back to the repo. PFR stays local, since its cookies expire in hours and
  it has to stay coordinated with another app.
- **The first run silently did nothing.** `app.py` falls back to a local
  SQLite file whenever `DATABASE_URL` isn't a Postgres URL, so a malformed
  secret meant every load "succeeded" into a throwaway file on the runner
  while the real database sat untouched, and the job went green. The
  workflow now fails in 20 seconds if it isn't pointed at Postgres.
- **`pull_live_data.py`** brings it all back to the laptop: the exact
  scraped files from the last run, the newest archived market snapshot, and
  optionally the live tables.
- `scrape_weekly_weather.py` prints ASCII only. It had been saving its file
  and then crashing on the final line, because Windows' cp1252 can't encode
  an arrow, so a successful run looked like a failure.

## Where things stand (Sept 23, 2026)

Weeks 1 and 2 are scored, Week 3 is live with salaries, props and research
tabs current. The site runs on Neon with backups in three places, and the
live data refreshes itself five times a day without the laptop. What still
needs a person: the Tuesday scoring run (PFR), and grading the week's bets.

---

# Part 1 - August 2026: the original build log

---

## [Step 1] — Project scaffold + player slate table

### What was built
- `app.py` — Flask app with two CLI commands and one route:
  - `flask init-db` — creates the SQLite tables (`users`, `players`)
  - `flask ingest-slate --week N [--slate-id N]` — hits RotoWire API, populates `players` table
  - `GET /` — displays the current week's player slate
- `scraper.py` — RotoWire DFS data scraper ported from the original `FF_2025` repo
  - `fetch_slate_data(slate_id, week, year)` — hits the RotoWire JSON API
  - `find_latest_slate()` — auto-discovers the current Thu-Mon Classic slate ID by walking backward from a start ID
- `templates/slate.html` — public player table page (position, name, team, opp, salary, proj pts, proj own%, value)
- `static/style.css` — dark-themed data table design
- `static/slate.js` — client-side column sorting (no dependencies)
- `requirements.txt` — Flask, requests, BeautifulSoup, gunicorn
- `Procfile` — for Render/Heroku deployment
- `.gitignore` — excludes DB, pyc, venv, .env

### Key decisions made
- SQLite for now, easy path to Postgres later
- RotoWire JSON API (same endpoint as original repo) for salary/projection data
- Slate data auto-discovery by walking backward from a known high slate ID
- Dark UI theme inspired by firstdown.studio
- No auth yet — everything is public at this stage
- Hosting target: Render (free tier to start)
- Credentials will be assigned manually (known group of ~20 participants)
- Lineup submission: click-to-select from the slate table (not yet built)

### Open questions / deferred items
- RotoWire slate ID starting point (`find_latest_slate`) uses 9500 as default — will need to be updated/verified at the start of the 2025 season
- Player props data source: The Odds API (~$29/mo during season) — deferred until props challenge is built
- `users` table created but no auth routes yet
- Scoring engine (`Weekly_Scores_Scrape.py`) not yet ported into Flask
- Lineup submission UI not yet built
- Season standings page not yet built

### Next proposed step (Step 2)
Add lineup submission:
- Position filter buttons above the table (QB / RB / WR / TE / DST / All)
- Click a row to select a player; sidebar shows current lineup + running salary
- Enforce roster rules (1 QB, 2 RB, 3 WR, 1 TE, 1 FLEX, 1 DST) and $50k cap
- "Submit Lineup" button (no auth yet — just saves to a `lineups` table keyed by a name the user types)

---

---

## [Step 2] — Lineup builder

### What was built
- **`app.py`** — added:
  - `lineups` table (week, year, submitter, lineup_json, total_salary, submitted_at; unique per submitter+week)
  - `POST /submit-lineup` route — validates roster rules + $50k cap server-side, then upserts into `lineups`
  - Default year changed from 2025 → 2026
- **`templates/slate.html`** — full two-column layout:
  - Left: player table with position filter buttons (All / QB / RB / WR / TE / DST)
  - Right: sticky lineup sidebar (9 roster slots, live salary counter, name input, submit button)
- **`static/style.css`** — all new sidebar + slot styles; selected/ineligible row states; over-cap warning color
- **`static/slate.js`** — complete lineup builder logic:
  - Click row → auto-fills correct slot (primary slot first, FLEX fallback)
  - Click selected row → removes player
  - Ineligible rows dimmed (no open slot OR adding would bust cap)
  - Clear (✕) button per slot
  - Submit → POST to `/submit-lineup`, shows success/error message
  - Column sort preserved

### Key decisions made
- Players auto-route to first open eligible slot; FLEX is used as fallback for RB/WR/TE
- Lineup re-submissions overwrite previous entry (ON CONFLICT DO UPDATE) — participants can change their lineup before the deadline
- Server re-validates everything (don't trust client-side checks)
- No auth yet — submitter identity is just a typed name for now

### Open questions / deferred items
- **1-player scraper bug** — RotoWire JSON structure may have changed for 2026 slates; needs investigation when slates go live
- **Auth** — still deferred; right now anyone can submit under any name
- **Submission deadline enforcement** — no lockout before kickoff yet
- **Season standings page** — not yet built
- **Scoring engine** — not yet ported into Flask

### Repo cleanup needed (do this before pushing)
```bash
mv gitignore .gitignore
git rm --cached ff_dfs.db
git rm -r --cached __pycache__/
git rm --cached .DS_Store
git add .
git commit -m "Step 2: lineup builder + repo cleanup"
git push
```

### Next proposed step (Step 3)
- Add simple session-based auth (Flask-Login or manual sessions)
- Admin creates users with username + hashed password
- Login required to submit a lineup (name field replaced by logged-in user)
- Public pages (slate, standings) remain unauthenticated

---

## [Step 3] — Authentication (Flask-Login)

### What was built
- **`app.py`** — added:
  - `flask-login` + `bcrypt` integration
  - `User` class (UserMixin) loaded from DB per request
  - `SECRET_KEY` env var for session signing (falls back to dev value)
  - `flask create-user --username X --password Y` — hashes password with bcrypt, inserts into DB
  - `flask list-users` — prints all registered participants
  - `flask delete-user --username X` — removes a user
  - `GET/POST /login` — login form, bcrypt password check, redirects to `?next=` if set
  - `GET /logout` — clears session, redirects to login
  - `/submit-lineup` now requires `@login_required`; submitter always comes from `current_user.username` (never from request body)
  - Slate route passes `existing_lineup` to template if user already submitted this week
- **`templates/login.html`** — clean centered login form with error display
- **`templates/slate.html`** — updated:
  - Header shows "username | Sign out" when logged in, "Sign in to submit" when not
  - Submit area shows login link (not a button) when unauthenticated
  - Existing-lineup banner shows timestamp if already submitted this week
  - Name text field removed entirely
- **`static/style.css`** — auth card, login form, header badge, existing-banner styles appended
- **`static/slate.js`** — updated:
  - Reads `window.APP.isAuthenticated` and `window.APP.existingLineup`
  - Pre-populates sidebar slots from existing lineup on page load
  - Submit handler no longer sends submitter name (server reads from session)
  - Unauthenticated users can still browse/build lineup visually but can't submit
- **`requirements.txt`** — added `flask-login>=0.6`, `bcrypt>=4.0`

### Key decisions made
- Passwords stored as bcrypt hashes (never plaintext)
- `SECRET_KEY` must be set as env var in production (Render dashboard)
- All user management via CLI — no admin web UI needed
- Unauthenticated users see the slate and can build a lineup but get a "Sign in to submit" prompt
- Lineup re-submissions overwrite the previous one (same as before)

### Deployment steps
```bash
# Install new dependencies
pip install -r requirements.txt

# Re-run init-db to ensure tables exist (safe to re-run, uses CREATE IF NOT EXISTS)
flask init-db

# Create your first user
flask create-user --username yourname --password yourpassword

# Set SECRET_KEY in prod (Render env vars dashboard)
# SECRET_KEY=some-long-random-string

git add .
git commit -m "Step 3: Flask-Login authentication"
git push
```

### Open questions / deferred items
- No password-reset flow — admin just deletes and recreates the user
- No submission deadline enforcement yet (lockout before kickoff)
- Standings page not yet built
- Scoring engine not yet ported

### Next proposed step (Step 4)
Season standings page at `/standings`:
- Public page listing all participants
- Columns: Rank, Participant, Wk1, Wk2, … WkN, Total (drop lowest)
- Pulls from `lineups` table — shows submitted vs. not-yet-scored
- Scoring engine port: `flask score-week --week N` CLI command that runs the PFR scrape and writes scores back to `lineups`

---

## [Step 3b] — Switch to PostgreSQL (persistent DB on Render free tier)

### What was built
- **`app.py`** — full dual-driver rewrite:
  - Detects `DATABASE_URL` at startup: if it starts with `postgres` → uses `psycopg2`; otherwise → uses `sqlite3`
  - `_connect()` / `_cursor()` / `_ph()` helpers abstract the driver differences
  - `db_fetchone()`, `db_fetchall()`, `db_execute()` replace raw sqlite3 calls throughout
  - All SQL uses `%s` placeholders for Postgres, `?` for SQLite
  - `ON CONFLICT` upserts work in both (syntax is identical between SQLite 3.24+ and Postgres)
  - `_auto_init()` creates tables using the correct DDL for each driver (SERIAL vs AUTOINCREMENT, NOW() vs datetime('now'))
  - CLI commands (create-user, list-users, delete-user, ingest-slate) all updated
- **`requirements.txt`** — added `psycopg2-binary>=2.9`

### Key decisions made
- `psycopg2-binary` (not `psycopg2`) — includes compiled C extension, no system libpq needed on Render
- SQLite still works locally — no Postgres install required for development
- `DATABASE_URL` is the single config knob: set it in Render env vars, leave it unset locally

### Deployment steps
1. **Create Render Postgres DB:**
   - Render dashboard → New + → PostgreSQL
   - Name it `ff-dfs-db`, region same as your web service, instance type Free
   - Copy the **Internal Database URL** (starts with `postgres://...`)

2. **Link to web service:**
   - Go to your web service → Environment tab
   - Add env var: `DATABASE_URL` = (paste Internal Database URL)
   - The `BOOTSTRAP_USER` / `BOOTSTRAP_PASS` vars create your first user on deploy

3. **Push the code:**
   ```bash
   git add app.py requirements.txt
   git commit -m "Step 3b: PostgreSQL support, dual SQLite/Postgres driver"
   git push
   ```

4. **Render auto-redeploys** — watch logs for "Bootstrap user created" and no errors

### Note on local dev
- Locally, `DATABASE_URL` is unset → SQLite (`ff_dfs.db`) is used as before
- `flask ingest-slate --week 1 --slate-id 2000` still works locally for testing

### Open questions / deferred items
- 1-player scraper bug still unresolved (needs a valid 2026 slate or raw API debug output)
- Standings page not yet built
- Scoring engine not yet ported

### Next proposed step (Step 4)
Standings page at `/standings` + scoring engine:
- `flask score-week --week N` CLI command — runs PFR scrape, calculates each lineup's score, writes to DB
- Public `/standings` page — leaderboard table: Rank, Participant, Wk1…WkN, Total (drop lowest)

---

## [Step 4a] — Historical data scrapers (RotoGuru + PFR)

### What was built
Two standalone scraper scripts in `scrapers/`. These run locally, produce
flat files, and only need re-running once per season.

**`scrapers/scrape_rotoguru.py`**
- Scrapes RotoGuru's historical DraftKings data 2014–2021 (all weeks)
- URL format confirmed from uploaded HTML: `?week=W&year=Y&game=dk&scsv=1`
- scsv columns: `Week;Year;GID;Name;Pos;Team;h/a;Oppt;DK points;DK salary`
- Output: `data/rotoguru_dk_2014_2021.csv.gz`
- Resume-safe (skips already-done year/week pairs), 2s polite sleep
- Adds `name_normalized` (lowercase, no punctuation) for joining with PFR

**`scrapers/scrape_pfr.py`**
- Extended version of `Weekly_Scores_Scrape.py` with full stat columns
- Player stats: dk_pts + pass_cmp/att/yds/td/int, rush_att/yds/td, rec_tgt/rec/yds/td, snap_pct
- Game info: roof, surface, weather, attendance, vegas_line, over_under (from boxscores)
- Two outputs: `data/pfr_player_stats_2014_2025.csv.gz` and `data/pfr_game_info_2014_2025.csv.gz`
- Resume-safe (checkpoints every 20 players), 4s sleep between player pages
- CLI: `python scrapers/scrape_pfr.py --years 2014-2025` (or `--skip-players`, `--skip-games`)
- Expected runtime: ~8-10 hours for full 12-year run

**`scrapers/README.md`** — full workflow documentation

### Key decisions made
- **gzipped CSV** over parquet — simpler (no pyarrow), still small, pandas reads natively
- **name_normalized** is the join key between RotoGuru and PFR data (lowercase, no punctuation)
- **RotoGuru GID** preserved as `rg_id` for future player mapping table
- **PFR slug** (`pfr_id`) is the gold-standard player identifier
- Game info (weather, Vegas) scraped in same pass as schedule to avoid duplicate PFR hits
- 2022–2025 DFS salary gap: covered by RotoWire live ingest (existing `flask ingest-slate`)

### Data coverage
| Years     | DFS Salaries       | Detailed stats |
|-----------|--------------------|----------------|
| 2014–2021 | RotoGuru           | PFR scraper    |
| 2022–2025 | RotoWire backfill  | PFR scraper    |
| 2026+     | RotoWire live      | PFR (weekly)   |

### Open questions / deferred items
- `flask load-history` CLI command not yet built (loads flat files → DB)
- `/history` and `/players/<name>` web routes not yet built
- RotoWire 2022–2025 salary backfill requires manual slate ID discovery
- Player ID mapping table (rg_id → pfr_id) not yet built

### Next proposed step (Step 4b)
- Add `hist_dfs_salaries` and `hist_player_stats` tables to DB schema
- Add `flask load-history` CLI command to bulk-load the CSV.gz files
- Build `/history` route: filterable table by year/week/position
- Build `/players/<name>` route: player career page with salary + stat history
