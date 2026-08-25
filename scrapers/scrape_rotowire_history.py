"""
scrapers/scrape_rotowire_history.py
------------------------------------
Backfills DraftKings salary data from RotoWire for seasons 2022-2025.
(RotoGuru covered 2014-2021; this fills the gap.)

What this provides per player per week:
  dk_salary, projected_pts, ownership_pct
  (dk_pts_scored = None here; actual points come from PFR scraper)

Strategy for finding slate IDs:
  - RotoWire slate IDs are sequential integers
  - Known anchors: ID ~3600 ≈ Sep 2022, ID ~9500 ≈ Sep 2025
  - For each NFL week, search a small window around a running estimate
  - Verify each candidate using the dfs-opportunities page (same as live scraper)
  - Save confirmed (week, year, slate_id) pairs to a local catalog so we
    never re-search a week we've already found

Output: data/rotowire_dk_2022_2025.csv.gz
        Same columns as rotoguru_dk_2014_2021.csv.gz for easy concatenation.

Run:
    python scrapers/scrape_rotowire_history.py
    python scrapers/scrape_rotowire_history.py --years 2024-2025
    python scrapers/scrape_rotowire_history.py --find-ids-only   # just build ID catalog
"""

import os
from dotenv import load_dotenv
load_dotenv()
import re
import json
import time
import argparse
import requests
import urllib3
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR     = os.path.join(os.path.dirname(__file__), '..', 'data')
SCHED_DIR    = os.path.join(DATA_DIR, 'schedules')
OUTPUT_FILE  = os.path.join(DATA_DIR, 'rotowire_dk_2022_2025.csv.gz')
CATALOG_FILE = os.path.join(DATA_DIR, 'rotowire_slate_catalog.csv')  # week→slate_id map
SLEEP_SEC    = 1.5

CHECK_URL = ("https://www.rotowire.com/daily/nfl/dfs-opportunities.php"
             "?site=DraftKings&slateID={slate_id}")
DATA_URL  = ("https://www.rotowire.com/daily/tables/value-report-nfl.php"
             "?siteID=2&slateID={slate_id}&projSource=RotoWire&oshipSource=RotoWire")

# Known slate ID anchors (week 1 of each season, approximate)
# Used to seed the search window — adjust if RotoWire changes numbering
ANCHOR_IDS = {
    2022: 3550,   # ~Sep 2022 Week 1
    2023: 5400,   # ~Sep 2023 Week 1
    2024: 7400,   # ~Sep 2024 Week 1
    2025: 9200,   # ~Sep 2025 Week 1
}
SEARCH_WINDOW = 300   # search ±300 IDs around each anchor/last-found ID

COLUMNS = [
    'week', 'year', 'name', 'name_normalized', 'position',
    'team', 'opponent', 'dk_salary', 'dk_pts_scored',
    'projected_pts', 'ownership_pct', 'source',
]

# ---------------------------------------------------------------------------
# Auth cookies
# RotoWire requires a logged-in session to return the full player list.
# Set these as environment variables before running — never hardcode them.
#
# How to get them:
#   1. Log into rotowire.com in Chrome
#   2. DevTools (F12) → Application → Cookies → www.rotowire.com
#   3. Copy the values for PHPSESSID and rw_tsd
#   4. Export before running:
#        export RW_PHPSESSID=your_value
#        export RW_TSD=your_value
#      Or inline:
#        RW_PHPSESSID=xxx RW_TSD=yyy python scrapers/scrape_rotowire_history.py
#
# Cookies expire periodically. If you start getting 1 player per slate again,
# grab fresh values from your browser and re-export.
# ---------------------------------------------------------------------------

RW_PHPSESSID = os.environ.get("RW_PHPSESSID", "")
RW_TSD       = os.environ.get("RW_TSD", "")

def _cookie_string() -> str:
    parts = []
    if RW_PHPSESSID:
        parts.append(f"PHPSESSID={RW_PHPSESSID}")
    if RW_TSD:
        parts.append(f"rw_tsd={RW_TSD}")
    return "; ".join(parts)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())

def _float(s) -> float:
    try:
        return float(str(s).replace('$','').replace(',','').strip())
    except:
        return 0.0

def _int(s) -> int:
    try:
        return int(float(str(s).replace('$','').replace(',','').strip()))
    except:
        return 0

def _headers() -> dict:
    h = {
        'User-Agent':       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
        'Accept':           'application/json, text/javascript, */*; q=0.01',
        'Accept-Language':  'en-US,en;q=0.9',
        'Accept-Encoding':  'gzip, deflate, br',
        'Referer':          'https://www.rotowire.com/daily/nfl/value-report.php',
        'X-Requested-With': 'XMLHttpRequest',   # RotoWire API expects AJAX requests
        'DNT':              '1',
        'Connection':       'keep-alive',
    }
    cookie = _cookie_string()
    if cookie:
        h['Cookie'] = cookie
    else:
        print("WARNING: No RotoWire cookies set. Will likely get 1 player per slate.")
        print("  Set RW_PHPSESSID and RW_TSD environment variables.")
    return h

# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------

def load_week_dates(year: int) -> dict:
    """
    Returns {week: (thursday_date, monday_date)} — the Thu-Mon window
    for each NFL week, derived from the local schedule CSV.
    """
    path = os.path.join(SCHED_DIR, f'{year}_schedule_df.csv')
    if not os.path.exists(path):
        return {}

    df = pd.read_csv(path)
    df = df[df['team_2'] != 'BYE']
    df = df[df['boxscore_url'].notna() & (df['boxscore_url'] != '')]
    df['week'] = pd.to_numeric(df['week'], errors='coerce')
    df = df[df['week'].notna() & (df['week'] <= 18)].copy()
    df['week'] = df['week'].astype(int)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')

    ranges = {}
    for week, grp in df.groupby('week'):
        dates = grp['date'].dropna()
        if not dates.empty:
            # Thu slate starts 3 days before Sunday (earliest game)
            thu = dates.min() - timedelta(days=3)
            mon = dates.max() + timedelta(days=1)
            ranges[week] = (thu, mon)
    return ranges

# ---------------------------------------------------------------------------
# Slate ID catalog
# ---------------------------------------------------------------------------

def load_catalog() -> dict:
    """Returns {(year, week): slate_id} from the saved catalog file."""
    if not os.path.exists(CATALOG_FILE):
        return {}
    df = pd.read_csv(CATALOG_FILE)
    return {(int(r.year), int(r.week)): int(r.slate_id)
            for _, r in df.iterrows()}

def save_catalog(catalog: dict):
    rows = [{'year': y, 'week': w, 'slate_id': sid}
            for (y, w), sid in sorted(catalog.items())]
    pd.DataFrame(rows).to_csv(CATALOG_FILE, index=False)

# ---------------------------------------------------------------------------
# Slate verification (same logic as live scraper)
# ---------------------------------------------------------------------------

def check_slate(slate_id: int) -> tuple[bool, str]:
    """
    Returns (is_thu_mon_classic, slate_title).
    Checks the dfs-opportunities page for this slate ID.
    """
    url = CHECK_URL.format(slate_id=slate_id)
    try:
        r = requests.get(url, headers=_headers(), verify=False, timeout=10)
    except requests.RequestException:
        return False, ""

    if r.status_code != 200:
        return False, ""

    soup  = BeautifulSoup(r.text, 'html.parser')
    title = soup.find('div', class_='page-title__secondary')
    if title:
        text = title.get_text(strip=True)
        is_classic = ('Thu' in text or 'thu' in text.lower()) and \
                     ('Classic' in text or 'classic' in text.lower())
        return is_classic, text

    # Fallback: look for the slate name in the page title
    h1 = soup.find('h1')
    if h1:
        text = h1.get_text(strip=True)
        return 'thu' in text.lower() or 'classic' in text.lower(), text

    return False, ""

# ---------------------------------------------------------------------------
# Find Thu-Mon Classic slate ID for a given (year, week)
# ---------------------------------------------------------------------------

def find_slate_id(year: int, week: int, week_dates: dict,
                  hint_id: int) -> int | None:
    """
    Search around `hint_id` for the Thu-Mon Classic slate matching this week.
    Uses the week's date range to confirm the match once a Classic slate is found.
    Returns the slate_id or None.
    """
    thu, mon = week_dates.get(week, (None, None))
    if thu is None:
        return None

    print(f"    Searching near ID {hint_id} (window ±{SEARCH_WINDOW})...")

    # Search upward first (more likely for sequential IDs going forward),
    # then downward
    candidates = (list(range(hint_id, hint_id + SEARCH_WINDOW)) +
                  list(range(hint_id - 1, hint_id - SEARCH_WINDOW, -1)))

    for slate_id in candidates:
        if slate_id <= 0:
            continue

        is_classic, title = check_slate(slate_id)
        time.sleep(0.3)   # light rate limiting during search

        if not is_classic:
            continue

        # Found a Classic slate — verify it's for our week by checking
        # whether any players are returned (non-empty means active slate data)
        # For historical slates the page title may include a date
        # e.g. "Classic contest - Thu-Mon slate - 16 games - Sep 8, 1:00 PM"
        date_match = _slate_date_in_range(title, thu, mon)

        if date_match:
            print(f"    Found: slate {slate_id} '{title}'")
            return slate_id

        # If no date in title, accept the first Classic slate we find
        # near the hint (for older slates without dates in the title)
        if 'classic' in title.lower():
            print(f"    Found (no date verify): slate {slate_id} '{title}'")
            return slate_id

    return None


