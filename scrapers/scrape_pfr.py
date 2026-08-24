"""
scrapers/scrape_pfr.py
----------------------
Scrapes Pro Football Reference for detailed weekly player stats + game info
for seasons 2014-2025.

Two output files:
  data/pfr_player_stats_2014_2025.csv.gz   — one row per player per week
  data/pfr_game_info_2014_2025.csv.gz      — one row per game (weather, Vegas, etc.)

Player stats columns:
  pfr_id, name, name_normalized, year, week, team, opponent,
  home_away, position, dk_pts,
  pass_cmp, pass_att, pass_yds, pass_td, pass_int,
  rush_att, rush_yds, rush_td,
  rec_tgt, rec, rec_yds, rec_td,
  snap_pct

Game info columns (from boxscore):
  boxscore_url, year, week, team_home, team_away,
  roof, surface, weather, attendance, vegas_line, over_under

Run:
    python scrapers/scrape_pfr.py --years 2014-2025
    python scrapers/scrape_pfr.py --years 2024-2025   # incremental update

IMPORTANT: PFR rate-limits aggressively. The scraper uses:
  - 4-second sleep between player pages (same as your original)
  - 2-second sleep between boxscore pages
  - Randomized User-Agent rotation
  - Checkpoint saves every 20 players (resume-safe)

Expect ~8-10 hours for a full 2014-2025 run (~600 players × 12 years).
Run it overnight. You can Ctrl-C at any time and re-run — it will resume.
"""

import os
import re
import sys
import time
import glob
import pickle
import random
import argparse
import requests
import pandas as pd
from bs4 import BeautifulSoup, Comment
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR         = os.path.join(os.path.dirname(__file__), '..', 'data')
CHECKPOINT_DIR   = os.path.join(DATA_DIR, '_pfr_checkpoints')
PLAYERS_OUT      = os.path.join(DATA_DIR, 'pfr_player_stats_2014_2025.csv.gz')
GAMES_OUT        = os.path.join(DATA_DIR, 'pfr_game_info_2014_2025.csv.gz')

PFR_BASE         = "https://www.pro-football-reference.com"
SLEEP_PLAYER     = 4.0      # seconds between player pages
SLEEP_BOXSCORE   = 2.0      # seconds between boxscore pages
CHECKPOINT_EVERY = 20       # save checkpoint every N players

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
]

PLAYER_COLUMNS = [
    'pfr_id', 'name', 'name_normalized', 'year', 'week',
    'team', 'opponent', 'home_away', 'position', 'dk_pts',
    'pass_cmp', 'pass_att', 'pass_yds', 'pass_td', 'pass_int',
    'rush_att', 'rush_yds', 'rush_td',
    'rec_tgt', 'rec', 'rec_yds', 'rec_td',
    'snap_pct',
]

GAME_COLUMNS = [
    'boxscore_url', 'year', 'week', 'team_home', 'team_away',
    'roof', 'surface', 'weather', 'attendance', 'vegas_line', 'over_under',
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_headers() -> dict:
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }


def normalize_name(name: str) -> str:
    """'Christian McCaffrey' → 'christian mccaffrey'"""
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


def pfr_id_from_url(url: str) -> str:
    """'/players/M/McCAC00.htm' → 'McCAC00'"""
    m = re.search(r'/players/\w/(\w+)\.htm', url)
    return m.group(1) if m else ''


def _float(s) -> float:
    try:
        return float(str(s).strip().replace('%', ''))
    except:
        return 0.0


def _int(s) -> int:
    try:
        return int(str(s).strip())
    except:
        return 0


