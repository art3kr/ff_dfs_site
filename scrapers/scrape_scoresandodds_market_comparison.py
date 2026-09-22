"""
scrapers/scrape_scoresandodds_market_comparison.py
--------------------------------------------------------
Fetches the full multi-sportsbook comparison for props already
scraped by scrape_scoresandodds_props.py — the same data you'd see by
clicking a player to expand on the site, but pulled directly via the
API it calls (found via the user's own browser DevTools Network tab,
not guessed).

Confirmed real, live response structure (Sept 2026), tested against
the exact URL the user provided (Lamar Jackson, passing yards):
    https://rga51lus77.execute-api.us-east-1.amazonaws.com/prod/market-comparison
        ?event={event_id}&market={market name with spaces}&filter={player name}
No API key or auth needed; the 't=' timestamp param the site's JS
adds is NOT required — confirmed by a successful fetch omitting it
entirely, it's just a frontend cache-buster.

**`filter` is optional, and leaving it off returns the whole market.**
One request per (event, market) comes back with every player in that
market for that game, same structure. That is what this scraper does now:
224 requests instead of 1,689, about 3 minutes instead of ~34, and much
lighter on the site. Verified by diagnose_scoresandodds_bulk_market.py on
2026-09-22 across all 18 categories: no player the per-player form found
was missing from the bulk form, and the bulk form found 2 extra. Run that
diagnostic again if the site's markup or API ever changes.

Response JSON's relevant shape:
    {
      "markets": [{
        "player": {"first_name": ..., "last_name": ..., "team": {"key": ...}},
        "projection": <site's consensus projection>,
        "comparison": {
          "<book_slug>": {"value": <line>, "over": <odds>, "under": <odds>,
                          "available": <bool>, ...},
          ...
        }
      }]
    }
One row per (player, category, book) is written — so a single scraped
prop with 9 books comparison becomes 9 output rows, each with that
book's own line and odds side by side.

Requires event_id from scrape_scoresandodds_props.py's output (added
specifically to support this script) — run that first. Only the distinct
(event_id, category) pairs are used, not the individual players: the
player list comes back from the API itself.

Output: data/scoresandodds_market_comparison.csv.gz
  Columns: category, player_name, team, book, line, over_odds,
           under_odds, site_projection, available

`available` is the API's own per-book flag. A book can keep returning a
line and prices for a market it has pulled or suspended, and that stale
price is exactly what looks like an edge in find_ev_bets.py, so analysis
scripts should drop available == False.

Usage:
    python scrapers/scrape_scoresandodds_market_comparison.py \\
        --input data/scoresandodds_props_all.csv.gz
    python scrapers/scrape_scoresandodds_market_comparison.py --per-player
        (the old one-request-per-player form, kept as a fallback in case a
         category ever misbehaves in bulk)
"""

import os
import sys
import time
import argparse
import urllib.parse
import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
OUTPUT_FILE = os.path.join(DATA_DIR, 'scoresandodds_market_comparison.csv.gz')
API_BASE    = "https://rga51lus77.execute-api.us-east-1.amazonaws.com/prod/market-comparison"
SLEEP_SEC   = 1.0
SAVE_EVERY  = 25      # markets between writes; the whole gzip is rewritten each time

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

# scoresandodds' URL-path category slugs use hyphens (e.g.
# "passing-yards"); this API's `market` param uses the space-separated
# display name instead (e.g. "passing yards") — confirmed via the
# user's real URL and the response's own "stat" field echoing it back.
CATEGORY_SLUG_TO_MARKET_NAME = {
    'passing-yards':   'passing yards',
    'rushing-yards':   'rushing yards',
    'receiving-yards': 'receiving yards',
    'receptions':      'receptions',
    'touchdowns':      'touchdowns',
    'passing-tds':     'passing tds',
    'completions':     'completions',
    'interceptions':   'interceptions',
    'pass-attempts':   'pass attempts',
    'rush-attempts':   'rush attempts',
    'passing-and-rushing-yards':   'passing & rushing yards',
    'rushing-and-receiving-yards': 'rushing & receiving yards',
    'first-touchdown-scorer': 'first touchdown scorer',
    'last-touchdown-scorer':  'last touchdown scorer',
    'longest-reception':   'longest reception',
    'longest-rush':        'longest rush',
    'longest-completion':  'longest completion',
    'kicking-points':      'kicking points',
}

OUT_COLUMNS = ['category', 'player_name', 'team', 'book',
               'line', 'over_odds', 'under_odds', 'site_projection', 'available']


def fetch_market(event_id: str, category: str) -> list:
    """Every player in one market for one game, as output rows.

    Same endpoint as fetch_comparison() below, minus the `filter` param.
    Returns [] on any error, after printing why — a single bad market
    shouldn't stop a slate.
    """
    market_name = CATEGORY_SLUG_TO_MARKET_NAME.get(category, category.replace('-', ' '))
    params = {'event': event_id, 'market': market_name}
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"    Request error for {category} ({event_id}): {e}")
        return []

    if r.status_code != 200:
        print(f"    HTTP {r.status_code} for {category} ({event_id})")
        return []

    try:
        markets = r.json().get('markets', [])
    except ValueError:
        print(f"    Non-JSON response for {category} ({event_id})")
        return []

    rows = []
    for market in markets:
        player_info = market.get('player') or {}
        first, last = player_info.get('first_name'), player_info.get('last_name')
        # The props list spells names "First Last"; keep that so every
        # downstream join (name_normalized, the bet log, the archive) still
        # matches rows scraped by the old per-player form.
        player_name = f"{first} {last}".strip() if (first or last) else market.get('description')
        if not player_name:
            continue
        team_raw = (player_info.get('team') or {}).get('key')
        team = normalize_team(team_raw) if team_raw else None
        projection = market.get('projection')

        for book_slug, book_data in (market.get('comparison') or {}).items():
            rows.append({
                'category': category,
                'player_name': player_name,
                'team': team,
                'book': book_slug,
                'line': book_data.get('value'),
                'over_odds': book_data.get('over'),
                'under_odds': book_data.get('under'),
                'site_projection': projection,
                'available': book_data.get('available'),
            })
    return rows


