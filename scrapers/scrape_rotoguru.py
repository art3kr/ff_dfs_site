"""
scrapers/scrape_rotoguru.py
---------------------------
One-time scrape of RotoGuru's historical DraftKings NFL salary + actual
fantasy points data for seasons 2014-2021.

Output: data/rotoguru_dk_2014_2021.csv.gz
        ~70,000 rows | columns: week, year, rg_id, name, position,
                                 team, home_away, opponent,
                                 dk_pts_scored, dk_salary

Run:
    python scrapers/scrape_rotoguru.py

Re-running is safe: skips years/weeks already in the output file.
Polite scraping: 2-second sleep between requests.
"""

import os
import re
import time
import requests
import pandas as pd
from io import StringIO

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_FILE  = os.path.join(os.path.dirname(__file__), '..', 'data', 'rotoguru_dk_2014_2021.csv.gz')
BASE_URL     = "http://rotoguru1.com/cgi-bin/fyday.pl"
SLEEP_SEC    = 2.0          # seconds between requests (be polite)
YEARS        = list(range(2014, 2022))   # 2014 through 2021 inclusive
# Week counts by season (17 weeks pre-2021, 18 from 2021 onward)
WEEKS_BY_YEAR = {y: 17 for y in range(2014, 2021)}
WEEKS_BY_YEAR[2021] = 18

SCSV_COLUMNS = ['week','year','rg_id','name','position','team','home_away','opponent','dk_pts_scored','dk_salary']

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_week(year: int, week: int) -> list[dict]:
    """
    Fetch one week of DK data from RotoGuru scsv endpoint.
    Returns list of dicts, one per player row.
    """
    params = {'week': week, 'year': year, 'game': 'dk', 'scsv': 1}
    try:
        r = requests.get(BASE_URL, params=params, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  Request error {year} W{week}: {e}")
        return []

    # The scsv data lives between <pre> tags in the HTML
    match = re.search(r'<pre>(.*?)</pre>', r.text, re.DOTALL | re.IGNORECASE)
    if not match:
        print(f"  No <pre> block found for {year} W{week}")
        return []

    raw = match.group(1).strip()
    if not raw:
        return []

    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(';')
        if len(parts) < 10:
            continue
        # Skip the header row ("Week;Year;GID;Name;...")
        if not parts[0].lstrip('-').isdigit():
            continue
        try:
            rows.append({
                'week':          int(parts[0]),
                'year':          int(parts[1]),
                'rg_id':         str(parts[2]).strip(),
                'name':          str(parts[3]).strip(),
                'position':      str(parts[4]).strip().upper(),
                'team':          str(parts[5]).strip().lower(),
                'home_away':     str(parts[6]).strip(),   # 'h' or 'a'
                'opponent':      str(parts[7]).strip().lower(),
                'dk_pts_scored': _to_float(parts[8]),
                'dk_salary':     _to_int(parts[9]),
            })
        except Exception as e:
            print(f"  Parse error on line '{line}': {e}")
            continue

    return rows


def _to_float(s: str) -> float:
    try:
        return float(str(s).strip())
    except ValueError:
        return 0.0


def _to_int(s: str) -> int:
    try:
        return int(str(s).replace('$', '').replace(',', '').strip())
    except ValueError:
        return 0


def normalize_name(name: str) -> str:
    """
    'McCaffrey, Christian' → 'christian mccaffrey'
    Used later for fuzzy-joining with PFR names.
    """
    parts = [p.strip() for p in name.split(',')]
    if len(parts) == 2:
        normalized = f"{parts[1]} {parts[0]}"
    else:
        normalized = name
    # lowercase, strip punctuation except spaces
    normalized = re.sub(r"[^a-z0-9 ]", "", normalized.lower()).strip()
    return normalized


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _save(df: pd.DataFrame):
    """Write the dataframe to the output file (overwrites each time)."""
    df = df.drop_duplicates(subset=['year', 'week', 'rg_id'])
    df = df.sort_values(['year', 'week', 'position', 'dk_salary'],
                        ascending=[True, True, True, False])
    df.to_csv(OUTPUT_FILE, index=False, compression='gzip')


def main():
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    # Load existing data so we can skip already-scraped (year, week) pairs
    already_done: set[tuple] = set()
    if os.path.exists(OUTPUT_FILE):
        print(f"Found existing file: {OUTPUT_FILE}")
        existing_df = pd.read_csv(OUTPUT_FILE)
        for _, row in existing_df[['year', 'week']].drop_duplicates().iterrows():
            already_done.add((int(row['year']), int(row['week'])))
        print(f"  Already scraped: {len(already_done)} (year, week) pairs")
    else:
        existing_df = pd.DataFrame(columns=SCSV_COLUMNS + ['name_normalized'])

    # Keep all rows (existing + new) in memory; write to disk after every week
    # so Ctrl-C at any point leaves a valid, readable file.
    all_rows = existing_df.to_dict('records') if not existing_df.empty else []

    total_weeks = sum(WEEKS_BY_YEAR.values())
    done_count  = 0

    for year in YEARS:
        max_week = WEEKS_BY_YEAR[year]
        for week in range(1, max_week + 1):
            done_count += 1

            if (year, week) in already_done:
                print(f"  Skip {year} W{week:02d} (already done)")
                continue

            print(f"  Scraping {year} W{week:02d} ... ({done_count}/{total_weeks})", end='', flush=True)
            rows = fetch_week(year, week)

            if rows:
                print(f" {len(rows)} players")
                for r in rows:
                    r['name_normalized'] = normalize_name(r['name'])
                all_rows.extend(rows)
            else:
                print(" (empty — possible playoff week)")

            # Write after every week — safe to Ctrl-C at any point
            _save(pd.DataFrame(all_rows))
            time.sleep(SLEEP_SEC)

    final_df = pd.read_csv(OUTPUT_FILE)
    print(f"\nDone. {len(final_df):,} rows → {OUTPUT_FILE}")
    print(f"Years: {sorted(final_df['year'].unique())}")


if __name__ == '__main__':
    main()