def save_checkpoint(data: list, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        pickle.dump(data, f)


def load_checkpoint(path: str) -> list:
    with open(path, 'rb') as f:
        return pickle.load(f)


# ---------------------------------------------------------------------------
# Step 1: Get all players for a year (master list with pfr_id + player_url)
# ---------------------------------------------------------------------------

def get_players_for_year(year: int, session: requests.Session) -> pd.DataFrame:
    """
    Scrape PFR's annual fantasy table to get all players + their page URLs.
    Returns df with columns: name, pfr_id, player_url, team, position, dk_pts_season
    """
    url = f"{PFR_BASE}/years/{year}/fantasy.htm"
    print(f"  Fetching player list: {url}")

    r = session.get(url, headers=get_headers(), verify=False, timeout=30)
    if r.status_code != 200:
        print(f"  ERROR {r.status_code} fetching {url}")
        return pd.DataFrame()

    soup = BeautifulSoup(r.content, 'html.parser')
    table = soup.find('table', id='fantasy')
    if not table:
        # Try first table fallback
        tables = soup.find_all('table')
        table = tables[0] if tables else None
    if not table:
        print(f"  No fantasy table found for {year}")
        return pd.DataFrame()

    rows_data = []
    for row in table.find('tbody').find_all('tr'):
        # Skip header rows
        if row.get('class') and 'thead' in row.get('class'):
            continue

        player_cell = row.find('td', attrs={'data-stat': 'player'})
        if not player_cell or not player_cell.find('a'):
            continue

        try:
            a_tag      = player_cell.find('a')
            name       = a_tag.get_text(strip=True)
            player_url = a_tag.get('href', '')
            pfr_id     = pfr_id_from_url(player_url)
            team       = row.find('td', attrs={'data-stat': 'team'}).get_text(strip=True)
            position   = row.find('td', attrs={'data-stat': 'fantasy_pos'}).get_text(strip=True)

            # DK season total — useful for sanity check
            dk_cell = row.find('td', attrs={'data-stat': 'draftkings_points'})
            dk_pts  = _float(dk_cell.get_text()) if dk_cell else 0.0

            if pfr_id and name:
                rows_data.append({
                    'name': name, 'pfr_id': pfr_id, 'player_url': player_url,
                    'team': team, 'position': position, 'dk_pts_season': dk_pts,
                })
        except Exception as e:
            continue

    df = pd.DataFrame(rows_data)
    print(f"  Found {len(df)} players for {year}")
    return df


# ---------------------------------------------------------------------------
# Step 2: Scrape weekly stats for one player in one year
# ---------------------------------------------------------------------------

def get_player_weekly_stats(pfr_id: str, name: str, player_url: str,
                             year: int, session: requests.Session) -> list[dict]:
    """
    Hit the player's individual fantasy page for `year`.
    Returns list of dicts, one per week played.
    """
    # e.g. /players/M/McCAC00/fantasy/2024/
    url = PFR_BASE + re.sub(r'\.htm$', '', player_url) + f'/fantasy/{year}/'
    r   = session.get(url, headers=get_headers(), verify=False, timeout=30)

    if r.status_code != 200:
        return []

    soup  = BeautifulSoup(r.content, 'html.parser')
    table = soup.find('table', id='player_fantasy')
    if not table:
        return []

    tbody = table.find('tbody')
    if not tbody:
        return []

    rows = []
    for row in tbody.find_all('tr'):
        # Skip sub-header rows
        if row.get('class') and 'thead' in row.get('class'):
            continue

        def get(stat):
            cell = row.find('td', attrs={'data-stat': stat})
            return cell.get_text(strip=True) if cell else ''

        try:
            week_num = _int(get('week_num'))
            if week_num == 0:
                continue

            # Team / opponent / home-away
            team_cell = row.find('td', attrs={'data-stat': 'team'})
            team      = team_cell.get_text(strip=True) if team_cell else ''

            opp_cell  = row.find('td', attrs={'data-stat': 'opp'})
            opp_raw   = opp_cell.get_text(strip=True) if opp_cell else ''
            # PFR formats opponent as '@OPP' for away games
            home_away = 'a' if opp_raw.startswith('@') else 'h'
            opponent  = opp_raw.lstrip('@').strip()

            position  = get('fantasy_pos') or get('pos')
            dk_pts    = _float(get('draftkings_points'))

            # Passing
            pass_cmp  = _int(get('pass_cmp'))
            pass_att  = _int(get('pass_att'))
            pass_yds  = _int(get('pass_yds'))
            pass_td   = _int(get('pass_td'))
            pass_int  = _int(get('pass_int'))

            # Rushing
            rush_att  = _int(get('rush_att'))
            rush_yds  = _int(get('rush_yds'))
            rush_td   = _int(get('rush_td'))

            # Receiving
            rec_tgt   = _int(get('targets'))
            rec       = _int(get('rec'))
            rec_yds   = _int(get('rec_yds'))
            rec_td    = _int(get('rec_td'))

            # Snap % (may not exist in older years — graceful fallback)
            snap_raw  = get('off_pct')
            snap_pct  = _float(snap_raw) if snap_raw else None

            rows.append({
                'pfr_id':          pfr_id,
                'name':            name,
                'name_normalized': normalize_name(name),
                'year':            year,
                'week':            week_num,
                'team':            team.lower(),
                'opponent':        opponent.lower(),
                'home_away':       home_away,
                'position':        position.upper(),
                'dk_pts':          dk_pts,
                'pass_cmp':        pass_cmp,
                'pass_att':        pass_att,
                'pass_yds':        pass_yds,
                'pass_td':         pass_td,
                'pass_int':        pass_int,
                'rush_att':        rush_att,
                'rush_yds':        rush_yds,
                'rush_td':         rush_td,
                'rec_tgt':         rec_tgt,
                'rec':             rec,
                'rec_yds':         rec_yds,
                'rec_td':          rec_td,
                'snap_pct':        snap_pct,
            })

        except Exception as e:
            continue

    return rows


# ---------------------------------------------------------------------------
# Step 3: Scrape game info from boxscores (weather, Vegas, etc.)
# ---------------------------------------------------------------------------

def get_game_info(boxscore_url: str, year: int, week: int,
                  team_home: str, team_away: str,
                  session: requests.Session) -> dict:
    """
    Hit one PFR boxscore page and extract game_info table.
    """
    url = PFR_BASE + boxscore_url
    r   = session.get(url, headers=get_headers(), verify=False, timeout=30)

    result = {
        'boxscore_url': boxscore_url,
        'year': year, 'week': week,
        'team_home': team_home, 'team_away': team_away,
        'roof': '', 'surface': '', 'weather': '',
        'attendance': '', 'vegas_line': '', 'over_under': '',
    }

    if r.status_code != 200:
        return result

    soup  = BeautifulSoup(r.content, 'html.parser')
    # game_info table is inside an HTML comment in PFR
    soup2 = BeautifulSoup("\n".join(soup.find_all(string=Comment)), 'html.parser')
    table = soup2.find('table', id='game_info')
    if not table:
        return result

    titles = [cell.get_text(strip=True) for row in table.find_all('tr')
              for cell in row.find_all('th')]
    values = [cell.get_text(strip=True) for row in table.find_all('tr')
              for cell in row.find_all('td')]

    field_map = {
        'Roof':       'roof',
        'Surface':    'surface',
        'Weather':    'weather',
        'Attendance': 'attendance',
        'Vegas Line': 'vegas_line',
        'Over/Under': 'over_under',
    }

    for pfr_title, col in field_map.items():
        if pfr_title in titles:
            idx = titles.index(pfr_title)
            if idx < len(values):
                result[col] = values[idx]

    return result


# ---------------------------------------------------------------------------
# Step 4: Load schedule from local CSV files (faster than scraping PFR)
# ---------------------------------------------------------------------------

SCHEDULES_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'schedules')


