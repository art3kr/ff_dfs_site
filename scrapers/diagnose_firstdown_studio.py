"""
scrapers/diagnose_firstdown_studio.py
----------------------------------------
Diagnostic for firstdown.studio's season-long Vegas prop rankings pages,
run BEFORE building the real scraper — same approach used successfully
throughout this project (PFR, FantasyPros): get real HTML evidence
first, rather than guess at page structure and risk silently wrong
data.

We only have web_fetch's markdown-rendered view of these pages so far,
which shows real table DATA but not the underlying HTML tags/classes/
attributes needed to build a reliable BeautifulSoup parser. This script
gets that missing piece.

Usage:
    python scrapers/diagnose_firstdown_studio.py
    python scrapers/diagnose_firstdown_studio.py --position wr
"""

import argparse
import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}


def main(position: str):
    url = f"https://www.firstdown.studio/season-rankings/{position}" if position != 'qb' \
        else "https://www.firstdown.studio/season-rankings"
    print(f"Fetching: {url}\n")

    r = requests.get(url, headers=HEADERS, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Content-Type: {r.headers.get('content-type')}")
    print(f"Response length: {len(r.text)} chars\n")

    if r.status_code != 200:
        print("Non-200 response — dumping first 1000 chars of body:")
        print(r.text[:1000])
        return

    soup = BeautifulSoup(r.content, 'lxml')

    # Does this page use real <table> elements, or a CSS-grid/div-based
    # layout? This determines the whole parsing strategy.
    tables = soup.find_all('table')
    print(f"Real <table> elements found: {len(tables)}")

    if tables:
        table = tables[0]
        print(f"First table's id: {table.get('id')!r}, class: {table.get('class')!r}\n")

        rows = table.find_all('tr')
        print(f"Rows in first table: {len(rows)}\n")

        # Confirmed via live output: there are 4 header rows (0-3), not 3
        # — row 3 still showed 'Player'/'Pts' header text, not real data.
        # Row 4 is the first genuine player data row.
        for i, row in enumerate(rows[4:9], start=4):
            print(f"{'='*80}")
            print(f"ROW {i}")
            print(f"{'='*80}")
            cells = row.find_all(['td', 'th'])
            for j, cell in enumerate(cells):
                print(f"  cell[{j}]: tag={cell.name} class={cell.get('class')!r}")
                links = cell.find_all('a')
                for link in links:
                    print(f"      <a href='{link.get('href')}'>{link.get_text(strip=True)!r}</a>")
                print(f"      text: {cell.get_text(strip=True)!r}")
            print()

        # Deep-dump the raw HTML of the player-name cell for the FIRST
        # REAL data row (row 4, cell[1]) — this is what we actually need
        # to see to understand the initials/short-name/full-name/team
        # concatenation pattern observed in the rendered text.
        if len(rows) > 4:
            first_data_row = rows[4]
            cells = first_data_row.find_all(['td', 'th'])
            if len(cells) > 1:
                print(f"{'='*80}")
                print(f"RAW HTML of the player-name cell (row 4, cell[1] — first real data row):")
                print(f"{'='*80}")
                print(str(cells[1]))
    else:
        # No real tables — likely a div/CSS-grid layout. Look for
        # repeating row-like containers instead.
        print("No <table> elements — this is likely a div-based grid layout.")
        print("Searching for likely player-row containers...\n")

        # Look for elements with many repeated sibling structures
        # (common pattern: each row is a div with a consistent class)
        candidates = {}
        for el in soup.find_all(True, class_=True):
            classes = tuple(el.get('class', []))
            if classes:
                candidates.setdefault(classes, []).append(el)

        # Show the class combos that repeat most often (likely = rows)
        sorted_candidates = sorted(candidates.items(), key=lambda x: -len(x[1]))
        print("Most-repeated class combinations on the page (likely candidates for 'one row'):")
        for classes, elements in sorted_candidates[:10]:
            print(f"  {len(elements):>4}x  class={list(classes)}")

        # Dump the raw HTML of the single most-repeated element, so we
        # can see its real internal structure
        if sorted_candidates:
            most_common_classes, most_common_elements = sorted_candidates[0]
            print(f"\n{'='*80}")
            print(f"RAW HTML of first element matching the most-repeated class combo:")
            print(f"{'='*80}")
            print(str(most_common_elements[0])[:2000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--position", default="qb", choices=["qb", "rb", "wr", "te", "flex"])
    args = parser.parse_args()
    main(args.position)
