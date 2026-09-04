"""
scrapers/scrape_fantasy_points_against.py
---------------------------------------------
Scrapes season-level "fantasy points allowed by position" data — which
teams are weak/strong matchups against QB/RB/WR/TE. Confirmed real,
live table structure via web_fetch (Sept 2026):

    https://www.pro-football-reference.com/years/{year}/fantasy-points-against-{POS}.htm

Real confirmed columns (2024 WR page): Tm, G, Tgt, Rec, Yds, TD,
FantPt, DKPt, FDPt, FantPt (per game), DKPt (per game), FDPt (per game).
Uses DKPt (DraftKings-specific) rather than the old function's FDPt
(FanDuel) — this project is DK-focused throughout.

IMPORTANT — this is a SEASON-TOTAL page, not weekly splits (confirmed:
every team shows G=17, a full season). Works for both historical and
in-season years via the same /years/{year}/ URL pattern already proven
elsewhere in this project (games.htm, opp.htm) — unlike FantasyPros,
which silently ignores year entirely.

FIELD NAMES NOT YET VERIFIED: we have the rendered table (real data)
but not the raw HTML's data-stat attribute names. Built with strong
candidates based on PFR's confirmed naming conventions elsewhere
(e.g. 'draftkings_points' on player fantasy pages) — but with a full
debug dump if none of the candidates match, rather than silently
guessing wrong. Check the output for a WARNING before trusting results.

Usage:
    python scrapers/scrape_fantasy_points_against.py --year 2024
    python scrapers/scrape_fantasy_points_against.py --year 2024 --position WR
"""

import os
import sys
import time
import argparse
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from scrape_pfr import make_session, pfr_get, PFR_BASE
from team_mapping import normalize_team

DATA_DIR   = os.path.join(os.path.dirname(__file__), '..', 'data')
SLEEP_SEC  = 2.0
POSITIONS  = ['QB', 'RB', 'WR', 'TE']

# Candidate data-stat names per logical field, tried in order.
# 'games' should be safe (basic/common PFR convention); the fantasy
# point fields are the ones genuinely unverified.
FIELD_CANDIDATES = {
    'games':               ['g', 'games'],
    'fantasy_pts':          ['fantasy_points', 'fantasy_points_total'],
    'dk_pts':               ['draftkings_points', 'dk_points'],
    'fd_pts':               ['fanduel_points', 'fd_points'],
    'fantasy_pts_per_game': ['fantasy_points_per_game', 'fantasy_points_pg'],
    'dk_pts_per_game':      ['draftkings_points_per_game', 'dk_points_per_game'],
    'fd_pts_per_game':      ['fanduel_points_per_game', 'fd_points_per_game'],
}

OUT_COLUMNS = ['year', 'position', 'team', 'games', 'fantasy_pts', 'dk_pts', 'fd_pts',
               'fantasy_pts_per_game', 'dk_pts_per_game', 'fd_pts_per_game']


def _get_field(row, field_key: str, warned: set) -> str:
    for candidate in FIELD_CANDIDATES[field_key]:
        cell = row.find('td', attrs={'data-stat': candidate})
        if cell:
            return cell.get_text(strip=True)
    if field_key not in warned:
        available = [c.get('data-stat') for c in row.find_all(['td', 'th'])]
        print(f"    WARNING: none of {FIELD_CANDIDATES[field_key]} matched for "
              f"'{field_key}'. Available data-stat names on this row: {available}")
        warned.add(field_key)
    return ''


def _float(s) -> float:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return 0.0


def _int(s) -> int:
    try:
        return int(float(str(s).strip()))
    except (ValueError, TypeError):
        return 0


def scrape_position(year: int, position: str, session) -> pd.DataFrame:
    url = f"{PFR_BASE}/years/{year}/fantasy-points-against-{position}.htm"
    print(f"  Fetching {url}")
    r = pfr_get(session, url, use_headers=False)

    if r is None:
        print(f"    Request failed")
        return pd.DataFrame()

    soup = BeautifulSoup(r.content, 'lxml')
    tables = soup.find_all('table')
    if not tables:
        print(f"    No table found")
        return pd.DataFrame()

    table = tables[0]
    tbody = table.find('tbody')
    if not tbody:
        print(f"    No <tbody> found")
        return pd.DataFrame()

    records = []
    warned = set()

    for row in tbody.find_all('tr'):
        if row.get('class') and 'thead' in row.get('class'):
            continue

        team_cell = row.find('th', attrs={'data-stat': 'team'}) or row.find('td', attrs={'data-stat': 'team'})
        if not team_cell:
            continue
        team_raw = team_cell.get_text(strip=True)
        if not team_raw:
            continue
        team = normalize_team(team_raw)
        if not team:
            print(f"    WARNING: couldn't normalize team '{team_raw}' — skipping")
            continue

        records.append({
            'year': year,
            'position': position,
            'team': team,
            'games': _int(_get_field(row, 'games', warned)),
            'fantasy_pts': _float(_get_field(row, 'fantasy_pts', warned)),
            'dk_pts': _float(_get_field(row, 'dk_pts', warned)),
            'fd_pts': _float(_get_field(row, 'fd_pts', warned)),
            'fantasy_pts_per_game': _float(_get_field(row, 'fantasy_pts_per_game', warned)),
            'dk_pts_per_game': _float(_get_field(row, 'dk_pts_per_game', warned)),
            'fd_pts_per_game': _float(_get_field(row, 'fd_pts_per_game', warned)),
        })

    print(f"    {len(records)} teams parsed")
    return pd.DataFrame(records, columns=OUT_COLUMNS)


def main(year: int, positions: list[str]):
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, f'fantasy_points_against_{year}.csv.gz')

    all_rows = []
    with make_session() as session:
        for position in positions:
            print(f"\n{position}:")
            df = scrape_position(year, position, session)
            if not df.empty:
                all_rows.append(df)
            time.sleep(SLEEP_SEC)

    if not all_rows:
        print("\nNo data scraped — nothing saved.")
        return

    combined = pd.concat(all_rows, ignore_index=True)
    combined.to_csv(out_path, index=False, compression='gzip')
    print(f"\nSaved {len(combined)} rows -> {out_path}")

    print(f"\nSample — top 5 teams most generous to WRs (by DK pts/game allowed):")
    wr = combined[combined['position'] == 'WR'].sort_values('dk_pts_per_game', ascending=False)
    print(wr.head(5).to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--position", choices=POSITIONS + ['all'], default='all')
    args = parser.parse_args()

    positions = POSITIONS if args.position == 'all' else [args.position]
    main(args.year, positions)