def load_schedule(year: int) -> pd.DataFrame:
    """
    Load schedule from local CSV file at data/schedules/{year}_schedule_df.csv.
    Falls back to scraping PFR if the file doesn't exist.

    Columns expected: year, week, team_1, team_2, date, time, location, boxscore_url
    Home team = team whose city matches `location`. In your files, team_2's
    city always matches location (team_2 = home team, team_1 = visitor).
    """
    path = os.path.join(SCHEDULES_DIR, f'{year}_schedule_df.csv')

    if not os.path.exists(path):
        print(f"  No local schedule for {year}, falling back to PFR web scrape")
        return _scrape_schedule_from_pfr(year)

    df = pd.read_csv(path)

    # Drop bye weeks (team_2 == 'BYE') and rows with no boxscore_url
    df = df[df['team_2'] != 'BYE']
    df = df[df['boxscore_url'].notna() & (df['boxscore_url'] != '')]
    df = df[df['week'].apply(lambda w: str(w).isdigit() or isinstance(w, int))]
    df['week'] = df['week'].astype(int)

    # Regular season only (weeks 1-18)
    df = df[df['week'] <= 18]

    # In these files: location matches team_2's city → team_2 is home
    df = df.rename(columns={'team_2': 'team_home', 'team_1': 'team_away'})

    return df[['year', 'week', 'boxscore_url', 'team_home', 'team_away', 'date']].copy()


