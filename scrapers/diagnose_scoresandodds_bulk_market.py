"""
scrapers/diagnose_scoresandodds_bulk_market.py
--------------------------------------------------
Confirms the shape of the market-comparison API when the `filter` (player)
parameter is LEFT OFF, before scrape_scoresandodds_market_comparison.py is
rewritten to use it.

The existing scraper asks for one player at a time:
    ?event={event_id}&market={market name}&filter={player name}
which is 1,689 requests (~34 minutes) for a full slate. Without `filter`
the same endpoint appears to return every player in that market for that
game in one request, which would be ~224 requests (~4.5 minutes).

"Appears to" is why this script exists. It checks, per (event, category):
  - the request works, and how long it takes
  - how many player-markets come back
  - how that compares to the players the per-player scrape knows about
    (scoresandodds_props_all.csv.gz), and names the ones each side is
    missing
  - which fields each market and each book quote carries, so the parser
    can be written against confirmed keys rather than guesses
  - whether any category needs a different market name than the existing
    CATEGORY_SLUG_TO_MARKET_NAME map

It writes one raw response to data/_diagnostics/ so the JSON can be read by
eye, and prints a per-category table.

Usage:
    python scrapers/diagnose_scoresandodds_bulk_market.py
    python scrapers/diagnose_scoresandodds_bulk_market.py --events 3
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(__file__))
from scrape_scoresandodds_market_comparison import (API_BASE, HEADERS,
                                                    CATEGORY_SLUG_TO_MARKET_NAME)

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
OUT_DIR = os.path.join(DATA_DIR, '_diagnostics')


def fetch_market(event_id: str, market_name: str):
    """One (event, market) request with no player filter. Returns
    (status_code, seconds, parsed_json_or_None)."""
    params = {'event': event_id, 'market': market_name}
    start = time.time()
    try:
        r = requests.get(API_BASE, params=params, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        return None, time.time() - start, {'error': str(e)}
    elapsed = time.time() - start
    if r.status_code != 200:
        return r.status_code, elapsed, None
    try:
        return r.status_code, elapsed, r.json()
    except ValueError:
        return r.status_code, elapsed, None


def player_name_of(market: dict) -> str:
    """The display name, however this response spells it."""
    p = market.get('player') or {}
    first, last = p.get('first_name'), p.get('last_name')
    if first and last:
        return f'{first} {last}'
    return p.get('name') or market.get('description') or '?'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--props', default=os.path.join(DATA_DIR, 'scoresandodds_props_all.csv.gz'))
    ap.add_argument('--events', type=int, default=2, help='How many games to probe (default 2).')
    ap.add_argument('--sleep', type=float, default=1.0)
    args = ap.parse_args()

    props = pd.read_csv(args.props).dropna(subset=['event_id'])
    events = list(props['event_id'].drop_duplicates())[:args.events]
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f'{len(props)} props across {props["event_id"].nunique()} games; '
          f'probing {len(events)} game(s), all {len(CATEGORY_SLUG_TO_MARKET_NAME)} categories\n')

    market_keys, book_keys, saved_sample = Counter(), Counter(), False
    total_requests = total_seconds = 0
    rows = []

    for event in events:
        for slug, market_name in CATEGORY_SLUG_TO_MARKET_NAME.items():
            status, elapsed, data = fetch_market(event, market_name)
            total_requests += 1
            total_seconds += elapsed
            markets = (data or {}).get('markets', []) if isinstance(data, dict) else []
            bulk_names = {player_name_of(m) for m in markets}

            known = set(props[(props['event_id'] == event) & (props['category'] == slug)]['player_name'])
            for m in markets:
                market_keys.update(m.keys())
                for quote in (m.get('comparison') or {}).values():
                    book_keys.update(quote.keys())

            if markets and not saved_sample:
                path = os.path.join(OUT_DIR, f'bulk_market_sample_{slug}.json')
                with open(path, 'w') as f:
                    json.dump(markets[0], f, indent=2)
                print(f'raw sample of one market entry -> {path}\n')
                saved_sample = True

            rows.append({
                'event': event.split('/')[-1], 'category': slug, 'status': status,
                'secs': round(elapsed, 2), 'bulk_players': len(markets),
                'props_all_players': len(known),
                'missing_from_bulk': ', '.join(sorted(known - bulk_names))[:60],
                'extra_in_bulk': len(bulk_names - known),
                'books': max((len(m.get('comparison') or {}) for m in markets), default=0),
            })
            time.sleep(args.sleep)

    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
    print()
    bad = df[(df['status'] != 200) | ((df['props_all_players'] > 0) & (df['bulk_players'] == 0))]
    missing = df[df['missing_from_bulk'] != '']
    print(f'requests: {total_requests}, average {total_seconds / max(total_requests, 1):.2f}s')
    print(f'categories returning nothing while props_all has players: {len(bad)}')
    if len(bad):
        print(bad[['event', 'category', 'status', 'bulk_players', 'props_all_players']].to_string(index=False))
    print(f'(event, category) pairs where bulk MISSED a player the per-player scrape had: {len(missing)}')
    if len(missing):
        print(missing[['event', 'category', 'missing_from_bulk']].to_string(index=False))
    print(f'\nextra players bulk found that props_all did not: {int(df["extra_in_bulk"].sum())}')
    print(f'\nmarket-level keys seen: {sorted(market_keys)}')
    print(f'per-book quote keys seen: {sorted(book_keys)}')
    full_slate = props.groupby(['event_id', 'category']).ngroups
    print(f'\nfull slate would be {full_slate} requests; at the measured average that is '
          f'{full_slate * (total_seconds / max(total_requests, 1)) / 60:.1f} minutes '
          f'(per-player today: {len(props)} requests)')


if __name__ == '__main__':
    main()
