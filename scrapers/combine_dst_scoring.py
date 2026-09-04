"""
scrapers/combine_dst_scoring.py
----------------------------------
Merges DST fantasy stats with team points-allowed data
(scrape_team_points.py) and computes DraftKings' actual DST scoring
formula — including the points-allowed bracket, which neither source
alone provides.

DST stat category source is auto-detected per year:
  - scrape_dst_fantasy_stats.py (FantasyPros) — current season only,
    used automatically when data/dst_fantasy_stats_{year}.csv.gz exists
  - scrape_pfr_defense_historical.py — any year, aggregated from
    per-game PFR boxscores, used automatically as a fallback when the
    FantasyPros file for that year doesn't exist
Both share the identical output schema, so this script works the same
way regardless of which one is actually feeding it. Override with
--dst-stats-file to force a specific file instead of auto-detecting.

DraftKings NFL Classic DST scoring:
    Sack:                    +1
    Interception:            +2
    Fumble Recovery:         +2
    Defensive/Return TD:     +6   (def_td + special_teams_td combined)
    Safety:                  +2
    Points allowed brackets:
        0 pts        -> +10
        1-6 pts       -> +7
        7-13 pts      -> +4
        14-20 pts     -> +1
        21-27 pts     -> 0
        28-34 pts     -> -1
        35+ pts       -> -4
(Forced fumbles don't score points on their own in DK's system — only
the recovery does. We still store forced_fumble as informational data.)

Output: data/hist_dst_stats.csv.gz
  Columns: year, week, team, opponent, points_allowed, dk_pts,
           sack, interception, fumble_rec, forced_fumble,
           def_td, safety, special_teams_td

Usage:
    python scrapers/combine_dst_scoring.py --year 2026
"""

import os
import argparse
import pandas as pd

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
POINTS_FILE = os.path.join(DATA_DIR, 'team_points_by_week.csv.gz')
OUTPUT_FILE = os.path.join(DATA_DIR, 'hist_dst_stats.csv.gz')
HISTORICAL_FILE = os.path.join(DATA_DIR, 'hist_dst_stats_historical.csv.gz')

OUT_COLUMNS = ['year', 'week', 'team', 'opponent', 'points_allowed', 'dk_pts',
               'sack', 'interception', 'fumble_rec', 'forced_fumble',
               'def_td', 'safety', 'special_teams_td']


def find_dst_stats_file(year: int, dst_stats_file_arg: str | None) -> tuple[str | None, str]:
    """
    Two possible DST stat category sources, same output schema:
      - scrape_dst_fantasy_stats.py (FantasyPros) — CURRENT SEASON ONLY,
        one file per year: data/dst_fantasy_stats_{year}.csv.gz
      - scrape_pfr_defense_historical.py — ANY year, aggregated from
        per-game PFR boxscores, ALL years combined into one file:
        data/hist_dst_stats_historical.csv.gz (needs filtering by year)

    If --dst-stats-file was given explicitly, use it as-is (caller's
    choice, no auto-detection). Otherwise try the FantasyPros file
    first (it's the more complete/direct source when available — real
    per-week category data straight from FantasyPros' own page), then
    fall back to the historical aggregation.
    """
    if dst_stats_file_arg:
        return dst_stats_file_arg, 'explicit (--dst-stats-file)'

    fp_path = os.path.join(DATA_DIR, f'dst_fantasy_stats_{year}.csv.gz')
    if os.path.exists(fp_path):
        return fp_path, 'FantasyPros (current season)'

    if os.path.exists(HISTORICAL_FILE):
        return HISTORICAL_FILE, 'historical (aggregated from PFR boxscores)'

    return None, 'none'


def points_allowed_bonus(points_allowed: int) -> float:
    if points_allowed == 0:
        return 10.0
    elif points_allowed <= 6:
        return 7.0
    elif points_allowed <= 13:
        return 4.0
    elif points_allowed <= 20:
        return 1.0
    elif points_allowed <= 27:
        return 0.0
    elif points_allowed <= 34:
        return -1.0
    else:
        return -4.0