def _slate_date_in_range(title: str, thu: datetime, mon: datetime) -> bool:
    """
    Try to parse a month/day from the slate title and check if it falls
    within the Thu-Mon window for this week.
    e.g. "Sep 8" → datetime(year, 9, 8)
    """
    months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
              'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}

    m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})', title)
    if not m:
        return False   # can't verify — caller decides

    month_num = months.get(m.group(1), 0)
    day       = int(m.group(2))
    year      = thu.year if month_num >= 9 else thu.year + 1

    try:
        slate_date = datetime(year, month_num, day)
        return thu - timedelta(days=1) <= slate_date <= mon + timedelta(days=1)
    except:
        return False

# ---------------------------------------------------------------------------
# Fetch player data for one slate
# ---------------------------------------------------------------------------

def fetch_slate_players(slate_id: int, week: int, year: int) -> list[dict]:
    url = DATA_URL.format(slate_id=slate_id)
    try:
        r = requests.get(url, headers=_headers(), verify=False, timeout=15)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"    Fetch error slate {slate_id}: {e}")
        return []

    if not raw:
        return []

    players = []
    for item in raw:
        name = str(item.get('player', '') or item.get('name', '')).strip()
        if not name:
            continue

        salary_raw = str(item.get('salary', '0')).replace('$','').replace(',','').strip()
        salary     = _int(salary_raw)
        if salary == 0:
            continue

        players.append({
            'week':            week,
            'year':            year,
            'name':            name,
            'name_normalized': normalize_name(name),
            'position':        str(item.get('position', '')).strip().upper(),
            'team':            str(item.get('team', '')).strip().lower(),
            'opponent':        str(item.get('opp', '')).strip().lower(),
            'dk_salary':       salary,
            'dk_pts_scored':   None,   # filled by joining with PFR
            'projected_pts':   _float(item.get('proj', 0)),
            'ownership_pct':   _float(item.get('ownership', 0)),
            'source':          'rotowire',
        })

    return players

# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def _save(rows: list):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=COLUMNS)
    df = df.drop_duplicates(subset=['year','week','name','position'])
    df = df.sort_values(['year','week','position','dk_salary'],
                        ascending=[True,True,True,False])
    df.to_csv(OUTPUT_FILE, index=False, compression='gzip')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(years: list[int], find_ids_only: bool = False):
    os.makedirs(DATA_DIR, exist_ok=True)

    # Load existing output + slate catalog
    catalog = load_catalog()
    already_done: set[tuple] = set()
    all_rows = []

    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        all_rows = existing.to_dict('records')
        for _, r in existing[['year','week']].drop_duplicates().iterrows():
            already_done.add((int(r.year), int(r.week)))
        print(f"Loaded {len(existing):,} existing rows, "
              f"{len(already_done)} (year,week) pairs done")

    for year in years:
        print(f"\n{'='*50}\nYEAR {year}\n{'='*50}")

        week_dates = load_week_dates(year)
        if not week_dates:
            print(f"  No schedule for {year}")
            continue

        # Running hint: start from the year anchor, advance as we find IDs
        hint = ANCHOR_IDS.get(year, 5000)

        for week in range(1, max(week_dates.keys()) + 1):
            if week not in week_dates:
                continue

            if (year, week) in already_done and not find_ids_only:
                print(f"  W{week:02d}: already done, skip")
                continue

            # Check if we already have this slate ID in the catalog
            slate_id = catalog.get((year, week))

            if slate_id is None:
                print(f"  W{week:02d}: searching for slate ID...")
                slate_id = find_slate_id(year, week, week_dates, hint)
                if slate_id:
                    catalog[(year, week)] = slate_id
                    save_catalog(catalog)
                    hint = slate_id + 1   # next week's ID will be slightly higher
                else:
                    print(f"  W{week:02d}: slate not found — skipping")
                    continue
            else:
                print(f"  W{week:02d}: using catalog slate {slate_id}")
                hint = slate_id + 1

            if find_ids_only:
                continue

            if (year, week) in already_done:
                continue

            print(f"  W{week:02d}: fetching players from slate {slate_id}...", end='', flush=True)
            players = fetch_slate_players(slate_id, week, year)

            if players:
                print(f" {len(players)} players")
                all_rows.extend(players)
                already_done.add((year, week))
                _save(all_rows)
            else:
                print(" (empty)")

            time.sleep(SLEEP_SEC)

    if not find_ids_only and all_rows:
        final = pd.read_csv(OUTPUT_FILE)
        print(f"\nDone. {len(final):,} rows → {OUTPUT_FILE}")

    print(f"Slate catalog: {len(catalog)} entries → {CATALOG_FILE}")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--years', default='2022-2025')
    parser.add_argument('--find-ids-only', action='store_true',
                        help='Only build the slate ID catalog, do not fetch player data')
    args = parser.parse_args()

    if '-' in args.years:
        s, e = args.years.split('-')
        years = list(range(int(s), int(e)+1))
    elif ',' in args.years:
        years = [int(y) for y in args.years.split(',')]
    else:
        years = [int(args.years)]

    print(f"Years: {years}")
    main(years, find_ids_only=args.find_ids_only)
