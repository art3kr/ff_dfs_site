"""
scrapers/diagnose_draftedge_injuries.py
------------------------------------------
Confirmed via web_fetch (Sept 2026): draftedge.com/nfl/nfl-injury-report
is real, live, current-season data with exactly the fields needed
(player, position, team, status matching Out/Doubtful/Questionable/IR),
returned as a markdown-rendered table in that fetch. This dumps the
actual underlying HTML before any parsing logic gets written against
it — a markdown table view has hidden real structural surprises
several times already in this project (moneyline vs. line-shaped
props, "field" rows with fake player names instead of real ones), so
building final logic against the raw HTML directly, not a converted
view, matches how everything else in this project got built.

Usage:
    python scrapers/diagnose_draftedge_injuries.py
"""

import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

URL = "https://draftedge.com/nfl/nfl-injury-report/"


def main():
    print(f"Fetching: {URL}\n")
    r = requests.get(URL, headers=HEADERS, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Response length: {len(r.text)} chars\n")

    if r.status_code != 200:
        print(r.text[:1000])
        return

    soup = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')

    # The markdown view showed a clean pipe-table - look for an actual
    # <table> element first as the most likely real structure.
    tables = soup.find_all('table')
    print(f"<table> elements found: {len(tables)}")

    if tables:
        first_table = tables[0]
        rows = first_table.find_all('tr')
        print(f"Rows in first table: {len(rows)}")
        print(f"\n{'='*70}")
        print("FULL untruncated HTML of the first 3 rows:")
        print(f"{'='*70}")
        for row in rows[:3]:
            print(str(row))
            print()
    else:
        # Fall back to checking for a non-<table> structure (some sites
        # build "tables" out of divs for styling flexibility)
        print("No <table> found - checking for div-based row structures...")
        candidates = soup.find_all(attrs={'class': lambda c: c and any(
            'row' in cls.lower() or 'injury' in cls.lower() for cls in c
        )})
        print(f"Possible row-like elements found: {len(candidates)}")
        for c in candidates[:3]:
            print(str(c)[:500])
            print()


if __name__ == "__main__":
    main()
