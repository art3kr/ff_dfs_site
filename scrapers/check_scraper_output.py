"""
scrapers/check_scraper_output.py
----------------------------------
Sanity-checks the files the scrapers just wrote, before load-history
puts them in the database. Catches the failure mode where a scraper
exits 0 but produced nothing useful, which is invisible in the weekly
run's output.

Two real cases this would have caught (both 2026-09-15):
  - ourlads_injuries.csv.gz had been header-only for at least a week.
    The class the scraper looked for (lc_red) had vanished from the
    page, so every scrape "succeeded" with zero injuries.
  - firstdown_studio_rankings.csv.gz had a `team` column that was empty
    on every row, because the scraper was reading the avatar's initials
    span instead of the matchup span.

So each file is checked three ways: it exists, it has at least a
plausible number of rows, and its key columns aren't entirely blank. A
freshness check (was it actually rewritten by this run?) is applied to
the live/current-week files.

Warnings don't stop the weekly run — the point is to make the failure
visible, not to block a load that's still mostly good.

Usage:
    python scrapers/check_scraper_output.py --year 2026 --week 2
    python scrapers/check_scraper_output.py --year 2026 --week 2 --max-age-hours 6
"""

import argparse
import os
import time

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# (filename pattern, min rows, key columns that must not be all-blank, check freshness)
CHECKS = [
    ("ourlads_depth_charts.csv.gz",        300, ["player_name", "team", "pos"],              True),
    ("ourlads_injuries.csv.gz",              1, ["player_name", "status"],                   True),
    ("draftedge_injuries.csv.gz",           20, ["player_name", "status"],                   True),
    ("scoresandodds_game_odds.csv.gz",      20, ["team", "spread", "over_under"],            True),
    ("scoresandodds_props_all.csv.gz",     200, ["player_name", "team", "category"],         True),
    ("firstdown_studio_rankings.csv.gz",    50, ["player_name", "team", "pts"],              True),
    ("fp_dk_salaries_week{week}_{year}.csv.gz", 300, ["name", "team", "dk_salary"],          True),
    ("weekly_weather.csv.gz",               10, ["away_team", "home_team"],                  True),
    ("nflverse_usage_{year}.csv.gz",       100, ["name", "team", "offense_snaps"],           False),
    ("team_points_by_week.csv.gz",         100, ["team", "points_scored"],                   False),
    ("hist_dst_stats.csv.gz",              100, ["team", "dk_pts"],                          False),
    ("fantasy_points_against_{year}.csv.gz", 50, ["team", "dk_pts_per_game"],                False),
    ("pfr_player_stats_2014_2025.csv.gz", 1000, ["name", "team", "dk_pts"],                  False),
    ("pfr_game_info_2014_2025.csv.gz",     100, ["boxscore_url", "team_home"],               False),
]


def check_file(path: str, min_rows: int, key_columns: list, check_fresh: bool,
               max_age_hours: float) -> list:
    """Returns a list of warning strings (empty when the file looks fine)."""
    name = os.path.basename(path)
    if not os.path.exists(path):
        return [f"{name}: MISSING"]

    warnings = []
    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    try:
        df = pd.read_csv(path)
    except Exception as e:
        return [f"{name}: unreadable ({e})"]

    if len(df) < min_rows:
        warnings.append(f"{name}: {len(df)} rows, expected at least {min_rows}")

    for col in key_columns:
        if col not in df.columns:
            warnings.append(f"{name}: column '{col}' is missing")
        elif len(df) and df[col].isna().all():
            warnings.append(f"{name}: column '{col}' is empty on every row")

    if check_fresh and max_age_hours and age_hours > max_age_hours:
        warnings.append(f"{name}: {age_hours:.0f}h old, this run may not have rewritten it")

    if not warnings:
        print(f"  OK    {name:<42} {len(df):>7,} rows, {age_hours:>4.0f}h old")
    return warnings


def main(data_dir: str, year: int, week: int, max_age_hours: float, only: str = None):
    print(f"Checking scraper output in {os.path.abspath(data_dir)}")
    # --only limits the run to the files a particular job actually scrapes.
    # The cloud refresh (.github/workflows/refresh-live-data.yml) never
    # touches PFR stats, salaries, weather or usage, and those files aren't
    # in git, so on a fresh runner every one of them reads as MISSING and
    # buries the warnings that matter.
    wanted = [w.strip() for w in only.split(',')] if only else None
    checks = CHECKS
    if wanted:
        checks = [c for c in CHECKS if any(w in c[0] for w in wanted)]
        unmatched = [w for w in wanted if not any(w in c[0] for c in CHECKS)]
        if unmatched:
            print(f"  (nothing in CHECKS matches: {', '.join(unmatched)})")

    all_warnings = []
    for pattern, min_rows, key_columns, check_fresh in checks:
        filename = pattern.format(year=year, week=week)
        path = os.path.join(data_dir, filename)
        all_warnings += check_file(path, min_rows, key_columns, check_fresh, max_age_hours)

    print()
    if all_warnings:
        print(f"{len(all_warnings)} WARNING(S) — a scraper may have silently produced nothing useful:")
        for w in all_warnings:
            print(f"  WARN  {w}")
        print("\nCheck the scraper for that file before trusting the pages it feeds.")
        return 1

    print(f"All {len(checks)} files look fine.")
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', default=DATA_DIR)
    ap.add_argument('--year', type=int, required=True)
    ap.add_argument('--week', type=int, required=True)
    ap.add_argument('--max-age-hours', type=float, default=36,
                    help='Warn when a live file is older than this (0 disables the check).')
    ap.add_argument('--only', default=None,
                    help='Comma-separated substrings; only matching files are checked. '
                         'Use it when a job scrapes a subset, e.g. '
                         '--only ourlads,draftedge,scoresandodds')
    args = ap.parse_args()
    raise SystemExit(main(args.data_dir, args.year, args.week, args.max_age_hours, args.only))