def _scrape_schedule_from_pfr(year: int) -> pd.DataFrame:
    """Fallback: scrape schedule from PFR if no local file available."""
    url = f"{PFR_BASE}/years/{year}/games.htm"
    print(f"  Fetching schedule from PFR: {url}")
    try:
        r = requests.get(url, headers=get_headers(), verify=False, timeout=30)
        if r.status_code != 200:
            return pd.DataFrame()
    except Exception as e:
        print(f"  Schedule fetch error: {e}")
        return pd.DataFrame()

    soup  = BeautifulSoup(r.content, 'html.parser')
    table = soup.find('table', id='games')
    if not table:
        return pd.DataFrame()

    rows = []
    for row in table.find('tbody').find_all('tr'):
        if row.get('class') and 'thead' in row.get('class'):
            continue
        try:
            week_cell = row.find('th', attrs={'data-stat': 'week_num'})
            week      = _int(week_cell.get_text()) if week_cell else 0
            if week == 0 or week > 18:
                continue

            boxscore_cell = row.find('td', attrs={'data-stat': 'gamelog_url'})
            if not boxscore_cell or not boxscore_cell.find('a'):
                continue
            boxscore_url = boxscore_cell.find('a').get('href', '')

            winner_cell   = row.find('td', attrs={'data-stat': 'winner'})
            loser_cell    = row.find('td', attrs={'data-stat': 'loser'})
            game_loc_cell = row.find('td', attrs={'data-stat': 'game_location'})
            game_loc      = game_loc_cell.get_text(strip=True) if game_loc_cell else ''
            winner = winner_cell.get_text(strip=True) if winner_cell else ''
            loser  = loser_cell.get_text(strip=True)  if loser_cell  else ''

            if game_loc == '@':
                team_home, team_away = loser, winner
            else:
                team_home, team_away = winner, loser

            rows.append({
                'year': year, 'week': week,
                'boxscore_url': boxscore_url,
                'team_home': team_home, 'team_away': team_away,
                'date': '',
            })
        except:
            continue

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def scrape_player_stats(years: list[int]):
    """Scrape player weekly stats for all given years."""
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    # Load existing output
    if os.path.exists(PLAYERS_OUT):
        existing = pd.read_csv(PLAYERS_OUT)
        done_pairs = set(zip(existing['pfr_id'], existing['year']))
        print(f"Loaded {len(existing):,} existing player-week rows")
        print(f"  {len(done_pairs)} (pfr_id, year) pairs already scraped")
    else:
        existing   = pd.DataFrame(columns=PLAYER_COLUMNS)
        done_pairs = set()

    all_new_rows = []

    with requests.Session() as session:
        for year in years:
            print(f"\n{'='*50}")
            print(f"YEAR {year}")
            print(f"{'='*50}")

            # Get player list for this year
            players_df = get_players_for_year(year, session)
            time.sleep(SLEEP_PLAYER)

            if players_df.empty:
                print(f"  No players found for {year}, skipping")
                continue

            for i, row in players_df.iterrows():
                pfr_id     = row['pfr_id']
                name       = row['name']
                player_url = row['player_url']

                if (pfr_id, year) in done_pairs:
                    print(f"  Skip {name} {year} (already done)")
                    continue

                print(f"  [{i+1}/{len(players_df)}] {name} ({pfr_id}) {year}", end='', flush=True)

                weekly_rows = get_player_weekly_stats(pfr_id, name, player_url, year, session)
                print(f" → {len(weekly_rows)} weeks")

                all_new_rows.extend(weekly_rows)
                done_pairs.add((pfr_id, year))

                # Checkpoint
                if (i + 1) % CHECKPOINT_EVERY == 0:
                    _flush_player_rows(existing, all_new_rows)
                    print(f"  [checkpoint saved at player {i+1}]")

                time.sleep(SLEEP_PLAYER)

    # Final save
    _flush_player_rows(existing, all_new_rows, final=True)


