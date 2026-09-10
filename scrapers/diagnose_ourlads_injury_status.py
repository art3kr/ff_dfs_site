"""
scrapers/diagnose_ourlads_injury_status.py
--------------------------------------------------
Checks how Ourlads encodes injury status on their depth chart pages —
confirmed real player report: Ourlads shows an injured player in red
text, likely OUT specifically, on the same page/rows the existing
depth chart scraper already reads. If real, this is a genuinely
better source than draftedge.com for OUT status specifically (closer
to game day, since depth charts get updated as teams' own injury
reports come out), and worth preferring over draftedge for whatever
Ourlads actually captures.

This dumps the raw HTML of Ourlads' depth chart page directly, since
the existing depth chart scraper may already be discarding a color/
class attribute it doesn't currently use — the fastest path here is
confirming what's actually in the HTML we're already fetching, not
assuming a new page/source is needed.

Usage:
    python scrapers/diagnose_ourlads_injury_status.py [--team buf]
"""

import argparse
import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}


def main(team: str):
    # Matches the URL pattern the existing depth chart scraper uses —
    # if this guess is wrong, check scrape_ourlads_depth_charts.py's
    # own URL construction directly.
    url = f"https://www.ourlads.com/nfldepthcharts/depthchart/{team.upper()}"
    print(f"Fetching: {url}\n")

    r = requests.get(url, headers=HEADERS, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Response length: {len(r.text)} chars\n")

    if r.status_code != 200:
        print(r.text[:1000])
        return

    soup = BeautifulSoup(r.content, 'html.parser', from_encoding='utf-8')

    # Look for anything color-related first - inline red styles, or a
    # CSS class with "injur", "red", "out" in the name.
    red_inline = soup.find_all(style=lambda s: s and 'red' in s.lower())
    print(f"Elements with inline red styling: {len(red_inline)}")
    for el in red_inline[:5]:
        print(f"  {str(el)[:300]}")

    injury_classed = soup.find_all(class_=lambda c: c and any(
        kw in cls.lower() for cls in c for kw in ('injur', 'out', 'red', 'status')
    ))
    print(f"\nElements with an injury/status/red/out-related class: {len(injury_classed)}")
    for el in injury_classed[:5]:
        print(f"  {str(el)[:300]}")

    # Also dump one full player-row area so the real row structure is
    # visible even if the above searches miss something (e.g. color
    # applied via an external stylesheet rule keyed on a generic class
    # rather than inline style or an obviously-named class).
    tables = soup.find_all('table')
    print(f"\n<table> elements found: {len(tables)}")
    if tables:
        rows = tables[0].find_all('tr')
        print(f"Rows in first table: {len(rows)}")
        print(f"\nFull HTML of first 5 rows:")
        for row in rows[:5]:
            print(str(row))
            print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", default="buf", help="Team code to check, e.g. buf")
    args = parser.parse_args()
    main(args.team)
