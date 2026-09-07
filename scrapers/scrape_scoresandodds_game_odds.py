"""
scrapers/scrape_scoresandodds_game_odds.py
--------------------------------------------------
Scrapes game-level spread, total (over/under), and favorite for every
upcoming NFL game — real pre-game data (unlike hist_game_info/PFR,
which is post-game only), confirmed via a live diagnostic dump of the
real HTML (Sept 2026), tested against the exact Patriots/Seahawks row
before writing this.

Structure confirmed real:
  - Three <tbody id="odds-table-{market}--0"> elements (spread, total,
    moneyline), joined across markets by a shared data-event="nfl/{ID}"
    id embedded in each team-row's data-content attribute
  - Each game is two <tr> rows (one per team); the first carries a
    rowspan=2 <td class="game-time"> with the real kickoff timestamp,
    shared by both rows
  - Team name comes from a data-abbr attribute (e.g. "Patriots")
  - Multiple <td class="game-odds"> columns exist (one per sportsbook)
    — this takes the FIRST one as a representative line, not a full
    multi-book comparison like scrape_scoresandodds_market_comparison.py
  - Favorite is derived from whichever side has the negative spread
    value, not a separate field on the page

Output: data/scoresandodds_game_odds.csv.gz
  Columns: event_id, kickoff, team, opponent, spread, spread_odds,
           over_under, favorite

Usage:
    python scrapers/scrape_scoresandodds_game_odds.py
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
OUTPUT_FILE = os.path.join(DATA_DIR, 'scoresandodds_game_odds.csv.gz')
URL         = "https://www.scoresandodds.com/nfl/odds"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}


def _parse_market_tbody(tbody):
    """Returns {(event_id, team_abbr): {...}} for one market's tbody."""
    result = {}
    current_kickoff = None

    for row in tbody.find_all('tr'):
        time_cell = row.find('td', class_='game-time')
        if time_cell:
            time_link = time_cell.find('a', attrs={'data-value': True})
            if time_link:
                current_kickoff = time_link.get('data-value')

        team_cell = row.find('td', class_='game-team')
        if not team_cell:
            continue

        data_content = team_cell.get('data-content', '')
        event_match = re.search(r'data-event="(nfl/\d+)"', data_content)
        event_id = event_match.group(1) if event_match else None

        team_link = team_cell.find('a', attrs={'data-abbr': True})
        team_abbr = team_link.get('data-abbr') if team_link else None

        odds_cells = row.find_all('td', class_='game-odds')
        if not odds_cells or not event_id or not team_abbr:
            continue

        first_cell = odds_cells[0]
        value_span = first_cell.find('span', class_='data-value')
        odds_span = first_cell.find('small', class_='data-odds')
        value = value_span.get_text(strip=True) if value_span else None
        odds = odds_span.get_text(strip=True) if odds_span else None

        result[(event_id, team_abbr)] = {'value': value, 'odds': odds, 'kickoff': current_kickoff}

    return result


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

    tbodies = soup.find_all('tbody', id=lambda i: i and i.startswith('odds-table-'))
    print(f"  {len(tbodies)} market tbodies found")

    markets = {}
    for tbody in tbodies:
        market_name = tbody['id'].replace('odds-table-', '').split('--')[0]
        markets[market_name] = _parse_market_tbody(tbody)
        print(f"    {market_name}: {len(markets[market_name])} team-rows parsed")

    if 'spread' not in markets:
        print("  WARNING: no spread market found — page structure may have changed.")
        return pd.DataFrame()

    games = {}
    for (event_id, team_abbr), data in markets['spread'].items():
        games.setdefault(event_id, []).append((team_abbr, data))

    records = []
    for event_id, teams in games.items():
        if len(teams) != 2:
            print(f"    WARNING: event {event_id} has {len(teams)} teams, expected 2 — skipping")
            continue

        (team_a, data_a), (team_b, data_b) = teams

        def spread_float(v):
            if v is None:
                return None
            try:
                return float(v)
            except ValueError:
                return None

        spread_a = spread_float(data_a['value'])
        spread_b = spread_float(data_b['value'])

        favorite = None
        if spread_a is not None and spread_b is not None:
            if spread_a < 0:
                favorite = team_a
            elif spread_b < 0:
                favorite = team_b

        total_a = markets.get('total', {}).get((event_id, team_a), {}).get('value')

        for team_abbr, data in [(team_a, data_a), (team_b, data_b)]:
            opponent = team_b if team_abbr == team_a else team_a
            records.append({
                'event_id': event_id,
                'kickoff': data['kickoff'],
                'team': normalize_team(team_abbr),
                'opponent': normalize_team(opponent),
                'spread': spread_float(data['value']),
                'spread_odds': data['odds'],
                'over_under': total_a,
                'favorite': normalize_team(favorite) if favorite else None,
            })

    print(f"  {len(records)} team-rows across {len(games)} games")
    return pd.DataFrame(records)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    df = scrape()

    if df.empty:
        print("No data scraped — nothing saved.")
        return

    df.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    print(f"\nSaved {len(df)} rows -> {OUTPUT_FILE}")
    print(f"\nSample:")
    print(df.head(6).to_string(index=False))


if __name__ == "__main__":
    main()
