"""
scrapers/scrape_scoresandodds_props.py
------------------------------------------
Scrapes real sportsbook player prop lines from scoresandodds.com,
across every category confirmed real by the user directly (via
browser DevTools Network tab — not guessed): each category is a
plain URL at /nfl/props/{slug}, not an AJAX API call as originally
suspected. Much simpler than expected.

Structure confirmed via live diagnostic (Sept 2026) for the default
Passing Yards view specifically, full untruncated row HTML, tested
against 3 real players:
  - Each player is one <li data-name="..." data-proj="..." data-diff="...">
  - Player name: the <a> tag's text (proper case)
  - Matchup: a <span class="bold small gray"> with either "TEAM vs TEAM"
    (home game) or "TEAM @ TEAM" (away game) — both forms tested
  - TWO <div class="best-odds-container"> per row, distinguished by
    the 'o'/'u' prefix on <span class="data-moneyline"> (e.g.
    "o265.5"/"u271.5") rather than by position — robust either way
  - Sportsbook name from the odds link's <img alt="...">
  - data-proj / data-diff on the <li> give the site's own consensus
    projection and (projection - line) delta directly

NOT YET VERIFIED for the other categories — only Passing Yards has
been checked against real HTML. Two categories in particular
(First/Last Touchdown Scorer) are almost certainly YES/NO
propositions rather than over/under lines, and likely won't match
this parser's structure at all — this is flagged with a clear
per-category warning rather than silently producing wrong or empty
data, so a real run tells us exactly what needs fixing rather than
guessing further.

IMPORTANT PAYWALL NOTE: the Proj/Diff/Pct data sits in elements with
a data-fallback attribute referencing "Subscribe now to access this
content" — but the real values are present directly in the raw HTML
regardless; the paywall prompt appears to be a client-side-only
overlay a plain fetch never triggers. Scraping around even a
client-side paywall is a different situation from scraping openly
public data — that distinction is left for you to decide on, not
assumed silently. Pass --no-projection to drop site_projection/
projection_diff from the output entirely if you'd rather not include
that data.

Output: data/scoresandodds_props_{category_slug}.csv.gz (one per
category), or data/scoresandodds_props_all.csv.gz when --all is used
  Columns: category, player_name, team, opponent, home_away,
           over_line, over_odds, over_book, under_line, under_odds,
           under_book, site_projection, projection_diff

Usage:
    python scrapers/scrape_scoresandodds_props.py --category passing-yards
    python scrapers/scrape_scoresandodds_props.py --all
    python scrapers/scrape_scoresandodds_props.py --all --no-projection
"""

import os
import sys
import time
import argparse
import requests
import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR   = os.path.join(os.path.dirname(__file__), '..', 'data')
BASE_URL   = "https://www.scoresandodds.com/nfl/props"
SLEEP_SEC  = 2.0

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}

# Confirmed real via the user's own browser DevTools Network tab
# (Sept 2026) — not guessed. The default page (no slug) is Passing
# Yards, already separately confirmed via direct HTML inspection.
CATEGORIES = {
    'passing-yards':             '',   # default view, no slug needed
    'rushing-yards':             'rushing-yards',
    'receiving-yards':           'receiving-yards',
    'receptions':                'receptions',
    'touchdowns':                'touchdowns',
    'passing-tds':               'passing-tds',
    'completions':                'completions',
    'interceptions':              'interceptions',
    'pass-attempts':              'pass-attempts',
    'rush-attempts':              'rush-attempts',
    'passing-and-rushing-yards':  'passing-&-rushing-yards',
    'rushing-and-receiving-yards': 'rushing-&-receiving-yards',
    'first-touchdown-scorer':     'first-touchdown-scorer',   # likely yes/no, not over/under — untested
    'last-touchdown-scorer':      'last-touchdown-scorer',    # likely yes/no, not over/under — untested
    'longest-reception':          'longest-reception',
    'longest-rush':               'longest-rush',
    # Two categories from the original dropdown the user didn't send a
    # URL for — inferred from the same naming convention, NOT confirmed:
    'kicking-points':             'kicking-points',
    'longest-completion':         'longest-completion',
}

OUT_COLUMNS = ['category', 'player_name', 'team', 'opponent', 'home_away',
               'over_line', 'over_odds', 'over_book',
               'under_line', 'under_odds', 'under_book',
               'site_projection', 'projection_diff']


def _parse_matchup(text: str):
    """'CIN vs TB' (home) or 'DAL @ NYG' (away) -> (team, opponent, home_away)."""
    if ' vs ' in text:
        team, opp = text.split(' vs ')
        return team.strip(), opp.strip(), 'h'
    elif ' @ ' in text:
        team, opp = text.split(' @ ')
        return team.strip(), opp.strip(), 'a'
    return None, None, None


