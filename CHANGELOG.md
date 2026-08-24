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
