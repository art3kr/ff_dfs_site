"""
scrapers/test_pfr_returns.py
------------------------------
Diagnoses the -6.0 point discrepancies found by compare_dk_points.py.
Every worst-offender row belonged to a known punt/kick return
specialist (Rashid Shaheed, Marvin Mims, Kalif Raymond, Xavier Gipson,
Tyler Lockett) — strongly suggesting the missing points are a return
touchdown, which isn't in the 'stats' table we currently scrape
(pass/rush/rec only). This checks what other tables exist on a known
returner's gamelog page and dumps rows from anything return-related.

Usage:
    python scrapers/test_pfr_returns.py
    python scrapers/test_pfr_returns.py --pfr-id ShahRa00
"""

import os
import sys
import argparse
from bs4 import BeautifulSoup, Comment

sys.path.insert(0, os.path.dirname(__file__))
from scrape_pfr import make_session, pfr_get, PFR_BASE

DEFAULT_PFR_ID = "ShahRa00"   # Rashid Shaheed — known punt returner


def main(pfr_id: str):
    url = f"{PFR_BASE}/players/{pfr_id[0]}/{pfr_id}/gamelog/"
    print(f"Fetching: {url}\n")

    with make_session() as session:
        r = pfr_get(session, url, use_headers=True)

    if r is None:
        print("Request failed — cf_clearance cookie likely expired.")
        return

    soup = BeautifulSoup(r.content, 'lxml')

    live_tables = soup.find_all('table')
    live_ids = [t.get('id') for t in live_tables if t.get('id')]
    print(f"Live table ids: {live_ids}")

    comments = soup.find_all(string=lambda text: isinstance(text, Comment))
    commented_tables = []
    for c in comments:
        c_soup = BeautifulSoup(str(c), 'lxml')
        commented_tables.extend(c_soup.find_all('table'))
    commented_ids = [t.get('id') for t in commented_tables if t.get('id')]
    print(f"Commented table ids: {commented_ids}\n")

    all_tables = [(t, 'LIVE') for t in live_tables] + [(t, 'COMMENTED') for t in commented_tables]

    # Look for anything that might contain return stats
    keywords = ['return', 'ret', 'special', 'kick', 'punt']
    candidates = [(t, loc) for t, loc in all_tables
                 if t.get('id') and any(kw in t.get('id').lower() for kw in keywords)]

    if not candidates:
        print("No table id contains 'return'/'ret'/'special'/'kick'/'punt'.")
        print("Dumping first row of EVERY table found, to see what's available:\n")
        candidates = all_tables

    for table, loc in candidates:
        tid = table.get('id', '(no id)')
        tbody = table.find('tbody')
        if not tbody:
            print(f"[{loc} / id={tid}] no <tbody>")
            continue
        print(f"{'='*80}")
        print(f"[{loc}] table id='{tid}'")
        print(f"{'='*80}")
        shown = 0
        for tr in tbody.find_all('tr'):
            if tr.get('class') and 'thead' in tr.get('class'):
                continue
            cells = tr.find_all(['td', 'th'])
            print(f"  row {shown+1}:")
            for cell in cells:
                stat = cell.get('data-stat', '(no data-stat)')
                text = cell.get_text(strip=True)
                print(f"    data-stat='{stat}' -> '{text}'")
            shown += 1
            if shown >= 2:
                break
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pfr-id", default=DEFAULT_PFR_ID)
    args = parser.parse_args()
    main(args.pfr_id)
