"""
scrapers/scrape_dst_fantasy_stats.py
---------------------------------------
Scrapes weekly DST fantasy stat categories from FantasyPros — sacks,
interceptions, fumble recoveries, defensive TDs, safeties, and special
teams TDs — for the CURRENT season only.

IMPORTANT LIMITATION: FantasyPros' year selector is client-side
JavaScript, not a URL parameter — passing &year=2023 was tested and
confirmed to have no effect; the page always returns the current
season regardless. This means historical years (2014-2024) can't be
backfilled from this source. Going forward from the current season,
this is fine.

We do NOT use FantasyPros' own FPTS column — it's their own generic
scoring system, not DraftKings' specific formula (notably missing any
points-allowed bracket, which is one of DK's biggest DST scoring
components). Instead we capture the raw stat categories here and
combine them with points-allowed data (scrape_team_points.py) in
combine_dst_scoring.py, applying DK's actual formula there.

Team names are normalized via team_mapping.py (FantasyPros uses
ESPN-style abbreviations like 'KC'/'NE', which differ from the
PFR-style 'kan'/'nwe' abbreviations used everywhere else in this
project).

Output: data/dst_fantasy_stats_{year}.csv.gz
  Columns: year, week, team, sack, interception, fumble_rec,
           forced_fumble, def_td, safety, special_teams_td, games

Usage:
    python scrapers/scrape_dst_fantasy_stats.py --year 2026
    python scrapers/scrape_dst_fantasy_stats.py --year 2026 --weeks 1-5
"""

import os
import re
import sys
import time
import argparse
import requests
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR  = os.path.join(os.path.dirname(__file__), '..', 'data')
BASE_URL  = "https://www.fantasypros.com/nfl/stats/dst.php"
SLEEP_SEC = 2.0

OUT_COLUMNS = ['year', 'week', 'team', 'sack', 'interception', 'fumble_rec',
               'forced_fumble', 'def_td', 'safety', 'special_teams_td', 'games']

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}


def _int(s) -> int:
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return 0


def fetch_week(week: int, year_label: int) -> list[dict]:
    """
    Fetch one week's DST stats table. `year_label` is just what we
    TAG the output rows with (since we can't control which season
    FantasyPros actually returns) — it's on the caller to confirm this
    matches the real current season before trusting the output.
    """
    params = {'range': 'week', 'week': week}
    try:
        r = requests.get(BASE_URL, params=params, headers=HEADERS, verify=False, timeout=15)
    except requests.RequestException as e:
        print(f"  Week {week}: request error: {e}")
        return []

    if r.status_code != 200:
        print(f"  Week {week}: HTTP {r.status_code}")
        return []

    soup = BeautifulSoup(r.content, 'lxml')
    tables = soup.find_all('table')
    if not tables:
        print(f"  Week {week}: no tables found on page")
        return []

    table = tables[0]
    tbody = table.find('tbody') or table

    rows = []
    for tr in tbody.find_all('tr'):
        tds = tr.find_all('td')
        if len(tds) < 11:
            continue   # header/separator row, not a data row

        try:
            # Player cell text looks like "Denver Broncos (DEN)"
            player_text = tds[1].get_text(strip=True)
            m = re.search(r'\(([A-Z]+)\)\s*$', player_text)
            if not m:
                continue
            espn_abb = m.group(1)
            team = normalize_team(espn_abb)
            if not team:
                print(f"  WARNING: couldn't normalize team abbreviation "
                      f"'{espn_abb}' from '{player_text}' — skipping row")
                continue

            rows.append({
                'year':             year_label,
                'week':             week,
                'team':             team,
                'sack':             _int(tds[2].get_text(strip=True)),
                'interception':     _int(tds[3].get_text(strip=True)),
                'fumble_rec':       _int(tds[4].get_text(strip=True)),
                'forced_fumble':    _int(tds[5].get_text(strip=True)),
                'def_td':           _int(tds[6].get_text(strip=True)),
                'safety':           _int(tds[7].get_text(strip=True)),
                'special_teams_td': _int(tds[8].get_text(strip=True)),
                'games':            _int(tds[9].get_text(strip=True)),
            })
        except Exception as e:
            continue

    if rows:
        print(f"  Week {week}: {len(rows)} teams parsed")
    else:
        print(f"  Week {week}: 0 rows parsed — table structure may have "
              f"changed. First row raw HTML: {str(tbody.find('tr'))[:500] if tbody.find('tr') else '(no rows)'}")

    return rows


def _save(existing: pd.DataFrame, new_rows: list, output_file: str) -> pd.DataFrame:
    if not new_rows:
        return existing
    new_df = pd.DataFrame(new_rows, columns=OUT_COLUMNS)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=['year', 'week', 'team'], keep='last')
    combined = combined.sort_values(['year', 'week', 'team'])
    combined.to_csv(output_file, index=False, compression='gzip')
    return combined


def main(year: int, weeks: list[int]):
    os.makedirs(DATA_DIR, exist_ok=True)
    output_file = os.path.join(DATA_DIR, f'dst_fantasy_stats_{year}.csv.gz')

    if os.path.exists(output_file):
        existing = pd.read_csv(output_file)
        print(f"Loaded existing file: {len(existing):,} rows")
    else:
        existing = pd.DataFrame(columns=OUT_COLUMNS)

    print(f"\nNOTE: FantasyPros doesn't support a year URL parameter — this")
    print(f"always scrapes whatever season is CURRENTLY live on their site.")
    print(f"Rows will be labeled year={year} — verify that's actually correct")
    print(f"by checking the page manually if you're unsure.\n")

    for week in weeks:
        rows = fetch_week(week, year)
        existing = _save(existing, rows, output_file)
        time.sleep(SLEEP_SEC)

    print(f"\nDone. {len(existing):,} rows total → {output_file}")


def parse_weeks(s: str) -> list[int]:
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    elif ',' in s:
        return [int(w) for w in s.split(',')]
    else:
        return [int(s)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, required=True,
                        help='Season year to LABEL the output with (see NOTE above)')
    parser.add_argument('--weeks', default='1-18',
                        help='Week(s) to scrape, e.g. "1-18" or "1,2,3" or "5"')
    args = parser.parse_args()

    weeks = parse_weeks(args.weeks)
    main(args.year, weeks)
