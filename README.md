# Historical Data Scrapers

Two standalone scripts that produce the flat files loaded into the DB.
Run these locally — they are slow by design (polite rate limits) and only
need to be run once per year at season's end.

---

## 1. RotoGuru — DFS Salaries + Actual Points (2014–2021)

```bash
python scrapers/scrape_rotoguru.py
```

* Output: `data/rotoguru_dk_2014_2021.csv.gz`
* Runtime: ~10 minutes (180 pages × 2s sleep)
* Resume-safe: skips (year, week) pairs already in the file
* Columns: week, year, rg_id, name, name_normalized, position,
  team, home_away, opponent, dk_pts_scored, dk_salary

## 2. PFR — Player Weekly Stats + Game Info (2014–2025)

```bash
# Full run (takes ~8-10 hours overnight)
python scrapers/scrape_pfr.py --years 2014-2025

# Just one year (e.g. after 2025 season ends)
python scrapers/scrape_pfr.py --years 2025

# Only game info (weather/Vegas), skip players
python scrapers/scrape_pfr.py --years 2014-2025 --skip-players

# Only players, skip game info
python scrapers/scrape_pfr.py --years 2014-2025 --skip-games
```

* Outputs:
  * `data/pfr_player_stats_2014_2025.csv.gz`
  * `data/pfr_game_info_2014_2025.csv.gz`
* Runtime: ~8-10 hours for full 2014-2025 run
* Resume-safe: checkpoints every 20 players, skips already-done (pfr_id, year) pairs
* Player columns: pfr_id, name, name_normalized, year, week, team, opponent,
  home_away, position, dk_pts,
  pass_cmp, pass_att, pass_yds, pass_td, pass_int,
  rush_att, rush_yds, rush_td,
  rec_tgt, rec, rec_yds, rec_td, snap_pct
* Game columns: boxscore_url, year, week, team_home, team_away,
  roof, surface, weather, attendance, vegas_line, over_under

Also available for historical DST stat categories (sacks, INTs, fumble
recoveries — anything FantasyPros' current-season-only page can't
provide for past years): `scrapers/scrape_pfr_defense_historical.py`,
aggregated from per-game boxscores rather than a season page. Resume-safe
(skips games where both teams' data is already present) — see the file's
own docstring for usage, since it's a much larger scrape (one request per
game, not per year) and typically needs multiple sessions with cookie
refreshes to complete a full historical backfill.

---

## 3. Load into DB

After the files are generated:

```bash
flask load-history
```

This reads every recognized `.csv.gz` file in `data/` and bulk-inserts
into the corresponding DB table (salaries, player stats, DST, weather,
game info, depth charts, fantasy-points-against, team points — whichever
files are present; missing ones are skipped with a note, not an error).

Useful scoping flags for when you only need part of this:
* `--salaries-only` — just salary files
* `--stats-only` — just player stats
* `--weather-only` — just the weather file, for the frequent re-runs
  you'll want as forecasts firm up or actual conditions come in, without
  waiting on the much slower full load

---

## Data coverage by source

| Years | Salaries + DK pts | Detailed stats |
|---|---|---|
| 2014–2021 | RotoGuru | PFR |
| 2022–2025 | RotoWire backfill* | PFR |
| 2026+ | FantasyPros (live) | PFR (weekly) |

\*RotoWire backfill for 2022-2025 requires finding old slate IDs manually
and running `flask ingest-slate --week N --year Y --slate-id XXXXX`.
These get stored in the regular `players` table and joined at query time.

---

## Join key strategy

RotoGuru uses its own GID (e.g., `5536` = McCaffrey).
PFR uses a slug (e.g., `McCAC00`).

The `name_normalized` column (lowercase, no punctuation, first + last) is
the join key between the two datasets. It works for ~95% of players.
Edge cases (Jr./Sr., name changes, DST teams) are handled at query time.

---

## 4. Weekly Workflow

### The easy way

Two scripts consolidate everything below into one command each:

```cmd
weekly_before.bat 2026 3
weekly_after.bat 2026 3
```

(year=2026, week=3 — adjust each week). These stop early on a failure
in a step everything downstream depends on (e.g. the salary scrape, or
player stats/team points before DST scoring), and otherwise warn and
keep going. Publishing props stays a manual final step either way —
see below — since that's not something to auto-publish without a
glance first.

