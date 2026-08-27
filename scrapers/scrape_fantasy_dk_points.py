"""
scrapers/scrape_fantasy_dk_points.py
--------------------------------------
Fills in ground-truth dk_pts for years the old file doesn't cover
(currently: 2025) by scraping the PFR "fantasy" page's own pre-computed
draftkings_points field — the same source pfr_player_stats_2014_2025_old
was originally built from.

We only need ONE field from this page now: draftkings_points. Everything
else useful (real pass_yds/rush_yds/TDs/etc.) already comes from the
career gamelog page via scrape_pfr.py — this page's OTHER stat columns
are redzone-limited splits (confirmed earlier), which is why we stopped
using it as the primary source. But draftkings_points itself was always
accurate, which is exactly why it's still useful here as a ground-truth
reference column.

Output: appends new (pfr_id, year, week, dk_pts) rows to the existing
"_old" file (default: data/pfr_player_stats_2014_2025_old.csv.gz),
de-duplicated so re-running is safe.

Usage:
    python scrapers/scrape_fantasy_dk_points.py
    python scrapers/scrape_fantasy_dk_points.py --years 2025
    python scrapers/scrape_fantasy_dk_points.py --years 2025-2026
"""

import os
import sys
import time
import argparse
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from scrape_pfr import (
    make_session, pfr_get, find_table, get_players_for_year,
    load_date_to_week, normalize_name, PFR_BASE, _int, _float,
)

DATA_DIR      = os.path.join(os.path.dirname(__file__), '..', 'data')
DEFAULT_OLD   = os.path.join(DATA_DIR, 'pfr_player_stats_2014_2025_old.csv.gz')
SLEEP_PLAYER  = 4.0
FAILURE_CIRCUIT_BREAKER = 5

OUT_COLUMNS = ['pfr_id', 'name', 'name_normalized', 'year', 'week',
               'team', 'position', 'dk_pts']


def get_fantasy_dk_points(pfr_id: str, name: str, player_url: str, position: str,
                          year: int, date_to_week: dict,
                          session) -> list[dict] | None:
    """
    Hit the player's /fantasy/{year}/ page and extract ONLY
    draftkings_points per week — nothing else needed from this page.
    Returns None on request failure (for circuit breaker), [] if no
    usable rows found.
    """
    import re
    url = PFR_BASE + re.sub(r'\.htm$', '', player_url) + f'/fantasy/{year}/'
    r   = pfr_get(session, url, use_headers=True)

    if r is None:
        return None

    soup  = BeautifulSoup(r.content, 'lxml')
    table = find_table(soup, 'player_fantasy')
    if not table:
        return []

    tbody = table.find('tbody')
    if not tbody:
        return []

    rows = []
    for row in tbody.find_all('tr'):
        if row.get('class') and 'thead' in row.get('class'):
            continue

        def get(stat):
            cell = row.find('td', attrs={'data-stat': stat})
            return cell.get_text(strip=True) if cell else ''

        try:
            game_date = get('game_date')
            if not game_date:
                continue

            # Use the season year (the `year` param, from the /fantasy/{year}/
            # URL) rather than the calendar year parsed from game_date.
            # Week 18 games often fall in January of the following calendar
            # year (e.g. a 2025-season game played 2026-01-04) but still
            # belong to the 2025 season — this is what we want stored.
            week_num = date_to_week.get(game_date[:10], 0)
            if week_num == 0:
                continue

            dk_pts = _float(get('draftkings_points'))
            team   = get('team')

            rows.append({
                'pfr_id':          pfr_id,
                'name':            name,
                'name_normalized': normalize_name(name),
                'year':            year,
                'week':            week_num,
                'team':            team.lower(),
                'position':        position.upper(),
                'dk_pts':          dk_pts,
            })
        except Exception:
            continue

    return rows


def _save(old_path: str, existing: pd.DataFrame, new_rows: list):
    if not new_rows:
        return existing
    new_df = pd.DataFrame(new_rows, columns=OUT_COLUMNS)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=['pfr_id', 'year', 'week'], keep='last')
    combined = combined.sort_values(['year', 'week', 'pfr_id'])
    combined.to_csv(old_path, index=False, compression='gzip')
    return combined


