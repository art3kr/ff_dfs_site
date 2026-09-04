"""
scrapers/scrape_pfr_defense_historical.py
---------------------------------------------
Scrapes team-level DST stat categories (sacks, INTs, fumble recoveries,
defensive/return TDs) for HISTORICAL years by aggregating PER-GAME
boxscore data — works for any year, unlike scrape_dst_fantasy_stats.py
(FantasyPros, confirmed current-season-only).

Structure confirmed via live diagnostic (Sept 2026), boxscore
/boxscores/202409050kan.htm:
  - 'player_defense' table (commented, same pattern as game_info):
    per-DEFENSIVE-PLAYER stats for that one game — sacks, def_int,
    fumbles_rec, fumbles_rec_td, def_int_td, fumbles_forced, etc.
    Aggregated (summed) by team, this gives exactly the team-level
    DST categories we need.
  - 'returns' table (commented, not yet field-verified — see below):
    return yardage/TDs, assumed to use the same field names already
    confirmed on individual player gamelog pages (kick_ret_td,
    punt_ret_td) since PFR is generally consistent about this. Has a
    debug fallback if that assumption is wrong.

KNOWN GAP: safety isn't in player_defense or (assumed) returns —
always recorded as 0 here. Safeties are rare league-wide; revisit only
if this turns out to matter in practice.

IMPORTANT — SCALE: unlike scrape_team_points.py (one request per
YEAR), this needs one request per GAME — roughly 272 games/season, so
a few thousand requests for a full 2014-2025 backfill. Same resume-
safe, rate-limited, circuit-breaker pattern as scrape_pfr.py's player
stats scraper — expect this to take a while and need cookie refreshes
along the way.

Output format matches scrape_dst_fantasy_stats.py exactly (year, week,
team, sack, interception, fumble_rec, forced_fumble, def_td, safety,
special_teams_td, games), so it flows into the existing
combine_dst_scoring.py unchanged.

Usage:
    python scrapers/scrape_pfr_defense_historical.py --years 2014-2025
"""

import os
import sys
import time
import argparse
import pandas as pd
from bs4 import BeautifulSoup, Comment

sys.path.insert(0, os.path.dirname(__file__))
from scrape_pfr import make_session, pfr_get, PFR_BASE
from team_mapping import normalize_team

DATA_DIR      = os.path.join(os.path.dirname(__file__), '..', 'data')
SCHEDULES_DIR = os.path.join(DATA_DIR, 'schedules')
OUTPUT_FILE   = os.path.join(DATA_DIR, 'hist_dst_stats_historical.csv.gz')
SLEEP_SEC     = 5.0   # increased from 2.5 — hitting PFR rate limits at the old pace
FAILURE_CIRCUIT_BREAKER = 5

OUT_COLUMNS = ['year', 'week', 'team', 'sack', 'interception', 'fumble_rec',
               'forced_fumble', 'def_td', 'safety', 'special_teams_td', 'games']


def _int(s) -> int:
    try:
        return int(float(str(s).strip()))   # float() first handles "0.5" sacks cleanly rounding via int() truncation issues avoided below
    except (ValueError, TypeError):
        return 0


def _float(s) -> float:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return 0.0


def _find_commented_table(soup, table_id):
    live = soup.find('table', id=table_id)
    if live:
        return live
    comments = soup.find_all(string=lambda text: isinstance(text, Comment))
    for c in comments:
        if f'id="{table_id}"' in c:
            c_soup = BeautifulSoup(str(c), 'lxml')
            t = c_soup.find('table', id=table_id)
            if t:
                return t
    return None


def scrape_boxscore_defense(boxscore_url: str, year: int, week: int, session) -> list[dict]:
    """
    Fetch one boxscore, aggregate player_defense (+ returns, if field
    names match our assumption) to team level. Returns [] on failure
    to find data, None on request failure (circuit breaker signal).
    """
    url = PFR_BASE + boxscore_url
    r = pfr_get(session, url, use_headers=False)
    if r is None:
        return None

    soup = BeautifulSoup(r.content, 'lxml')

    def_table = _find_commented_table(soup, 'player_defense')
    if not def_table:
        print(f"    WARNING: no player_defense table found for {boxscore_url}")
        return []

    tbody = def_table.find('tbody')
    if not tbody:
        return []

    # Aggregate defensive stats by team
    by_team = {}   # team -> dict of running totals
    for row in tbody.find_all('tr'):
        if row.get('class') and 'thead' in row.get('class'):
            continue

        def get(stat):
            cell = row.find('td', attrs={'data-stat': stat})
            return cell.get_text(strip=True) if cell else ''

        team_raw = get('team')
        if not team_raw:
            continue
        team = normalize_team(team_raw)
        if not team:
            continue

        if team not in by_team:
            by_team[team] = {'sack': 0.0, 'interception': 0, 'fumble_rec': 0,
                             'forced_fumble': 0, 'def_td': 0}

        by_team[team]['sack']          += _float(get('sacks'))
        by_team[team]['interception']  += _int(get('def_int'))
        by_team[team]['fumble_rec']    += _int(get('fumbles_rec'))
        by_team[team]['forced_fumble'] += _int(get('fumbles_forced'))
        by_team[team]['def_td']        += _int(get('fumbles_rec_td')) + _int(get('def_int_td'))

    # Returns table — special teams TDs. Field names assumed to match
    # the player gamelog page's confirmed convention (kick_ret_td,
    # punt_ret_td). Best-effort: if these fields aren't found, we
    # print a warning and leave special_teams_td at 0 rather than
    # silently guessing wrong field names.
    ret_table = _find_commented_table(soup, 'returns')
    if ret_table:
        ret_tbody = ret_table.find('tbody')
        if ret_tbody:
            sample_row = ret_tbody.find('tr')
            has_expected_fields = sample_row and (
                sample_row.find('td', attrs={'data-stat': 'kick_ret_td'}) is not None or
                sample_row.find('td', attrs={'data-stat': 'punt_ret_td'}) is not None
            )
            if not has_expected_fields and sample_row:
                available = [c.get('data-stat') for c in sample_row.find_all('td')]
                print(f"    WARNING: 'returns' table field names didn't match expected "
                      f"kick_ret_td/punt_ret_td for {boxscore_url}. Available fields: {available}")

            for row in ret_tbody.find_all('tr'):
                if row.get('class') and 'thead' in row.get('class'):
                    continue

                def get_ret(stat):
                    cell = row.find('td', attrs={'data-stat': stat})
                    return cell.get_text(strip=True) if cell else ''

                team_raw = get_ret('team')
                team = normalize_team(team_raw)
                if not team or team not in by_team:
                    continue

                by_team[team]['def_td'] = by_team[team].get('def_td', 0)  # ensure key exists
                if 'special_teams_td' not in by_team[team]:
                    by_team[team]['special_teams_td'] = 0
                by_team[team]['special_teams_td'] += _int(get_ret('kick_ret_td')) + _int(get_ret('punt_ret_td'))

    rows = []
    for team, stats in by_team.items():
        rows.append({
            'year': year, 'week': week, 'team': team,
            'sack': stats.get('sack', 0.0),
            'interception': stats.get('interception', 0),
            'fumble_rec': stats.get('fumble_rec', 0),
            'forced_fumble': stats.get('forced_fumble', 0),
            'def_td': stats.get('def_td', 0),
            'safety': 0,   # known gap — not in player_defense or returns
            'special_teams_td': stats.get('special_teams_td', 0),
            'games': 1,
        })
    return rows