The rest of this section is the detailed breakdown of what those
scripts actually do, useful for understanding what happened, or for
running an individual step by hand if something needs a rerun.

### Beginning of week (before games start)

```bash
# 1. Update the schedule — picks up newly-finalized kickoff times
#    (early in the season many are still TBD; flex scheduling updates
#    happen throughout the season too)
python scrapers/scrape_schedules.py --years 2026
flask load-schedule --year 2026

# 2. Scrape this week's DraftKings salaries
python scrapers/scrape_fp_dk_salaries.py --week N --year 2026
flask load-weekly-salary data/fp_dk_salaries_weekN_2026.csv.gz --year 2026

# 3. Scrape this week's weather forecasts (re-run again closer to
#    kickoff too — forecasts only populate ~1 week out; use
#    --weather-only on the load-history step below for these reruns
#    so you're not waiting on a full load every time)
python scrapers/scrape_weekly_weather.py --year 2026 --weeks N

# 4. Scrape depth charts (updates weekly — who's starting, who's
#    been dropped, injury-related depth chart moves)
python scrapers/scrape_ourlads_depth_charts.py

# 5. Scrape season-long Vegas props (or weekly, once FirstDown Studio
#    turns that on)
python scrapers/scrape_firstdown_studio.py --position all

# 6. Prop Bet Challenge — scrape candidate props, generate the
#    weekly slate, review it, then publish
python scrapers/scrape_scoresandodds_props.py --all --combine
python scrapers/convert_scoresandodds_to_props_csv.py --output data/props_candidates_weekN_2026.csv
python scrapers/select_top_props_by_category.py --input data/props_candidates_weekN_2026.csv --output data/props_weekN_2026.csv --top-n 6
# --- review data/props_weekN_2026.csv before the next line ---
flask add-props data/props_weekN_2026.csv --year 2026 --week N

# 7. Persist everything into the database
flask load-history
```

### End of week (after all games have played)

```bash
# 1. Player stats — the real results
python scrapers/scrape_pfr.py --years 2026

# 2. Team points scored/allowed (needed for DST scoring)
python scrapers/scrape_team_points.py --years 2026

# 3. DST fantasy stat categories (sacks, INTs, etc.) — current season
#    only; combine_dst_scoring.py automatically falls back to the
#    historical PFR-boxscore source for any year this isn't available
python scrapers/scrape_dst_fantasy_stats.py --year 2026 --weeks N

# 4. Combine into final DK-accurate DST scores
python scrapers/combine_dst_scoring.py --year 2026

# 5. Game info — roof, surface, actual weather, vegas lines, attendance
python scrapers/scrape_pfr.py --years 2026 --skip-players   # game info portion

# 6. Weather — re-scrape to get "Final" status + actual conditions
python scrapers/scrape_weekly_weather.py --year 2026 --weeks N

# 7. Persist everything — Standings, History, and My Lineups all
#    auto-recompute once this is loaded; prop picks auto-score too
flask load-history
```

### Separately: market analysis / arbitrage research

Not part of the core weekly operations above — this is personal
betting research using the same props data, documented in
`scoresandodds_workflow.md`. Its first step (`scrape_scoresandodds_props.py
--all --combine`) is the same command as step 6 above, so if you've
already run the weekly prep, that file's already fresh and you can
skip straight to that workflow's Step 2.

---

## Future Ideas — Not Yet Implemented

Notes on data sources worth revisiting later, not committed to yet.

### FirstDown Studio weekly implied points
Currently only scraping season-long FirstDown Studio data
(`scrape_firstdown_studio.py`). Once the season is underway, FirstDown
Studio may also expose week-by-week implied point totals per player —
worth checking whether that's live yet, and if so, whether it's worth
a standalone tab or folding into the existing Implied Points page
alongside the scoresandodds-derived numbers.

### FantasyPros projections
https://www.fantasypros.com/nfl/projections/qb.php (and the equivalent
pages for RB/WR/TE/etc.) has its own weekly fantasy point projections
per player — a genuinely different methodology from the prop-market-
derived Implied Points page (FantasyPros is an analyst projection, not
a betting-market-implied number). Could be scraped as its own column
or its own comparison page once there's a real need to see the two
side by side. Not investigated yet — unknown whether the page is
easily scrapeable (no diagnostic run against it so far).
