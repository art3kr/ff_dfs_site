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

Response JSON's relevant shape:
    {
      "markets": [{
        "player": {"first_name": ..., "last_name": ..., "team": {"key": ...}},
        "projection": <site's consensus projection>,
        "comparison": {
          "<book_slug>": {"value": <line>, "over": <odds>, "under": <odds>, ...},
          ...
        }
      }]
    }
One row per (player, category, book) is written — so a single scraped
prop with 9 books comparison becomes 9 output rows, each with that
book's own line and odds side by side.

Requires event_id from scrape_scoresandodds_props.py's output (added
specifically to support this script) — run that first.

Output: data/scoresandodds_market_comparison.csv.gz
  Columns: category, player_name, team, book, line, over_odds,
           under_odds, site_projection

Usage:
    python scrapers/scrape_scoresandodds_market_comparison.py \\
        --input data/scoresandodds_props_all.csv.gz
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
               'line', 'over_odds', 'under_odds', 'site_projection']


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
        })
    return rows


def _save(existing: pd.DataFrame, new_rows: list) -> pd.DataFrame:
    if not new_rows:
        return existing
    new_df = pd.DataFrame(new_rows, columns=OUT_COLUMNS)
    combined = pd.concat([existing, new_df], ignore_index=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    combined.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    return combined


def main(input_path: str, resume: bool):
    """
    Two different needs, handled separately since lines change over
    time (unlike e.g. historical PFR stats, which are fixed once a
    game is played):
      - Interruption safety: results are now saved incrementally after
        EVERY row, not just once at the very end — previously, a crash
        or cancel partway through lost everything already fetched, not
        just wasted the time spent so far.
      - Refresh vs. resume: by DEFAULT, every prop is re-fetched from
        scratch each run, since you'll usually want current lines, not
        whatever was cached from before. Pass --resume if you
        specifically want to continue an interrupted run without
        re-fetching what it already got (e.g. picking back up after a
        crash) rather than doing a full refresh.
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
    print(f"{len(df)} props with a usable event_id")

    if resume and os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        done_pairs = set(zip(existing['player_name'], existing['category']))
        print(f"--resume: loaded existing file with {len(existing):,} rows, "
              f"{len(done_pairs)} (player, category) pairs already done")
    else:
        if resume:
            print("--resume passed but no existing output file found — doing a full run")
        existing = pd.DataFrame(columns=OUT_COLUMNS)
        done_pairs = set()

    skipped = 0
    for i, row in df.iterrows():
        pair = (row['player_name'], row['category'])
        if resume and pair in done_pairs:
            skipped += 1
            continue

        print(f"  [{i+1}/{len(df)}] {row['player_name']} ({row['category']})")
        rows = fetch_comparison(row['event_id'], row['category'], row['player_name'])
        existing = _save(existing, rows)
        time.sleep(SLEEP_SEC)

    if skipped:
        print(f"\nSkipped {skipped} already-done (player, category) pairs (--resume)")

    print(f"\nDone. {len(existing):,} total rows -> {OUTPUT_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_props_all.csv.gz")
    parser.add_argument("--resume", action="store_true",
                        help="Skip (player, category) pairs already in the existing "
                             "output file, instead of the default full refresh. Use "
                             "this to continue an interrupted run; omit it when you "
                             "specifically want current lines (they change over time).")
    args = parser.parse_args()
    main(args.input, args.resume)
