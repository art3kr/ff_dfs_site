"""
scraper.py
----------
RotoWire DFS slate scraper, adapted from Slates_salaries_rotowire_scrape.py
in the original FF_2025 repo.

This module is imported by the Flask CLI command `flask ingest-slate`.
It can also be run directly for testing:
    python scraper.py --week 1 --slate-id 9276
"""

import os
from dotenv import load_dotenv
load_dotenv()
import requests
import urllib3
from bs4 import BeautifulSoup

# RotoWire blocks SSL verification warnings — suppress them (same as original)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROTOWIRE_SLATE_CHECK_URL = (
    "https://www.rotowire.com/daily/nfl/dfs-opportunities.php"
    "?site=DraftKings&slateID={slate_id}"
)
ROTOWIRE_DATA_URL = (
    "https://www.rotowire.com/daily/tables/value-report-nfl.php"
    "?siteID=2&slateID={slate_id}&projSource=RotoWire&oshipSource=RotoWire"
)

# ---------------------------------------------------------------------------
# Auth — RotoWire requires a logged-in session to return the full player list.
# Set RW_PHPSESSID and RW_TSD as environment variables (or in your .env file).
# Get them from: Chrome DevTools → Application → Cookies → www.rotowire.com
# ---------------------------------------------------------------------------

RW_PHPSESSID = os.environ.get("RW_PHPSESSID", "")
RW_TSD       = os.environ.get("RW_TSD", "")

def _rw_headers() -> dict:
    h = {
        'User-Agent':       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
        'Accept':           'application/json, text/javascript, */*; q=0.01',
        'Accept-Language':  'en-US,en;q=0.9',
        'Accept-Encoding':  'gzip, deflate, br',
        'Referer':          'https://www.rotowire.com/daily/nfl/value-report.php',
        'X-Requested-With': 'XMLHttpRequest',
        'DNT':              '1',
        'Connection':       'keep-alive',
    }
    parts = []
    if RW_PHPSESSID:
        parts.append(f"PHPSESSID={RW_PHPSESSID}")
    if RW_TSD:
        parts.append(f"rw_tsd={RW_TSD}")
    if parts:
        h['Cookie'] = "; ".join(parts)
    return h


def is_thu_mon_classic_slate(slate_id: int) -> bool:
    """Return True if the given RotoWire slate ID is a Thu-Mon Classic DraftKings contest."""
    url = ROTOWIRE_SLATE_CHECK_URL.format(slate_id=slate_id)
    try:
        r = requests.get(url, headers=_rw_headers(), verify=False, timeout=10)
    except requests.RequestException as e:
        print(f"  Request error checking slate {slate_id}: {e}")
        return False

    if r.status_code != 200:
        return False

    soup = BeautifulSoup(r.text, "html.parser")
    title_div = soup.find("div", class_="page-title__secondary")
    if title_div:
        title = title_div.text.strip()
        print(f"  Slate {slate_id} title: '{title}'")
        return "Thu-Mon" in title and "Classic contest" in title
    return False


def find_latest_slate(start: int = 9500, lower_bound: int = 9400) -> int | None:
    """
    Walk backward from `start` to `lower_bound` looking for the most recent
    Thu-Mon Classic slate. Adjust start/lower_bound each season as slate IDs advance.

    Returns the slate ID (int) or None if not found.
    """
    slate_id = start
    while slate_id >= lower_bound:
        print(f"Checking slate {slate_id}...")
        if is_thu_mon_classic_slate(slate_id):
            print(f"Found valid slate: {slate_id}")
            return slate_id
        slate_id -= 1
    return None


def fetch_slate_data(slate_id: int, week: int, year: int) -> list[dict]:
    """
    Hit the RotoWire JSON API for the given slate and return a list of dicts
    ready to be inserted into the `players` table.
    """
    url = ROTOWIRE_DATA_URL.format(slate_id=slate_id)
    try:
        r = requests.get(url, headers=_rw_headers(), verify=False, timeout=15)
    except requests.RequestException as e:
        print(f"Request error fetching slate data: {e}")
        return []

    if r.status_code != 200:
        print(f"Non-200 status {r.status_code} from RotoWire data API")
        return []

    raw = r.json()
    players = []

    for item in raw:
        # --- map fields, with safe fallbacks ---
        name     = str(item.get("player", "")).strip()
        if not name:
            continue  # skip blank rows

        position = str(item.get("position", "")).strip().upper()
        team     = str(item.get("team", "")).strip()
        opponent = str(item.get("opp", "")).strip()

        # Salary comes back as a string like "7500" or "$7,500" — normalise to int
        salary_raw = str(item.get("salary", "0")).replace("$", "").replace(",", "").strip()
        try:
            salary = int(float(salary_raw))
        except ValueError:
            salary = 0

        # Projected points
        try:
            projected_pts = float(item.get("proj", 0) or 0)
        except (ValueError, TypeError):
            projected_pts = 0.0

        # Ownership %
        try:
            ownership_pct = float(item.get("ownership", 0) or 0)
        except (ValueError, TypeError):
            ownership_pct = 0.0

        players.append({
            "week":          week,
            "year":          year,
            "name":          name,
            "position":      position,
            "team":          team,
            "opponent":      opponent,
            "salary":        salary,
            "projected_pts": projected_pts,
            "ownership_pct": ownership_pct,
        })

    print(f"Parsed {len(players)} players from slate {slate_id}")
    return players


# ---------------------------------------------------------------------------
# Quick test: python scraper.py --week 1 --slate-id 9276
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test RotoWire slate scraper")
    parser.add_argument("--week",     type=int, required=True)
    parser.add_argument("--slate-id", type=int, required=True)
    parser.add_argument("--year",     type=int, default=2025)
    args = parser.parse_args()

    players = fetch_slate_data(args.slate_id, args.week, args.year)
    for p in players[:5]:
        print(p)
    print(f"\nTotal: {len(players)} players")
