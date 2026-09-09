"""
scrapers/scrape_firstdown_studio_rankings.py
--------------------------------------------------
Scrapes FirstDown Studio's own Vegas-prop-derived fantasy point
estimate per player — confirmed real via a diagnostic dump of the
actual HTML (Sept 2026), tested against the exact Justin Herbert row
before writing this.

Worth adding as a COMPARISON column on Implied Player Points, not a
replacement: their page shows a separate "TDs" column (rushing +
receiving touchdowns, fractional values like 0.18) that their "Pts"
total appears to include — something our own implied points
deliberately excludes, since the anytime-TD prop is moneyline-shaped
and can't be reliably de-vigged without a "no" side price. Where the
two numbers diverge is often exactly where a TD-heavy player gets
underweighted by ours.

Structure confirmed real:
  - One <table> per position page (QB is the default /rankings URL,
    others are /rankings/rb, /rankings/wr, /rankings/te)
  - Data rows have <td> cells; header rows have <th> cells only, so
    filtering on "has at least one <td>" cleanly separates the two
  - Player name: a <span title="Full Name"> — avoids the duplicate
    mobile/desktop spans that would otherwise return the name twice
  - Team: a <span class="font-semibold text-foreground"> nested
    inside the same cell as the player name
  - Pts is always the 3rd <td> (index 2), regardless of position —
    the position-specific stat breakdown columns that follow differ,
    but this one's position is consistent across every page

This is a THIRD-PARTY, opaque estimate — their own model, not
something we can verify the internals of. Stored and displayed as a
comparison point, not a replacement for our own transparently-computed
number.

Output: data/firstdown_studio_rankings.csv.gz
  Columns: player_name, player_name_normalized, team, position, pts

Usage:
    python scrapers/scrape_firstdown_studio_rankings.py
"""

import os
import re
import sys
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
OUTPUT_FILE = os.path.join(DATA_DIR, 'firstdown_studio_rankings.csv.gz')

POSITION_URLS = {
    'QB': 'https://www.firstdown.studio/rankings',
    'RB': 'https://www.firstdown.studio/rankings/rb',
    'WR': 'https://www.firstdown.studio/rankings/wr',
    'TE': 'https://www.firstdown.studio/rankings/te',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


def scrape_position(position: str, url: str) -> pd.DataFrame:
    print(f"Fetching {position}: {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"  Request error: {e}")
        return pd.DataFrame()

    if r.status_code != 200:
        print(f"  HTTP {r.status_code}")
        return pd.DataFrame()

    soup = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')

    table = soup.find('table')
    if not table:
        print(f"  WARNING: no <table> found for {position} — page structure may have changed.")
        return pd.DataFrame()

    records = []
    for tr in table.find_all('tr'):
        cells = tr.find_all('td')
        if len(cells) < 3:
            continue

        name_tag = cells[1].find('span', title=True)
        team_tag = cells[1].find('span', class_='font-semibold')
        if not name_tag or not team_tag:
            continue

        player_name = name_tag['title'].strip()
        team_raw = team_tag.get_text(strip=True)
        team = normalize_team(team_raw) if team_raw else None
        pts_text = cells[2].get_text(strip=True)

        try:
            pts = float(pts_text)
        except ValueError:
            continue

        records.append({
            'player_name': player_name,
            'player_name_normalized': normalize_name(player_name),
            'team': team, 'position': position, 'pts': pts,
        })

    print(f"  {len(records)} players parsed")
    return pd.DataFrame(records)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    all_dfs = []

    for position, url in POSITION_URLS.items():
        df = scrape_position(position, url)
        if not df.empty:
            all_dfs.append(df)
        time.sleep(2)

    if not all_dfs:
        print("No data scraped — nothing saved.")
        return

    combined = pd.concat(all_dfs, ignore_index=True)
    combined.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    print(f"\nSaved {len(combined)} rows -> {OUTPUT_FILE}")
    print(f"\nSample:")
    print(combined.head(6).to_string(index=False))


if __name__ == "__main__":
    main()
