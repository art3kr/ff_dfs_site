# CHANGELOG — FF DFS Challenge Site

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
