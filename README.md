# Historical Data Scrapers

Two standalone scripts that produce the flat files loaded into the DB.
Run these locally — they are slow by design (polite rate limits) and only
need to be run once per year at season's end.

---

## 1. RotoGuru — DFS Salaries + Actual Points (2014–2021)

```bash
python scrapers/scrape_rotoguru.py
```

- Output: `data/rotoguru_dk_2014_2021.csv.gz`
- Runtime: ~10 minutes (180 pages × 2s sleep)
- Resume-safe: skips (year, week) pairs already in the file
- Columns: week, year, rg_id, name, name_normalized, position,
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

- Outputs:
  - `data/pfr_player_stats_2014_2025.csv.gz`
  - `data/pfr_game_info_2014_2025.csv.gz`
- Runtime: ~8-10 hours for full 2014-2025 run
- Resume-safe: checkpoints every 20 players, skips already-done (pfr_id, year) pairs
- Player columns: pfr_id, name, name_normalized, year, week, team, opponent,
                  home_away, position, dk_pts,
                  pass_cmp, pass_att, pass_yds, pass_td, pass_int,
                  rush_att, rush_yds, rush_td,
                  rec_tgt, rec, rec_yds, rec_td, snap_pct
- Game columns: boxscore_url, year, week, team_home, team_away,
                roof, surface, weather, attendance, vegas_line, over_under

---

## 3. Load into DB

After the files are generated:

```bash
flask load-history
```

This reads both parquet files and bulk-inserts into the DB tables
`hist_dfs_salaries` and `hist_player_stats`.

---

## Data coverage by source

| Years     | Salaries + DK pts | Detailed stats |
|-----------|-------------------|----------------|
| 2014–2021 | RotoGuru          | PFR            |
| 2022–2025 | RotoWire backfill*| PFR            |
| 2026+     | RotoWire live     | PFR (weekly)   |

*RotoWire backfill for 2022-2025 requires finding old slate IDs manually
 and running `flask ingest-slate --week N --year Y --slate-id XXXXX`.
 These get stored in the regular `players` table and joined at query time.

---

## Join key strategy

RotoGuru uses its own GID (e.g., `5536` = McCaffrey).
PFR uses a slug (e.g., `McCAC00`).

The `name_normalized` column (lowercase, no punctuation, first + last) is
the join key between the two datasets. It works for ~95% of players.
Edge cases (Jr./Sr., name changes, DST teams) are handled at query time.