def calculate_dst_dk_points(sack=0, interception=0, fumble_rec=0,
                            def_td=0, safety=0, special_teams_td=0,
                            points_allowed=0) -> float:
    pts = 0.0
    pts += sack * 1
    pts += interception * 2
    pts += fumble_rec * 2
    pts += (def_td + special_teams_td) * 6
    pts += safety * 2
    pts += points_allowed_bonus(points_allowed)
    return round(pts, 2)


def main(year: int, dst_stats_file_arg: str | None):
    dst_stats_file, source_label = find_dst_stats_file(year, dst_stats_file_arg)

    if dst_stats_file is None:
        print(f"ERROR: no DST stats file found for {year}.")
        print(f"  Tried: data/dst_fantasy_stats_{year}.csv.gz (FantasyPros, current season)")
        print(f"  Tried: {HISTORICAL_FILE} (historical)")
        print(f"Run scrape_dst_fantasy_stats.py (current season) or")
        print(f"scrape_pfr_defense_historical.py (historical) first.")
        return
    if not os.path.exists(dst_stats_file):
        print(f"ERROR: DST fantasy stats file not found: {dst_stats_file}")
        return
    if not os.path.exists(POINTS_FILE):
        print(f"ERROR: points-allowed file not found: {POINTS_FILE}")
        print(f"Run scrape_team_points.py first.")
        return

    print(f"DST stats source: {source_label}")
    print(f"  ({dst_stats_file})")

    dst_stats = pd.read_csv(dst_stats_file)
    points    = pd.read_csv(POINTS_FILE)
    points    = points[points['year'] == year]

    # The historical file has ALL years combined — filter to just the
    # requested one. Harmless no-op for the FantasyPros file, which is
    # already single-year, but doing this unconditionally means we
    # don't need to know which source we're dealing with here.
    if 'year' in dst_stats.columns:
        before = len(dst_stats)
        dst_stats = dst_stats[dst_stats['year'] == year]
        if len(dst_stats) < before:
            print(f"  Filtered to {year}: {len(dst_stats):,} of {before:,} rows")

    print(f"\nDST fantasy stat rows for {year}: {len(dst_stats):,}")
    print(f"Points-allowed rows for {year}:   {len(points):,}")

    merged = dst_stats.merge(
        points[['year', 'week', 'team', 'opponent', 'points_allowed']],
        on=['year', 'week', 'team'], how='left'
    )

    missing_points = merged['points_allowed'].isna().sum()
    if missing_points:
        print(f"\nWARNING: {missing_points} rows have no matching points-allowed "
              f"data (game not yet played, or points-allowed scraper hasn't "
              f"been run for {year} yet). These rows will be dropped rather "
              f"than scored with a missing/wrong points-allowed bracket.")
    merged = merged.dropna(subset=['points_allowed'])
    merged['points_allowed'] = merged['points_allowed'].astype(int)

    merged['dk_pts'] = merged.apply(lambda r: calculate_dst_dk_points(
        sack=r['sack'], interception=r['interception'], fumble_rec=r['fumble_rec'],
        def_td=r['def_td'], safety=r['safety'], special_teams_td=r['special_teams_td'],
        points_allowed=r['points_allowed'],
    ), axis=1)

    final = merged[OUT_COLUMNS].sort_values(['year', 'week', 'team'])

    # Merge into the combined output file (may already have other years)
    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        existing = existing[existing['year'] != year]   # replace this year's data
        final = pd.concat([existing, final], ignore_index=True)
        final = final.sort_values(['year', 'week', 'team'])

    final.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    print(f"\nSaved {len(final):,} total rows → {OUTPUT_FILE}")

    sample = final[final['year'] == year].head(5)
    print(f"\nSample rows for {year}:")
    print(sample.to_string(index=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--dst-stats-file', default=None,
                        help='Explicit override. Default: auto-detect — tries '
                             'data/dst_fantasy_stats_{year}.csv.gz (FantasyPros, '
                             'current season) first, falls back to '
                             'data/hist_dst_stats_historical.csv.gz (historical, '
                             'any year) if that does not exist.')
    args = parser.parse_args()

    main(args.year, args.dst_stats_file)
