"""
scrapers/scrape_ourlads_depth_charts.py
-------------------------------------------
Scrapes NFL depth charts (QB/RB/WR/TE, 1st-3rd string) from
ourlads.com — confirmed real, plain server-rendered HTML (no JS
execution needed, unlike ESPN's depth chart page which returns an
empty bot-detection response for a plain request).

Structure confirmed via live diagnostic (Sept 2026):
  - 6 <table> elements per team page: Offense, Defense, Special Teams,
    Practice Squad, Reserves, Coaching Staff — identified by their
    nearest preceding heading (no distinguishing id/class exists; all
    data tables share the same class="table table-bordered")
  - We only want the "Offense" table (confirmed via heading text
    starting with "Offense") — Practice Squad/Reserves have their own
    separate QB/RB/TE rows that would be wrong to mix in
  - Columns: Pos | No. | Player 1 | No | Player 2 | No | Player 3 |
    No | Player 4 | No | Player 5 — player cells at td index 2,4,6,8,10
    correspond to string rank 1-5. We only keep ranks 1-3 (per the
    explicit ask to ignore 4th string, even though we could scrape it)
  - Player cell text is "LastName, FirstName [trailing metadata]",
    e.g. "Coleman, Keon 24/2" or "GAINES, GREG U/TB" — name-parsing
    logic (extract + normalize casing) tested against 8 real examples
    from this exact page before building this script
  - WR is split into 3 distinct slots (LWR/RWR/SWR), not one generic
    WR list — preserved as-is rather than artificially collapsed,
    since it's more useful this way (shows who starts at each specific
    WR spot) — flagged as a design choice, not assumed silently

Team list (all 32 URL slugs) taken directly from ourlads' own
navigation sidebar, confirmed via live fetch — not guessed. Two of
these (ARZ, RAM) are ourlads-specific quirks different from every
other source in this project; added as new team_mapping aliases.

Output: data/ourlads_depth_charts.csv.gz
  Columns: team, pos, string_rank, player_name, ourlads_player_id

Usage:
    python scrapers/scrape_ourlads_depth_charts.py
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

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
OUTPUT_FILE = os.path.join(DATA_DIR, 'ourlads_depth_charts.csv.gz')
SLEEP_SEC   = 2.0

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

# All 32 team URL slugs, confirmed directly from ourlads' own
# navigation sidebar (not guessed) — several differ from every other
# convention used elsewhere in this project (KC not KAN, NE not NWE,
# LV not LVR, ARZ not ARI, RAM not LAR, NO not NOR, TB not TAM,
# GB not GNB).
OURLADS_TEAMS = [
    'BUF', 'MIA', 'NE', 'NYJ',
    'BAL', 'CIN', 'CLE', 'PIT',
    'HOU', 'IND', 'JAX', 'TEN',
    'DEN', 'KC', 'LV', 'LAC',
    'DAL', 'NYG', 'PHI', 'WAS',
    'CHI', 'DET', 'GB', 'MIN',
    'ATL', 'CAR', 'NO', 'TB',
    'ARZ', 'RAM', 'SF', 'SEA',
]

# Positions we want, exactly as ourlads labels them on the Offense
# table. WR is 3 distinct slots there, not one generic list.
WANTED_POSITIONS = {'QB', 'RB', 'TE', 'LWR', 'RWR', 'SWR'}

OUT_COLUMNS = ['team', 'pos', 'string_rank', 'player_name', 'ourlads_player_id']


def _parse_player_name(raw_link_text: str) -> str:
    """
    'Coleman, Keon 24/2' -> 'Keon Coleman'
    'DAWKINS, DION 17/2' -> 'Dion Dawkins' (normalize all-caps)
    Tested against 8 real examples from ourlads before building this.
    """
    if ',' not in raw_link_text:
        return raw_link_text.strip()
    last_name, rest = raw_link_text.split(',', 1)
    rest = rest.strip()
    parts = rest.split()
    first_name = parts[0] if parts else ''

    last_name = last_name.strip()
    if last_name.isupper():
        last_name = last_name.title()
    if first_name.isupper():
        first_name = first_name.title()

    return f"{first_name} {last_name}".strip()


def _find_offense_table(soup):
    """
    No id/class distinguishes the Offense table from Defense/Special
    Teams/Practice Squad/Reserves (all share the same generic classes)
    — identify it by its nearest preceding heading instead, confirmed
    the only reliable signal via live diagnostic.
    """
    for table in soup.find_all('table'):
        heading = None
        for sib in table.find_all_previous(['h1', 'h2', 'h3', 'h4']):
            heading = sib.get_text(strip=True)
            break
        if heading and heading.startswith('Offense'):
            return table
    return None


def scrape_team(ourlads_slug: str) -> list[dict]:
    url = f"https://www.ourlads.com/nfldepthcharts/depthchart/{ourlads_slug}"
    print(f"  Fetching {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"    Request error: {e}")
        return []

    if r.status_code != 200:
        print(f"    HTTP {r.status_code}")
        return []

    soup = BeautifulSoup(r.content, 'html.parser')
    table = _find_offense_table(soup)
    if not table:
        print(f"    Couldn't find the Offense table (heading text may have changed)")
        return []

    team = normalize_team(ourlads_slug)
    if not team:
        print(f"    WARNING: couldn't normalize team slug '{ourlads_slug}' — skipping entirely")
        return []

    rows = table.find_all('tr')
    records = []

    for row in rows[1:]:   # skip header row
        cells = row.find_all('td')
        if not cells:
            continue

        pos = cells[0].get_text(strip=True)
        if pos not in WANTED_POSITIONS:
            continue

        # Player cells at index 2, 4, 6 = string rank 1, 2, 3
        # (explicitly ignoring rank 4/5 per the ask, even though
        # they're present in the table)
        for rank, cell_idx in enumerate([2, 4, 6], start=1):
            if cell_idx >= len(cells):
                continue
            link = cells[cell_idx].find('a')
            if not link:
                continue
            raw_text = link.get_text(strip=True)
            if not raw_text:
                continue   # empty slot (no player at this rank)

            player_id_match = re.search(r'/player/(\d+)/', link.get('href', ''))
            player_id = player_id_match.group(1) if player_id_match else None

            records.append({
                'team': team,
                'pos': pos,
                'string_rank': rank,
                'player_name': _parse_player_name(raw_text),
                'ourlads_player_id': player_id,
            })

    print(f"    {len(records)} depth chart entries parsed")
    return records


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    all_records = []

    for slug in OURLADS_TEAMS:
        print(f"\n{slug}:")
        records = scrape_team(slug)
        all_records.extend(records)
        time.sleep(SLEEP_SEC)

    if not all_records:
        print("\nNo data scraped — nothing saved.")
        return

    df = pd.DataFrame(all_records, columns=OUT_COLUMNS)
    df.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    print(f"\nSaved {len(df)} rows -> {OUTPUT_FILE}")

    teams_found = df['team'].nunique()
    print(f"Teams covered: {teams_found} / {len(OURLADS_TEAMS)}")
    if teams_found < len(OURLADS_TEAMS):
        missing = set(normalize_team(t) for t in OURLADS_TEAMS) - set(df['team'].unique())
        print(f"Missing teams: {missing}")

    print(f"\nSample — Buffalo's depth chart:")
    print(df[df['team'] == 'buf'].to_string(index=False))


if __name__ == "__main__":
    main()