def _flush_player_rows(existing: pd.DataFrame, new_rows: list, final: bool = False):
    """Merge new rows with existing and save."""
    if not new_rows:
        return
    new_df   = pd.DataFrame(new_rows, columns=PLAYER_COLUMNS)
    final_df = pd.concat([existing, new_df], ignore_index=True)
    final_df = final_df.drop_duplicates(subset=['pfr_id','year','week'])
    final_df = final_df.sort_values(['year','week','position'])
    final_df.to_csv(PLAYERS_OUT, index=False, compression='gzip')
    label = "FINAL" if final else "checkpoint"
    print(f"  [{label}] Saved {len(final_df):,} rows → {PLAYERS_OUT}")


def scrape_game_info(years: list[int]):
    """Scrape boxscore game info (weather, Vegas, etc.) for all given years."""
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(GAMES_OUT):
        existing   = pd.read_csv(GAMES_OUT)
        done_urls  = set(existing['boxscore_url'])
        print(f"Loaded {len(existing):,} existing game rows")
    else:
        existing  = pd.DataFrame(columns=GAME_COLUMNS)
        done_urls = set()

    all_new = []

    with requests.Session() as session:
        for year in years:
            print(f"\nGame info: {year}")
            schedule = load_schedule(year)  # uses local CSV, no web request

            if schedule.empty:
                print(f"  No schedule for {year}")
                continue

            for _, game in schedule.iterrows():
                url = game['boxscore_url']
                if url in done_urls:
                    continue

                print(f"  W{game['week']:02d} {game['team_away']} @ {game['team_home']}", end='', flush=True)
                info = get_game_info(url, year, game['week'],
                                     game['team_home'], game['team_away'], session)
                print(f" ✓")
                all_new.append(info)
                done_urls.add(url)
                time.sleep(SLEEP_BOXSCORE)

    if all_new:
        new_df   = pd.DataFrame(all_new, columns=GAME_COLUMNS)
        final_df = pd.concat([existing, new_df], ignore_index=True)
        final_df = final_df.drop_duplicates(subset=['boxscore_url'])
        final_df = final_df.sort_values(['year','week'])
        final_df.to_csv(GAMES_OUT, index=False, compression='gzip')
        print(f"\nSaved {len(final_df):,} game rows → {GAMES_OUT}")
    else:
        print("\nNo new game data scraped.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_years(s: str) -> list[int]:
    """Parse '2014-2025' or '2024' or '2022,2023,2024' into list of ints."""
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    elif ',' in s:
        return [int(y) for y in s.split(',')]
    else:
        return [int(s)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Scrape PFR historical player stats and game info')
    parser.add_argument('--years', default='2014-2025',
                        help='Year range, e.g. "2014-2025" or "2024" or "2022,2023"')
    parser.add_argument('--skip-players', action='store_true',
                        help='Skip player stats scrape (only scrape game info)')
    parser.add_argument('--skip-games', action='store_true',
                        help='Skip game info scrape (only scrape player stats)')
    args = parser.parse_args()

    years = parse_years(args.years)
    print(f"Years to scrape: {years}")
    print(f"Player stats output: {PLAYERS_OUT}")
    print(f"Game info output:    {GAMES_OUT}")
    print()

    if not args.skip_players:
        print("=== SCRAPING PLAYER STATS ===")
        scrape_player_stats(years)

    if not args.skip_games:
        print("\n=== SCRAPING GAME INFO ===")
        scrape_game_info(years)

    print("\nDone.")