def fetch_comparison(event_id: str, category: str, player_name: str) -> list:
    market_name = CATEGORY_SLUG_TO_MARKET_NAME.get(category, category.replace('-', ' '))
    params = {'event': event_id, 'market': market_name, 'filter': player_name}
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"    Request error for {player_name} ({category}): {e}")
        return []

    if r.status_code != 200:
        print(f"    HTTP {r.status_code} for {player_name} ({category})")
        return []

    try:
        data = r.json()
    except ValueError:
        print(f"    Non-JSON response for {player_name} ({category})")
        return []

    markets = data.get('markets', [])
    if not markets:
        print(f"    No markets in response for {player_name} ({category})")
        return []

    market = markets[0]
    player_info = market.get('player', {})
    team_raw = (player_info.get('team') or {}).get('key')
    team = normalize_team(team_raw) if team_raw else None
    projection = market.get('projection')

    comparison = market.get('comparison', {})
    rows = []
    for book_slug, book_data in comparison.items():
        rows.append({
            'category': category,
            'player_name': player_name,
            'team': team,
            'book': book_slug,
            'line': book_data.get('value'),
            'over_odds': book_data.get('over'),
            'under_odds': book_data.get('under'),
            'site_projection': projection,
            'available': book_data.get('available'),
        })
    return rows


def _save(rows: list) -> None:
    """Write everything collected so far. Called every SAVE_EVERY markets
    rather than after each one: the whole gzip is rewritten each time, so
    saving per row made the run slower the longer it went."""
    os.makedirs(DATA_DIR, exist_ok=True)
    pd.DataFrame(rows, columns=OUT_COLUMNS).to_csv(OUTPUT_FILE, index=False, compression='gzip')


def main(input_path: str, resume: bool, per_player: bool):
    """
    By DEFAULT every prop is re-fetched from scratch, since you usually want
    current lines rather than whatever was cached (unlike, say, historical
    PFR stats, which are fixed once a game is played). --resume continues an
    interrupted run instead, skipping (event, category) pairs already saved.

    Results are saved every SAVE_EVERY markets, so a crash costs at most that
    much work.
    """
    if not os.path.exists(input_path):
        print(f"ERROR: {input_path} not found.")
        return

    df = pd.read_csv(input_path)
    if 'event_id' not in df.columns:
        print("ERROR: input file has no event_id column — re-run "
              "scrape_scoresandodds_props.py (updated to capture it) first.")
        return
    df = df.dropna(subset=['event_id'])

    if per_player:
        return _main_per_player(df, resume)

    pairs = list(df[['event_id', 'category']].drop_duplicates().itertuples(index=False, name=None))
    print(f"{len(df)} props -> {len(pairs)} (event, category) requests")

    rows, done = [], set()
    if resume and os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        rows = existing.to_dict('records')
        done = set(zip(existing['player_name'], existing['category']))
        print(f"--resume: {len(existing):,} rows already on disk")

    fetched = 0
    for i, (event_id, category) in enumerate(pairs, 1):
        if resume and any((p, category) in done for p in
                          df[(df['event_id'] == event_id) & (df['category'] == category)]['player_name']):
            continue
        new = fetch_market(event_id, category)
        rows.extend(new)
        fetched += 1
        print(f"  [{i}/{len(pairs)}] {category} ({event_id}): {len(new)} book quotes")
        if fetched % SAVE_EVERY == 0:
            _save(rows)
        time.sleep(SLEEP_SEC)

    _save(rows)
    players = len({(r['player_name'], r['category']) for r in rows})
    print(f"\nDone. {len(rows):,} rows covering {players:,} player-props -> {OUTPUT_FILE}")


def _main_per_player(df: pd.DataFrame, resume: bool):
    """The original one-request-per-player run, kept as a fallback."""
    print(f"{len(df)} props with a usable event_id (per-player mode)")
    rows, done = [], set()
    if resume and os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        rows = existing.to_dict('records')
        done = set(zip(existing['player_name'], existing['category']))
        print(f"--resume: {len(existing):,} rows already on disk")

    fetched = 0
    for i, row in enumerate(df.itertuples(index=False), 1):
        if resume and (row.player_name, row.category) in done:
            continue
        print(f"  [{i}/{len(df)}] {row.player_name} ({row.category})")
        rows.extend(fetch_comparison(row.event_id, row.category, row.player_name))
        fetched += 1
        if fetched % SAVE_EVERY == 0:
            _save(rows)
        time.sleep(SLEEP_SEC)

    _save(rows)
    print(f"\nDone. {len(rows):,} total rows -> {OUTPUT_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_props_all.csv.gz")
    parser.add_argument("--per-player", action="store_true",
                        help="Use the old one-request-per-player form (1,689 requests "
                             "instead of 224). Fallback only.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip (player, category) pairs already in the existing "
                             "output file, instead of the default full refresh. Use "
                             "this to continue an interrupted run; omit it when you "
                             "specifically want current lines (they change over time).")
    args = parser.parse_args()
    main(args.input, args.resume, args.per_player)
