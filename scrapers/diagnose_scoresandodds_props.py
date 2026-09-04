"""
scrapers/diagnose_scoresandodds_props.py
--------------------------------------------
Checks the real HTML structure of scoresandodds.com/nfl/props (the
DEFAULT "Passing Yards" view specifically — confirmed real, live,
server-rendered data via a direct fetch, Sept 2026: player names,
matchups, multi-sportsbook over/under lines with odds, and a
consensus/projected value, all present without needing JS execution).

NOT YET CONFIRMED: how the other 17 category dropdown options
(Rushing Yards, Receptions, Touchdowns, etc.) are accessed — a search
for alternate URL patterns came back empty, suggesting this is a
client-side AJAX filter rather than separate fetchable pages. If you
have real evidence of the actual request (e.g. from browser DevTools'
Network tab when switching the dropdown), that changes everything
here — share it and this script gets rebuilt around real evidence
instead of guessing.

This script only targets the confirmed-working default view.

Usage:
    python scrapers/diagnose_scoresandodds_props.py
"""

import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

URL = "https://www.scoresandodds.com/nfl/props"


def main():
    print(f"Fetching: {URL}\n")
    r = requests.get(URL, headers=HEADERS, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Response length: {len(r.text)} chars\n")

    if r.status_code != 200:
        print(r.text[:1000])
        return

    soup = BeautifulSoup(r.content, 'html.parser')

    # The prop rows aren't in a plain <table> based on the rendered
    # markdown (looked like a list of cards) — find likely candidates
    # by looking for repeated elements containing a player link
    # (scoresandodds.com/prop-bets/{id}/{slug})
    player_links = soup.find_all('a', href=lambda h: h and '/prop-bets/' in h)
    print(f"Player prop links found: {len(player_links)}")

    if not player_links:
        print("No player links found — page structure may differ from what was fetched before.")
        return

    # Walk up from the first player link to find the repeating "row"
    # container, and dump its structure
    first_link = player_links[0]
    row_container = first_link
    for _ in range(6):   # walk up a few levels looking for a repeating pattern
        if row_container.parent:
            row_container = row_container.parent
        else:
            break

    print(f"\n{'='*70}")
    print(f"FULL untruncated HTML of the first row (a single <li>, not the whole module):")
    print(f"{'='*70}")
    first_row_li = first_link.find_parent('li')
    if first_row_li:
        print(str(first_row_li))
    else:
        print("Couldn't find a parent <li> — dumping full row_container instead:")
        print(str(row_container))

    print(f"\n{'='*70}")
    print(f"Also dumping a 2nd and 3rd row for consistency check:")
    print(f"{'='*70}")
    all_rows = soup.find_all('li', attrs={'data-name': True})
    print(f"Total <li data-name=...> rows found: {len(all_rows)}")
    for row in all_rows[1:3]:
        print(f"\n--- data-name='{row.get('data-name')}' ---")
        print(str(row))

    # The dropdown-class heuristic search came back empty last time —
    # try scanning <script> tags instead for any hardcoded endpoint
    # pattern related to switching markets/categories (e.g. an API
    # base URL or a market-name-to-endpoint mapping).
    print(f"\n{'='*70}")
    print("Scanning <script> tags for hints of the category-switch endpoint:")
    print(f"{'='*70}")
    for script in soup.find_all('script'):
        text = script.string or ''
        if any(hint in text.lower() for hint in ['market', '/api/', 'endpoint', 'ajax', 'fetch(']):
            # Print just the relevant-looking lines, not the whole
            # (possibly huge) script block
            for line in text.split('\n'):
                if any(hint in line.lower() for hint in ['market', '/api/', 'endpoint', 'ajax', 'fetch(']):
                    print(line.strip()[:200])


if __name__ == "__main__":
    main()
