"""
scrapers/scrape_firstdown_studio.py
--------------------------------------
Scrapes firstdown.studio's Vegas prop-driven fantasy rankings —
currently SEASON-LONG projections only (confirmed live, Sept 2026);
weekly rankings (same site, different URL prefix) aren't live yet but
this same code will work for them once they launch — see --weekly.

Structure confirmed via live diagnostic (not guessed):
  - Real <table> element, not a div/CSS-grid layout
  - Player name cell has TWO nested name spans (a short "mobile" name
    and a full "desktop" name — sometimes identical, sometimes not,
    e.g. "J. Smith-Njigba" vs "Jaxon Smith-Njigba"); we take whichever
    is longer, which is always the untruncated full name
  - Team abbreviation is a separate sibling span with a
    'text-muted-foreground' class
  - Header structure is multi-row and includes some duplicate/responsive
    variants, so rather than hardcoding "skip N header rows", we detect
    real DATA rows by checking whether the first cell's text matches a
    rank pattern like "1" or "3(+1)" — robust regardless of exactly how
    many header rows precede the data
  - Column meaning varies by position (QB has Passing+Rushing, WR/TE
    have Receiving, RB likely has Rushing+Receiving) — rather than
    hardcode this per position (only QB and WR structure has actually
    been verified), we pull the real header labels from the page itself
    and use those as the output column names, so this works correctly
    for any position without needing to guess unverified ones

Output: data/firstdown_studio_{scope}_{position}.csv.gz
  where scope is 'season' or 'week{N}'

Usage:
    python scrapers/scrape_firstdown_studio.py --position wr
    python scrapers/scrape_firstdown_studio.py --position all
    python scrapers/scrape_firstdown_studio.py --position wr --weekly --week 1
"""

import os
import re
import sys
import time
import argparse
import requests
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR  = os.path.join(os.path.dirname(__file__), '..', 'data')
SLEEP_SEC = 2.0

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

POSITIONS = ['qb', 'rb', 'wr', 'te', 'flex']

RANK_PATTERN = re.compile(r'^\d+(\(.+\))?$')


def _extract_name_team(name_cell) -> tuple[str, str]:
    """
    Confirmed structure (live diagnostic, Sept 2026): team is a sibling
    span with a 'text-muted-foreground' class; player name is inside a
    'truncate' span wrapping two nested spans (short/mobile + full/
    desktop name) — we take whichever is longer, always the full name.
    """
    team_span = name_cell.find('span', class_=lambda c: c and 'text-muted-foreground' in c)
    team_raw = team_span.get_text(strip=True) if team_span else ''

    truncate_span = name_cell.find('span', class_=lambda c: c and 'truncate' in c)
    name = ''
    if truncate_span:
        inner_spans = truncate_span.find_all('span')
        if inner_spans:
            texts = [s.get_text(strip=True) for s in inner_spans]
            name = max(texts, key=len)

    return name, team_raw


def _is_data_row(row) -> bool:
    """A real player row's first cell is a rank like '1' or '3(+1)' —
    robust to however many header rows precede the data, rather than
    assuming a fixed count."""
    cells = row.find_all(['td', 'th'])
    if not cells:
        return False
    rank_text = cells[0].get_text(strip=True)
    return bool(RANK_PATTERN.match(rank_text))


def _find_header_labels(table) -> list[str]:
    """
    Pull real column labels from whichever header row contains 'Player'
    — that's the row with the top-level labels (Pts, Rec, Rec Yds, ...)
    confirmed present for both QB and WR. Used as-is for CSV column
    names, so this works for any position without hardcoding its
    specific stat categories.
    """
    for row in table.find_all('tr'):
        cells = row.find_all(['td', 'th'])
        texts = [c.get_text(strip=True) for c in cells]
        if 'Player' in texts:
            return texts
    return []


def scrape_position(position: str, weekly: bool, week: int | None) -> pd.DataFrame:
    if weekly:
        path_prefix = "rankings"
        url = f"https://www.firstdown.studio/{path_prefix}" if position == 'qb' \
            else f"https://www.firstdown.studio/{path_prefix}/{position}"
    else:
        path_prefix = "season-rankings"
        url = f"https://www.firstdown.studio/{path_prefix}" if position == 'qb' \
            else f"https://www.firstdown.studio/{path_prefix}/{position}"

    print(f"  Fetching {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"    Request error: {e}")
        return pd.DataFrame()

    if r.status_code != 200:
        print(f"    HTTP {r.status_code}")
        return pd.DataFrame()

    soup = BeautifulSoup(r.content, 'lxml')
    tables = soup.find_all('table')
    if not tables:
        print(f"    No <table> found — page structure may have changed, "
              f"or (for --weekly) this position's weekly rankings aren't live yet.")
        return pd.DataFrame()

    table = tables[0]
    header_labels = _find_header_labels(table)
    if not header_labels:
        print(f"    WARNING: couldn't find header row containing 'Player' — "
              f"using generic column names instead.")

    rows = table.find_all('tr')
    data_rows = [row for row in rows if _is_data_row(row)]

    if not data_rows:
        print(f"    0 data rows found. Dumping first 3 raw rows for diagnosis:")
        for row in rows[:3]:
            print(f"      {row.get_text(' | ', strip=True)[:200]}")
        return pd.DataFrame()

    records = []
    for row in data_rows:
        cells = row.find_all(['td', 'th'])
        if len(cells) < 2:
            continue

        rank_raw = cells[0].get_text(strip=True)
        name, team_raw = _extract_name_team(cells[1])
        team = normalize_team(team_raw)

        if not team:
            print(f"    WARNING: couldn't normalize team '{team_raw}' for player '{name}' — keeping raw value")
            team = team_raw.lower()

        record = {
            'rank': rank_raw,
            'name': name,
            'name_normalized': re.sub(r"[^a-z0-9 ]", "", name.lower().strip()),
            'team': team,
            'position': position,
        }

        # Remaining cells (index 2+) — use real header labels if we have
        # them (aligned by position), else generic stat_N names.
        for i, cell in enumerate(cells[2:], start=2):
            col_name = header_labels[i] if i < len(header_labels) and header_labels[i] else f"stat_{i}"
            col_name = re.sub(r'[^a-z0-9]+', '_', col_name.lower()).strip('_')
            record[col_name] = cell.get_text(strip=True)

        records.append(record)

    print(f"    {len(records)} players parsed")
    return pd.DataFrame(records)


def main(positions: list[str], weekly: bool, week: int | None):
    os.makedirs(DATA_DIR, exist_ok=True)
    scope = f"week{week}" if weekly and week else "season"

    for position in positions:
        print(f"\n{position.upper()}:")
        df = scrape_position(position, weekly, week)

        if df.empty:
            print(f"  Skipping save — no data.")
            time.sleep(SLEEP_SEC)
            continue

        out_path = os.path.join(DATA_DIR, f"firstdown_studio_{scope}_{position}.csv.gz")
        df.to_csv(out_path, index=False, compression='gzip')
        print(f"  Saved {len(df)} rows -> {out_path}")

        time.sleep(SLEEP_SEC)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--position", default="all",
                        choices=POSITIONS + ["all"])
    parser.add_argument("--weekly", action="store_true",
                        help="Scrape the weekly rankings (/rankings) instead of "
                             "season-long (/season-rankings). Not live yet as of "
                             "this writing — will 0-result gracefully until it is.")
    parser.add_argument("--week", type=int, default=None,
                        help="Week number, used only for labeling the output filename")
    args = parser.parse_args()

    positions = POSITIONS if args.position == "all" else [args.position]
    main(positions, args.weekly, args.week)
