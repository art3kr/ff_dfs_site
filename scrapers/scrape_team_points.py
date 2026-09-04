"""
scrapers/scrape_team_points.py
---------------------------------
Scrapes points scored/allowed for every team, every week, from PFR's
yearly schedule page — one request per year covers every game that
season, far cheaper than any per-player approach.

Why this is needed: DraftKings' DST scoring formula has a large bracket
component based on points allowed in that specific game (worth +10 to
-4 depending on the bracket). Neither FantasyPros' DST stats page nor
any per-player scrape we already do captures this. PFR's schedule page
has it directly via the confirmed data-stat fields: week_num, winner,
loser, pts_win, pts_lose.

Table id='games' and the winner/loser/game_location/week_num fields
are already proven working elsewhere in this codebase
(_scrape_schedule_from_pfr in scrape_pfr.py) — this reuses that same
proven request/parsing approach, adding pts_win/pts_lose and deriving
per-team points scored/allowed for BOTH teams in every game.

Team abbreviations are extracted directly from each team's link href
(e.g. /teams/kan/2024.htm -> 'kan') rather than guessing at a
full-name-to-abbreviation mapping — matches the abbreviation format
already used throughout hist_player_stats.team.

Output: data/team_points_by_week.csv.gz
  Columns: year, week, team, opponent, home_away,
           points_scored, points_allowed

Usage:
    python scrapers/scrape_team_points.py
    python scrapers/scrape_team_points.py --years 2024-2025
"""

import os
import re
import sys
import time
import argparse
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from scrape_pfr import make_session, pfr_get, PFR_BASE, _int

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
OUTPUT_FILE = os.path.join(DATA_DIR, 'team_points_by_week.csv.gz')
SLEEP_YEAR  = 3.0   # increased from 2.0 — being more conservative given recent rate limiting

OUT_COLUMNS = ['year', 'week', 'team', 'opponent', 'home_away',
               'points_scored', 'points_allowed']

# PFR's schedule page URLs (/teams/{code}/...) use LEGACY franchise
# codes for teams that relocated or renamed, rather than the modern
# abbreviation used everywhere else on the site (including the player
# gamelog pages hist_player_stats.team is built from). Confirmed via
# a real diagnostic comparing this scraper's output against
# hist_player_stats/DST data for all 32 teams — exactly these 8 teams
# differed, all relocated/renamed franchises:
#   Cardinals (moved from Chicago/St. Louis), Ravens (Cleveland Browns
#   lineage), Texans (expansion, but PFR still uses 'htx'), Colts
#   (moved from Baltimore), Chargers (San Diego era code), Rams (St.
#   Louis era code), Raiders (Oakland/LV moves), Titans (Houston
#   Oilers lineage).
# Without this translation, every points-allowed lookup for these 8
# teams would silently fail to match against the rest of the system.
LEGACY_TO_MODERN_CODE = {
    'crd': 'ari',   # Cardinals
    'rav': 'bal',   # Ravens
    'htx': 'hou',   # Texans
    'clt': 'ind',   # Colts
    'sdg': 'lac',   # Chargers
    'ram': 'lar',   # Rams
    'rai': 'lvr',   # Raiders
    'oti': 'ten',   # Titans
}


def _team_abbrev_from_cell(cell) -> str:
    """
    Extract a team's abbreviation from its link href, e.g.
    '/teams/kan/2024.htm' -> 'kan'. Falls back to the cell's raw text
    (lowercased, spaces stripped) if no link is found — rare, but keeps
    the row instead of dropping it silently. Legacy franchise codes are
    translated to their modern equivalent (see LEGACY_TO_MODERN_CODE).
    """
    if cell is None:
        return ''
    link = cell.find('a')
    if link and link.get('href'):
        m = re.search(r'/teams/(\w+)/', link['href'])
        if m:
            code = m.group(1)
            return LEGACY_TO_MODERN_CODE.get(code, code)
    return cell.get_text(strip=True).lower().replace(' ', '')


