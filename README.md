# Historical Data Scrapers

Two standalone scripts that produce the flat files loaded into the DB.
Run these locally — they are slow by design (polite rate limits) and only
need to be run once per year at season's end.

\---

## 1\. RotoGuru — DFS Salaries + Actual Points (2014–2021)

```bash
python scrapers/scrape\\\_rotoguru.py
```

* Output: `data/rotoguru\\\_dk\\\_2014\\\_2021.csv.gz`
* Runtime: \~10 minutes (180 pages × 2s sleep)
* Resume-safe: skips (year, week) pairs already in the file
* Columns: week, year, rg\_id, name, name\_normalized, position,
team, home\_away, opponent, dk\_pts\_scored, dk\_salary

## 2\. PFR — Player Weekly Stats + Game Info (2014–2025)

```bash
# Full run (takes \\\~8-10 hours overnight)
python scrapers/scrape\\\_pfr.py --years 2014-2025

# Just one year (e.g. after 2025 season ends)
python scrapers/scrape\\\_pfr.py --years 2025

# Only game info (weather/Vegas), skip players
python scrapers/scrape\\\_pfr.py --years 2014-2025 --skip-players

# Only players, skip game info
python scrapers/scrape\\\_pfr.py --years 2014-2025 --skip-games
```

* Outputs:

  * `data/pfr\\\_player\\\_stats\\\_2014\\\_2025.csv.gz`
  * `data/pfr\\\_game\\\_info\\\_2014\\\_2025.csv.gz`
* Runtime: \~8-10 hours for full 2014-2025 run
* Resume-safe: checkpoints every 20 players, skips already-done (pfr\_id, year) pairs
* Player columns: pfr\_id, name, name\_normalized, year, week, team, opponent,
home\_away, position, dk\_pts,
pass\_cmp, pass\_att, pass\_yds, pass\_td, pass\_int,
rush\_att, rush\_yds, rush\_td,
rec\_tgt, rec, rec\_yds, rec\_td, snap\_pct
* Game columns: boxscore\_url, year, week, team\_home, team\_away,
roof, surface, weather, attendance, vegas\_line, over\_under

\---

## 3\. Load into DB

After the files are generated:

```bash
flask load-history
```

This reads both parquet files and bulk-inserts into the DB tables
`hist\\\_dfs\\\_salaries` and `hist\\\_player\\\_stats`.

\---

## Data coverage by source

|Years|Salaries + DK pts|Detailed stats|
|-|-|-|
|2014–2021|RotoGuru|PFR|
|2022–2025|RotoWire backfill\*|PFR|
|2026+|RotoWire live|PFR (weekly)|

\*RotoWire backfill for 2022-2025 requires finding old slate IDs manually
and running `flask ingest-slate --week N --year Y --slate-id XXXXX`.
These get stored in the regular `players` table and joined at query time.

\---

## Join key strategy

RotoGuru uses its own GID (e.g., `5536` = McCaffrey).
PFR uses a slug (e.g., `McCAC00`).

The `name\\\_normalized` column (lowercase, no punctuation, first + last) is
the join key between the two datasets. It works for \~95% of players.
Edge cases (Jr./Sr., name changes, DST teams) are handled at query time.



\---

## 4\. Weekly Workflow

Beginning of week (before games start):

\# 1. Update the schedule — picks up newly-finalized kickoff times

\#    (early in the season many are still TBD; flex scheduling updates

\#    happen throughout the season too)

```bash

python scrapers/scrape\_schedules.py --years 2026

flask load-schedule --year 2026



\# 2. Scrape this week's DraftKings salaries

python scrapers/scrape\_fp\_dk\_salaries.py --week N --year 2026

flask load-weekly-salary data/fp\_dk\_salaries\_weekN\_2026.csv.gz --year 2026



\# 3. Scrape this week's weather forecasts (re-run again closer to

\#    kickoff too — forecasts only populate \~1 week out)

python scrapers/scrape\_weekly\_weather.py --year 2026 --weeks N



\# 4. Scrape season-long Vegas props (or weekly, once FirstDown Studio

\#    turns that on)

python scrapers/scrape\_firstdown\_studio.py --position all



\# 5. Persist everything into the database

flask load-history---

```



End of week (after all games have played):

```bash

\# 1. Player stats — the real results

python scrapers/scrape\_pfr.py --years 2026



\# 2. Team points scored/allowed (needed for DST scoring)

python scrapers/scrape\_team\_points.py --years 2026



\# 3. DST fantasy stat categories (sacks, INTs, etc.)

python scrapers/scrape\_dst\_fantasy\_stats.py --year 2026 --weeks N



\# 4. Combine into final DK-accurate DST scores

python scrapers/combine\_dst\_scoring.py --year 2026



\# 5. Game info — roof, surface, actual weather, vegas lines, attendance

python scrapers/scrape\_pfr.py --years 2026 --skip-players   # game info portion



\# 6. Weather — re-scrape to get "Final" status + actual conditions

python scrapers/scrape\_weekly\_weather.py --year 2026 --weeks N



\# 7. Persist everything — Standings auto-recomputes once this is loaded

flask load-history

```

