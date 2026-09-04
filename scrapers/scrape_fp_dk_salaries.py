"""
scrapers/scrape_fp_dk_salaries.py
-------------------------------------
Scrapes real, current DraftKings weekly salaries from FantasyPros'
salary-changes page — solves the RotoWire 1-player-free-tier problem.

Structure confirmed via live diagnostic (Sept 2026):
  - Real <table id="data-table">, one row per player, ALL positions
    (QB/RB/WR/TE/DST) combined in a single table — no need for
    separate requests per position
  - Player-name cell has a clean fp-player-name="..." attribute on
    the <a> tag (no responsive dual-name complexity like
    firstdown.studio had), followed by a <small> tag with
    "(TEAM - POS)" — both confirmed via real HTML dump
  - Opponent cell shows "@TEAM" for an away game, bare "TEAM" for home
  - Salary cell is "$X,XXX" format

IMPORTANT — kickoff date: this page only shows day-of-week + time
("Sun 4:25PM"), no calendar date. Rather than guess a date from an
ambiguous day name, we cross-reference the REAL kickoff datetime
already in game_schedule (populated from PFR via flask load-schedule,
which has actual confirmed dates) by (year, week, team) — strictly
more reliable than reconstructing one from this page. The raw
"Sun 4:25PM" text is still captured for reference/display, but isn't
used as the authoritative time for lock enforcement.

Output columns match hist_dfs_salaries exactly, so this feeds directly
into the same load-history pipeline as every other salary source (for
eventual merging with PFR stats after the week is played) AND can load
into the live `players` table for the current week's Slate page.

Usage:
    python scrapers/scrape_fp_dk_salaries.py --week 1 --year 2026
"""

import os
import re
import sys
import argparse
import requests
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
URL = "https://www.fantasypros.com/daily-fantasy/nfl/draftkings-salary-changes.php"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

OUT_COLUMNS = ['week', 'year', 'name', 'name_normalized', 'position', 'team',
               'opponent', 'home_away', 'dk_salary', 'dk_pts_scored',
               'projected_pts', 'ownership_pct', 'source', 'kickoff_raw']


def _extract_name_team_pos(name_cell) -> tuple[str, str, str]:
    """
    Confirmed structure: <a fp-player-name="...">Name</a> <small>(TEAM - POS)</small>
    """
    link = name_cell.find('a')
    if link:
        name = link.get('fp-player-name') or link.get_text(strip=True)
    else:
        name = name_cell.get_text(strip=True)

    small = name_cell.find('small')
    team_raw, position = '', ''
    if small:
        m = re.match(r'\(([A-Z]+)\s*-\s*(\w+)\)', small.get_text(strip=True))
        if m:
            team_raw, position = m.group(1), m.group(2)

    return name, team_raw, position


def _parse_salary(text: str) -> int | None:
    cleaned = re.sub(r'[^\d]', '', text)
    return int(cleaned) if cleaned else None


def scrape(week: int, year: int) -> pd.DataFrame:
    print(f"Fetching {URL}")
    try:
        r = requests.get(URL, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"  Request error: {e}")
        return pd.DataFrame()

    if r.status_code != 200:
        print(f"  HTTP {r.status_code}")
        return pd.DataFrame()

    soup = BeautifulSoup(r.content, 'lxml')
    table = soup.find('table', id='data-table')
    if not table:
        tables = soup.find_all('table')
        print(f"  'data-table' not found — page structure may have changed. "
              f"Found {len(tables)} other table(s).")
        return pd.DataFrame()

    rows = table.find_all('tr')
    records = []
    skipped = 0

    for row in rows:
        cells = row.find_all('td')   # skip header row (th cells only)
        if len(cells) < 5:
            continue

        try:
            name, team_raw, position = _extract_name_team_pos(cells[1])
            if not name:
                skipped += 1
                continue

            team = normalize_team(team_raw)
            if not team:
                print(f"  WARNING: couldn't normalize team '{team_raw}' for '{name}' — skipping")
                skipped += 1
                continue

            kickoff_raw = cells[2].get_text(strip=True)

            opp_text = cells[3].get_text(strip=True)
            home_away = 'a' if opp_text.startswith('@') else 'h'
            opp_raw = opp_text.lstrip('@')
            opponent = normalize_team(opp_raw) or opp_raw.lower()

            dk_salary = _parse_salary(cells[4].get_text(strip=True))

            name_normalized = re.sub(r"[^a-z0-9 ]", "", name.lower().strip())

            records.append({
                'week': week,
                'year': year,
                'name': name,
                'name_normalized': name_normalized,
                'position': position,
                'team': team,
                'opponent': opponent,
                'home_away': home_away,
                'dk_salary': dk_salary,
                'dk_pts_scored': None,      # not available from this source
                'projected_pts': None,      # not available from this source
                'ownership_pct': None,      # not available from this source
                'source': 'fantasypros_dk_salary',
                'kickoff_raw': kickoff_raw,  # reference only — NOT authoritative;
                                              # real kickoff comes from game_schedule
            })
        except Exception as e:
            print(f"  Error parsing a row: {e}")
            skipped += 1
            continue

    print(f"  {len(records)} players parsed, {skipped} skipped")
    return pd.DataFrame(records, columns=OUT_COLUMNS)


def main(week: int, year: int):
    os.makedirs(DATA_DIR, exist_ok=True)
    df = scrape(week, year)

    if df.empty:
        print("No data parsed — nothing saved.")
        return

    out_path = os.path.join(DATA_DIR, f"fp_dk_salaries_week{week}_{year}.csv.gz")
    df.to_csv(out_path, index=False, compression='gzip')
    print(f"Saved {len(df)} rows -> {out_path}")

    pos_counts = df['position'].value_counts()
    print(f"\nBy position:\n{pos_counts.to_string()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--year", type=int, required=True)
    args = parser.parse_args()
    main(args.week, args.year)