def scrape_year(year: int, session) -> list[dict]:
    """
    Fetch one year's schedule page and return per-team rows with points
    scored/allowed for every regular-season game (weeks 1-18).
    """
    url = f"{PFR_BASE}/years/{year}/games.htm"
    print(f"  Fetching {url}")
    r = pfr_get(session, url, use_headers=False)

    if r is None:
        print(f"  Request failed for {year}")
        return []

    soup  = BeautifulSoup(r.content, 'lxml')
    table = soup.find('table', id='games')
    if not table:
        live_ids = [t.get('id') for t in soup.find_all('table') if t.get('id')]
        print(f"  'games' table not found. Live table ids: {live_ids}")
        return []

    tbody = table.find('tbody')
    if not tbody:
        print(f"  'games' table has no <tbody>")
        return []

    rows = []
    skipped_no_score = 0

    for row in tbody.find_all('tr'):
        if row.get('class') and 'thead' in row.get('class'):
            continue

        try:
            week_cell = row.find('th', attrs={'data-stat': 'week_num'})
            week      = _int(week_cell.get_text()) if week_cell else 0
            if week == 0 or week > 18:
                continue   # skip playoff rows and header/separator rows

            winner_cell = row.find('td', attrs={'data-stat': 'winner'})
            loser_cell  = row.find('td', attrs={'data-stat': 'loser'})
            loc_cell    = row.find('td', attrs={'data-stat': 'game_location'})
            pts_win_cell = row.find('td', attrs={'data-stat': 'pts_win'})
            pts_lose_cell = row.find('td', attrs={'data-stat': 'pts_lose'})

            pts_win_text  = pts_win_cell.get_text(strip=True)  if pts_win_cell  else ''
            pts_lose_text = pts_lose_cell.get_text(strip=True) if pts_lose_cell else ''

            if not pts_win_text or not pts_lose_text:
                # Game hasn't been played yet (future week) — nothing to score
                skipped_no_score += 1
                continue

            pts_win  = _int(pts_win_text)
            pts_lose = _int(pts_lose_text)

            winner = _team_abbrev_from_cell(winner_cell)
            loser  = _team_abbrev_from_cell(loser_cell)
            game_loc = loc_cell.get_text(strip=True) if loc_cell else ''

            if not winner or not loser:
                continue

            # game_location == '@' means the WINNER was the away team
            if game_loc == '@':
                home_team, away_team = loser, winner
            else:
                home_team, away_team = winner, loser

            # One row for each team in the game
            rows.append({
                'year': year, 'week': week,
                'team': winner, 'opponent': loser,
                'home_away': 'a' if winner == away_team else 'h',
                'points_scored': pts_win, 'points_allowed': pts_lose,
            })
            rows.append({
                'year': year, 'week': week,
                'team': loser, 'opponent': winner,
                'home_away': 'a' if loser == away_team else 'h',
                'points_scored': pts_lose, 'points_allowed': pts_win,
            })

        except Exception:
            continue

    print(f"  {len(rows)} team-game rows ({skipped_no_score} games skipped — not yet played)")
    return rows


def _save(existing: pd.DataFrame, new_rows: list) -> pd.DataFrame:
    if not new_rows:
        return existing
    new_df = pd.DataFrame(new_rows, columns=OUT_COLUMNS)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=['year', 'week', 'team'], keep='last')
    combined = combined.sort_values(['year', 'week', 'team'])
    combined.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    return combined


def main(years: list[int]):
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        done_years = set(existing['year'].unique())
        print(f"Loaded existing file: {len(existing):,} rows, years already done: {sorted(done_years)}")
    else:
        existing = pd.DataFrame(columns=OUT_COLUMNS)
        done_years = set()

    with make_session() as session:
        for year in years:
            print(f"\n{'='*50}\nYEAR {year}\n{'='*50}")

            if year in done_years:
                print(f"  Already have {year} — re-scraping anyway to pick up any newly-played games")

            new_rows = scrape_year(year, session)
            existing = _save(existing, new_rows)
            time.sleep(SLEEP_YEAR)

    print(f"\nDone. {len(existing):,} rows total → {OUTPUT_FILE}")


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
    parser.add_argument('--years', default='2014-2025',
                        help='Year(s) to scrape, e.g. "2014-2025" or "2026"')
    args = parser.parse_args()

    years = parse_years(args.years)
    print(f"Scraping team points for: {years}")
    main(years)
