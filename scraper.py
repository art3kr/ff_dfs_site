"""
scraper.py
----------
RotoWire DFS slate scraper, adapted from Slates_salaries_rotowire_scrape.py
in the original FF_2025 repo.

This module is imported by the Flask CLI command `flask ingest-slate`.
It can also be run directly for testing:
    python scraper.py --week 1 --slate-id 9276
"""

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

# RotoWire fields we care about → our DB column names
# Inspect a real API response to confirm field names each season;
# these matched the Week 13 CSV in the repo.
FIELD_MAP = {
    "player":     "name",
    "position":   "position",
    "team":       "team",
    "opp":        "opponent",
    "salary":     "salary",
    "proj":       "projected_pts",
    "ownership":  "ownership_pct",
}


def is_thu_mon_classic_slate(slate_id: int) -> bool:
    """Return True if the given RotoWire slate ID is a Thu-Mon Classic DraftKings contest."""
    url = ROTOWIRE_SLATE_CHECK_URL.format(slate_id=slate_id)
    try:
        r = requests.get(url, verify=False, timeout=10)
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
        r = requests.get(url, verify=False, timeout=15)
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
