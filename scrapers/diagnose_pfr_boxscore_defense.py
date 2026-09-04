"""
scrapers/diagnose_pfr_boxscore_defense.py
---------------------------------------------
Checks whether an individual PFR game boxscore page has a per-player
(or per-team) DEFENSIVE stats table — sacks, INTs, fumble recoveries,
etc. — for that specific game. We already know boxscore pages have
'scoring' and 'player_offense' tables (used for game_info scraping);
this checks for a 'player_defense' table (or similarly named) we
haven't looked for yet.

Why this matters: FantasyPros' DST stats page (scrape_dst_fantasy_stats.py)
is confirmed CURRENT-SEASON-ONLY — no year parameter support. If a
per-game defensive table exists on the boxscore page itself, we could
aggregate individual defensive players' stats to team level for ANY
historical year (2014-2025), since we already have full access to every
year's boxscore URLs via the schedule data.

Usage:
    python scrapers/diagnose_pfr_boxscore_defense.py
    python scrapers/diagnose_pfr_boxscore_defense.py --boxscore /boxscores/202409050kan.htm
"""

import os
import sys
import argparse
from bs4 import BeautifulSoup, Comment

sys.path.insert(0, os.path.dirname(__file__))
from scrape_pfr import make_session, pfr_get, PFR_BASE

DEFAULT_BOXSCORE = "/boxscores/202409050kan.htm"   # 2024 Week 1, Chiefs @ Ravens
# (a completed, ordinary regular-season game — good baseline case)


def main(boxscore_path: str):
    url = PFR_BASE + boxscore_path
    print(f"Fetching: {url}\n")

    with make_session() as session:
        r = pfr_get(session, url, use_headers=False)

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

    # Look specifically for anything defense-related
    keywords = ['defense', 'def']
    candidates = [(t, loc) for t, loc in all_tables
                 if t.get('id') and any(kw in t.get('id').lower() for kw in keywords)]

    if not candidates:
        print("No table id contains 'defense'/'def'.")
        print("Dumping ALL table ids found (live + commented) for manual review:")
        for t, loc in all_tables:
            print(f"  [{loc}] id='{t.get('id')}'")
        return

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
            if shown >= 3:
                break
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--boxscore", default=DEFAULT_BOXSCORE,
                        help="PFR boxscore path, e.g. /boxscores/202409050kan.htm")
    args = parser.parse_args()
    main(args.boxscore)