def scrape_category(category_key: str, include_projection: bool = True) -> pd.DataFrame:
    slug = CATEGORIES[category_key]
    url = f"{BASE_URL}/{slug}" if slug else BASE_URL

    print(f"  Fetching {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"    Request error: {e}")
        return pd.DataFrame()

    if r.status_code != 200:
        print(f"    HTTP {r.status_code}")
        return pd.DataFrame()

    soup = BeautifulSoup(r.content, 'html.parser')
    rows = soup.find_all('li', attrs={'data-name': True})
    print(f"    {len(rows)} player rows found")

    if not rows:
        print(f"    WARNING: no rows found at all for '{category_key}' — "
              f"page structure may differ from the confirmed Passing Yards layout.")
        return pd.DataFrame()

    records = []
    structure_mismatches = 0

    for li in rows:
        name_tag = li.find('a')
        if not name_tag:
            continue
        player_name = name_tag.get_text(strip=True)

        matchup_span = li.find('span', class_='bold small gray')
        matchup_text = matchup_span.get_text(strip=True) if matchup_span else ''
        team_raw, opp_raw, home_away = _parse_matchup(matchup_text)
        team = normalize_team(team_raw) if team_raw else None
        opponent = normalize_team(opp_raw) if opp_raw else None

        over_line = over_odds = over_book = None
        under_line = under_odds = under_book = None

        for container in li.find_all('div', class_='best-odds-container'):
            ml_span = container.find('span', class_='data-moneyline')
            odds_tag = container.find('small', class_='data-odds')
            book_img = container.find('img', alt=True)
            if not ml_span:
                continue
            line_text = ml_span.get_text(strip=True)
            if not line_text or line_text[0] not in ('o', 'u'):
                continue
            try:
                line_val = float(line_text[1:])
            except ValueError:
                continue
            odds_val = odds_tag.get_text(strip=True) if odds_tag else None
            book = book_img.get('alt') if book_img else None

            if line_text[0] == 'o':
                over_line, over_odds, over_book = line_val, odds_val, book
            else:
                under_line, under_odds, under_book = line_val, odds_val, book

        site_projection = li.get('data-proj') if include_projection else None
        projection_diff = li.get('data-diff') if include_projection else None

        if not player_name or (over_line is None and under_line is None):
            structure_mismatches += 1
            continue

        records.append({
            'category': category_key,
            'player_name': player_name, 'team': team, 'opponent': opponent,
            'home_away': home_away,
            'over_line': over_line, 'over_odds': over_odds, 'over_book': over_book,
            'under_line': under_line, 'under_odds': under_odds, 'under_book': under_book,
            'site_projection': site_projection, 'projection_diff': projection_diff,
        })

    if structure_mismatches:
        print(f"    WARNING: {structure_mismatches}/{len(rows)} rows had no parseable "
              f"over/under line — this category likely has a different structure "
              f"(e.g. a yes/no proposition rather than a line) and needs its own "
              f"parsing logic, not this generic one. Share the diagnostic output for "
              f"this category and this can be fixed properly rather than guessed at.")

    print(f"    {len(records)} rows successfully parsed")
    return pd.DataFrame(records, columns=OUT_COLUMNS)


def main(category_keys: list[str], include_projection: bool, combine: bool):
    os.makedirs(DATA_DIR, exist_ok=True)
    all_dfs = []

    for i, key in enumerate(category_keys):
        print(f"\n{key}:")
        df = scrape_category(key, include_projection)
        if not df.empty:
            if combine:
                all_dfs.append(df)
            else:
                out_path = os.path.join(DATA_DIR, f"scoresandodds_props_{key}.csv.gz")
                df.to_csv(out_path, index=False, compression='gzip')
                print(f"    Saved -> {out_path}")
        if i < len(category_keys) - 1:
            time.sleep(SLEEP_SEC)

    if combine and all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        out_path = os.path.join(DATA_DIR, "scoresandodds_props_all.csv.gz")
        combined.to_csv(out_path, index=False, compression='gzip')
        print(f"\nSaved {len(combined)} total rows -> {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", choices=list(CATEGORIES.keys()),
                        help="Scrape just one category.")
    parser.add_argument("--all", action="store_true",
                        help="Scrape every known category (one file each, unless --combine).")
    parser.add_argument("--combine", action="store_true",
                        help="With --all, save one combined file instead of one per category.")
    parser.add_argument("--no-projection", action="store_true",
                        help="Drop site_projection/projection_diff from the output — see the "
                             "paywall note in this file's docstring.")
    args = parser.parse_args()

    if args.all:
        keys = list(CATEGORIES.keys())
    elif args.category:
        keys = [args.category]
    else:
        print("Specify --category NAME or --all. Categories:", list(CATEGORIES.keys()))
        sys.exit(1)

    main(keys, include_projection=not args.no_projection, combine=args.combine)