def main(years: list[int], old_path: str):
    os.makedirs(os.path.dirname(old_path), exist_ok=True)

    if os.path.exists(old_path):
        existing = pd.read_csv(old_path)
        # Normalize to the columns we care about if the file has more/fewer
        for col in OUT_COLUMNS:
            if col not in existing.columns:
                existing[col] = None
        done_pairs = set(zip(existing['pfr_id'].astype(str), existing['year'].astype(int)))
        print(f"Loaded existing old file: {len(existing):,} rows")
        print(f"  {len(done_pairs)} (pfr_id, year) pairs already present")
    else:
        existing = pd.DataFrame(columns=OUT_COLUMNS)
        done_pairs = set()
        print(f"No existing old file found at {old_path} — starting fresh")

    all_new_rows = []
    consecutive_failures = 0

    with make_session() as session:
        for year in years:
            print(f"\n{'='*50}")
            print(f"YEAR {year}")
            print(f"{'='*50}")

            players_df = get_players_for_year(year, session)
            time.sleep(SLEEP_PLAYER)

            if players_df.empty:
                print(f"  No players found for {year}, skipping")
                continue

            before = len(players_df)
            players_df = players_df[players_df['dk_pts_season'] > 0].reset_index(drop=True)
            skipped = before - len(players_df)
            if skipped:
                print(f"  Skipping {skipped} players with 0 season DK points — "
                      f"{len(players_df)} remain")

            date_to_week = load_date_to_week(year)

            for i, row in players_df.iterrows():
                pfr_id     = row['pfr_id']
                name       = row['name']
                player_url = row['player_url']
                position   = row['position']

                if (pfr_id, year) in done_pairs:
                    print(f"  Skip {name} {year} (already have this year)")
                    continue

                print(f"  [{i+1}/{len(players_df)}] {name} ({pfr_id}) {year}",
                      end='', flush=True)

                week_rows = get_fantasy_dk_points(
                    pfr_id, name, player_url, position, year, date_to_week, session
                )

                if week_rows is None:
                    consecutive_failures += 1
                    print(f" → FAILED ({consecutive_failures}/{FAILURE_CIRCUIT_BREAKER} consecutive)")

                    if consecutive_failures >= FAILURE_CIRCUIT_BREAKER:
                        print(f"\n{'!'*60}")
                        print(f"STOPPING: {FAILURE_CIRCUIT_BREAKER} consecutive requests failed.")
                        print(f"Your PFR_CF_CLEARANCE cookie has almost certainly expired.")
                        print(f"Refresh it, update .env, then re-run this exact command —")
                        print(f"it resumes from {name} onward, nothing is lost.")
                        print(f"{'!'*60}\n")
                        _save(old_path, existing, all_new_rows)
                        sys.exit(1)

                    time.sleep(SLEEP_PLAYER)
                    continue

                consecutive_failures = 0
                all_new_rows.extend(week_rows)
                done_pairs.add((pfr_id, year))

                existing = _save(old_path, existing, all_new_rows)
                all_new_rows = []   # flushed into existing, avoid re-saving repeatedly
                print(f" → {len(week_rows)} weeks [saved to {os.path.basename(old_path)}]")

                time.sleep(SLEEP_PLAYER)

    print(f"\nDone. Final file: {len(existing):,} rows → {old_path}")


def parse_years(s: str) -> list[int]:
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    elif ',' in s:
        return [int(y) for y in s.split(',')]
    else:
        return [int(s)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--years', default='2025',
                        help='Year(s) to fill in, e.g. "2025" or "2025-2026"')
    parser.add_argument('--old-file', default=DEFAULT_OLD,
                        help=f'Path to the old file to append to (default: {DEFAULT_OLD})')
    args = parser.parse_args()

    years = parse_years(args.years)
    print(f"Filling in ground-truth dk_pts for: {years}")
    print(f"Target file: {args.old_file}\n")

    main(years, args.old_file)