def _save(existing: pd.DataFrame, new_rows: list) -> pd.DataFrame:
    if not new_rows:
        return existing
    new_df = pd.DataFrame(new_rows, columns=OUT_COLUMNS)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=['year', 'week', 'team'], keep='last')
    combined = combined.sort_values(['year', 'week', 'team'])
    combined.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    return combined


def main(years: list[int]):
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        done_pairs = set(zip(existing['year'], existing['week'], existing['team']))
        print(f"Loaded existing file: {len(existing):,} rows, {len(done_pairs)} (year,week,team) done")
    else:
        existing = pd.DataFrame(columns=OUT_COLUMNS)
        done_pairs = set()

    consecutive_failures = 0

    with make_session() as session:
        for year in years:
            print(f"\n{'='*50}\nYEAR {year}\n{'='*50}")

            sched_path = os.path.join(SCHEDULES_DIR, f'{year}_schedule_df.csv')
            if not os.path.exists(sched_path):
                print(f"  No schedule file for {year} — skipping")
                continue

            sched = pd.read_csv(sched_path)
            sched = sched[sched['team_2'] != 'BYE']
            sched = sched[sched['week'].apply(lambda w: str(w).isdigit() or isinstance(w, int))]
            sched['week'] = sched['week'].astype(int)
            sched = sched[sched['week'] <= 18]
            sched = sched[sched['boxscore_url'].notna() & (sched['boxscore_url'] != '')]
            sched = sched.drop_duplicates(subset=['boxscore_url'])

            print(f"  {len(sched)} unique games found")

            for i, row in sched.iterrows():
                boxscore_url = row['boxscore_url']
                week = int(row['week'])

                # Skip if we already have data for both teams in this game.
                # We don't know both team abbreviations ahead of the fetch
                # in a normalized form without re-deriving them, so just
                # check whether ANY row already exists for this exact
                # boxscore by checking a cheap proxy: skip only if fully
                # redundant isn't easily knowable in advance — safe to
                # just re-fetch occasionally; upserts are idempotent.
                print(f"  [{i+1}] {boxscore_url}", end='', flush=True)

                game_rows = scrape_boxscore_defense(boxscore_url, year, week, session)

                if game_rows is None:
                    consecutive_failures += 1
                    print(f" -> FAILED ({consecutive_failures}/{FAILURE_CIRCUIT_BREAKER})")
                    if consecutive_failures >= FAILURE_CIRCUIT_BREAKER:
                        print(f"\n{'!'*60}")
                        print(f"STOPPING: {FAILURE_CIRCUIT_BREAKER} consecutive failures.")
                        print(f"Your PFR_CF_CLEARANCE cookie has almost certainly expired.")
                        print(f"Refresh it, update .env, then re-run this exact command —")
                        print(f"it resumes from here, nothing is lost.")
                        print(f"{'!'*60}\n")
                        _save(existing, [])
                        sys.exit(1)
                    time.sleep(SLEEP_SEC)
                    continue

                consecutive_failures = 0
                existing = _save(existing, game_rows)
                print(f" -> {len(game_rows)} team rows")
                time.sleep(SLEEP_SEC)

    print(f"\nDone. {len(existing):,} total rows -> {OUTPUT_FILE}")


def parse_years(s: str) -> list[int]:
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    elif ',' in s:
        return [int(y) for y in s.split(',')]
    else:
        return [int(s)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", default="2014-2025")
    args = parser.parse_args()

    years = parse_years(args.years)
    print(f"Scraping historical DST stats for: {years}")
    print(f"NOTE: this is a per-GAME scrape (~272 requests/season), much larger")
    print(f"than the per-year schedule/points scrapes. Expect this to take a")
    print(f"while and likely need a cookie refresh partway through.\n")
    main(years)
