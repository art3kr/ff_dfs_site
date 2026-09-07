"""
scrapers/diagnose_scoresandodds_game_odds.py
--------------------------------------------------
Confirmed real (Sept 2026, via web search + fetch): scoresandodds.com/nfl/odds
is a genuine PRE-GAME page listing spread/total/moneyline for every
upcoming game across multiple books — unlike hist_game_info (PFR),
which is post-game only and useless for setting a lineup before
kickoff.

The rendered/markdown view showed a table with two rows per game (one
per team) repeated three times (spread, then total, then moneyline)
sharing the same numeric row IDs (e.g. 451/452 for one game) — but
that's a markdown *rendering* of the page, not the real underlying
HTML. This dumps the actual raw structure so real parsing logic can be
built against evidence, not a converted view, matching how every
other scoresandodds scraper in this project got built.

Usage:
    python scrapers/diagnose_scoresandodds_game_odds.py
"""

import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

URL = "https://www.scoresandodds.com/nfl/odds"


def main():
    print(f"Fetching: {URL}\n")
    r = requests.get(URL, headers=HEADERS, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Response length: {len(r.text)} chars\n")

    if r.status_code != 200:
        print(r.text[:1000])
        return

    soup = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')

    li_rows = soup.find_all('li', attrs={'data-name': True})
    print(f"<li data-name=...> rows found (props-page-style): {len(li_rows)}")

    team_links = soup.find_all('a', href=lambda h: h and '/nfl/teams/' in h)
    print(f"Team links found (/nfl/teams/...): {len(team_links)}")

    if team_links:
        first_link = team_links[0]
        print(f"\n{'='*70}")
        print("First team link's surrounding structure (walking up 5 levels):")
        print(f"{'='*70}")
        container = first_link
        for _ in range(5):
            if container.parent:
                container = container.parent
        print(f"tag={container.name}, class={container.get('class')}, "
              f"data-*={[k for k in container.attrs if k.startswith('data-')]}")

        print(f"\n{'='*70}")
        print("FULL untruncated HTML of that container:")
        print(f"{'='*70}")
        print(str(container))

    print(f"\n{'='*70}")
    print("Scanning for elements with betting-related data-* attributes:")
    print(f"{'='*70}")
    candidates = soup.find_all(attrs={'data-market': True})
    print(f"Elements with data-market: {len(candidates)}")
    for c in candidates[:2]:
        print(f"  {str(c)[:300]}")


if __name__ == "__main__":
    main()
