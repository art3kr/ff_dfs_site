"""
scrapers/scrape_draftedge_injuries.py
------------------------------------------
Scrapes current NFL injury statuses (Out, Doubtful, Questionable, IR)
from draftedge.com — confirmed real, live, current-season data via a
diagnostic dump of the actual HTML (Sept 2026), tested against the
exact Chase Bisontis row before writing this.

Structure confirmed real:
  - A single <table>, one <tr data-status="..."> per player — the
    status comes directly from this attribute (lowercase: "out",
    "doubtful", "questionable", "ir", plus "probable"/"day-to-day"
    which this deliberately filters out, since only Out/Doubtful/
    Questionable/IR were asked for)
  - Player name: <div class="player-injury-name">
  - Position: <span class="pos-badge">
  - Team: the 3rd <td>, plain text (no nested structure)

ESPN's injury page was tried first and confirmed JS-rendered
(unreadable via a plain fetch) — this is a working alternative found
via search, not the originally-suggested source.

Since Ourlads (the depth chart source) already filters out injured
players from the depth chart entirely, Out/Doubtful/IR players
realistically won't ever show up there to begin with — Questionable
is expected to be the status that actually appears most often on the
Depth Charts page in practice, though this table stores everything
matched for completeness.

Output: data/draftedge_injuries.csv.gz
  Columns: player_name, player_name_normalized, position, team, status

Usage:
    python scrapers/scrape_draftedge_injuries.py
"""

import os
import re
import sys
import requests
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
OUTPUT_FILE = os.path.join(DATA_DIR, 'draftedge_injuries.csv.gz')
URL         = "https://draftedge.com/nfl/nfl-injury-report/"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

WANTED_STATUSES = {'out', 'doubtful', 'questionable', 'ir'}


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


def scrape() -> pd.DataFrame:
    print(f"Fetching {URL}")
    try:
        r = requests.get(URL, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"  Request error: {e}")
        return pd.DataFrame()

    if r.status_code != 200:
        print(f"  HTTP {r.status_code}")
        return pd.DataFrame()

    soup = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')

    table = soup.find('table')
    if not table:
        print("  WARNING: no <table> found — page structure may have changed.")
        return pd.DataFrame()

    rows = table.find_all('tr', attrs={'data-status': True})
    print(f"  {len(rows)} player rows found")

    records = []
    skipped_status = {}
    for tr in rows:
        status = tr.get('data-status', '').lower()
        if status not in WANTED_STATUSES:
            skipped_status[status] = skipped_status.get(status, 0) + 1
            continue

        name_tag = tr.find('div', class_='player-injury-name')
        pos_tag = tr.find('span', class_='pos-badge')
        tds = tr.find_all('td')

        if not name_tag or not pos_tag or len(tds) < 3:
            continue

        player_name = name_tag.get_text(strip=True)
        position = pos_tag.get_text(strip=True)
        team_raw = tds[2].get_text(strip=True)
        team = normalize_team(team_raw) if team_raw else None

        records.append({
            'player_name': player_name,
            'player_name_normalized': normalize_name(player_name),
            'position': position,
            'team': team,
            'status': status,
        })

    if skipped_status:
        print(f"  Skipped statuses not requested: {skipped_status}")
    print(f"  {len(records)} rows kept (Out/Doubtful/Questionable/IR)")

    result = pd.DataFrame(records)

    # Confirmed real concern: if the source page ever lists the same
    # player twice with different statuses (a stale cached row
    # alongside a fresh one, a mid-week update not fully replacing the
    # old entry, etc.), both would silently end up here, and whichever
    # one loads last into the DB wins — not necessarily the correct
    # one. Surfacing this explicitly rather than letting it pass
    # silently, since a mismatched status is exactly the kind of thing
    # that's easy to not notice until someone checks the site directly.
    if not result.empty:
        dupes = result[result.duplicated(subset=['player_name_normalized'], keep=False)]
        if not dupes.empty:
            print(f"\n*** WARNING: {dupes['player_name_normalized'].nunique()} player(s) appear "
                  f"more than once with possibly different statuses — the loader will keep "
                  f"whichever one loads last, which may not be the correct/current one: ***")
            print(dupes.sort_values('player_name_normalized').to_string(index=False))

    return result


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    df = scrape()

    if df.empty:
        print("No data scraped — nothing saved.")
        return

    df.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    print(f"\nSaved {len(df)} rows -> {OUTPUT_FILE}")
    print(f"\nStatus breakdown:")
    print(df['status'].value_counts().to_string())


if __name__ == "__main__":
    main()
