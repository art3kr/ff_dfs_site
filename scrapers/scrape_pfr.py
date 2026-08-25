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

dk_pts is computed by this script using the standard DraftKings PPR
formula (see calculate_dk_points()), since the career gamelog page this
scraper uses doesn't include a pre-computed DK points column.

Game info columns (from boxscore):
  boxscore_url, year, week, team_home, team_away,
  roof, surface, weather, attendance, vegas_line, over_under

Run:
    python scrapers/scrape_pfr.py --years 2014-2025
    python scrapers/scrape_pfr.py --years 2024-2025   # incremental update

IMPORTANT: PFR is behind Cloudflare, which blocks plain scraper requests
with a JS challenge. This scraper reuses a `cf_clearance` cookie captured
from a real logged-in browser session (see make_session() below for how
to get one) — this lets us use fast, simple `requests` calls instead of
a full browser, while still passing Cloudflare's check.

Required setup — add these to your .env file:
    PFR_CF_CLEARANCE=<value from your browser's cf_clearance cookie>
    PFR_CF_BM=<value from your browser's __cf_bm cookie>
    PFR_USER_AGENT=<run navigator.userAgent in your browser console>

These expire periodically (often within hours). When you start getting
403s again, grab fresh values from your browser and update .env.

SPEED: player stats are scraped via each player's career gamelog page
(/players/X/XXXX00/gamelog/ — no year in the URL), which returns their
ENTIRE career in one request. A player active across all 12 requested
years is hit once instead of 12 times. This cuts total requests (and
therefore both runtime and exposure to getting blocked) by roughly 3x
compared to fetching one page per player per year.

RELIABILITY: if 5 requests in a row fail, the scraper assumes your
PFR_CF_CLEARANCE cookie has expired and stops itself immediately with
instructions, rather than grinding through hundreds of players uselessly
while blocked. Refresh the cookie and re-run the same command — it
resumes exactly where it stopped.

Rate limiting:
  - 6-second sleep between player pages
  - 3-second sleep between boxscore pages
  - Retries with backoff on 403/429 responses
  - Saves to disk after every player and every game — Ctrl-C at any
    point leaves a valid, fully up-to-date file, and re-running the
    same command picks up exactly where you left off.

Expect roughly 3-4 hours for a full 2014-2025 run (down from ~10+ hours
with the old per-year approach), assuming your cookie stays valid.

"""

import os
import re
import sys
import time
import random
import argparse
import requests
import pandas as pd
from bs4 import BeautifulSoup, Comment
from dotenv import load_dotenv
load_dotenv()
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR         = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data'))
PLAYERS_OUT      = os.path.join(DATA_DIR, 'pfr_player_stats_2014_2025.csv.gz')
GAMES_OUT        = os.path.join(DATA_DIR, 'pfr_game_info_2014_2025.csv.gz')

PFR_BASE         = "https://www.pro-football-reference.com"
SLEEP_PLAYER     = 6.0      # seconds between player pages (increased to avoid 429s)
SLEEP_BOXSCORE   = 3.0      # seconds between boxscore pages
SLEEP_ON_429     = 90.0     # seconds to wait after a 429 rate-limit response
SLEEP_ON_403     = 30.0     # seconds to wait after a 403 before retrying
MAX_RETRIES      = 3        # number of retries per page before giving up

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
]

# ---------------------------------------------------------------------------
# Cloudflare cookie auth
#
# PFR is behind Cloudflare. A real browser that solves Cloudflare's JS
# challenge gets issued a `cf_clearance` cookie — reusing that cookie in
# a plain requests session lets us skip the challenge entirely, without
# needing a full browser (Playwright) at all.
#
# How to get fresh values when this expires:
#   1. Open pro-football-reference.com in Chrome, load any page normally
#   2. DevTools (F12) -> Application -> Cookies -> www.pro-football-reference.com
#   3. Copy the values of cf_clearance and __cf_bm
#   4. DevTools -> Console -> run: navigator.userAgent  -> copy that too
#   5. Put all three in your .env file (see below) and re-run
#
# cf_clearance is typically valid for a limited window (often hours, not
# days) and is usually tied to the User-Agent + IP that solved the
# challenge, so PFR_USER_AGENT must match the browser you copied it from.
# ---------------------------------------------------------------------------

PFR_CF_CLEARANCE = os.environ.get("PFR_CF_CLEARANCE", "")
PFR_CF_BM        = os.environ.get("PFR_CF_BM", "")
PFR_USER_AGENT   = os.environ.get(
    "PFR_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
)


def make_session() -> requests.Session:
    """
    Create a requests.Session pre-loaded with the Cloudflare clearance
    cookie captured from a real browser, so requests pass through without
    triggering the JS challenge.
    """
    session = requests.Session()

    if not PFR_CF_CLEARANCE:
        print("WARNING: PFR_CF_CLEARANCE not set in .env — requests will "
              "likely be blocked by Cloudflare. See scrape_pfr.py header "
              "comment for how to get a fresh cookie.")

    if PFR_CF_CLEARANCE:
        session.cookies.set(
            'cf_clearance', PFR_CF_CLEARANCE,
            domain='.pro-football-reference.com', path='/'
        )
    if PFR_CF_BM:
        session.cookies.set(
            '__cf_bm', PFR_CF_BM,
            domain='.pro-football-reference.com', path='/'
        )

    session.headers.update({'User-Agent': PFR_USER_AGENT})
    return session


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
    """
    Minimal headers matching the original working scraper.
    IMPORTANT: PFR's bot detection appears to flag "too complete" header
    sets (Sec-Fetch-*, DNT, etc.) as suspicious more than it flags bare
    requests. The original Weekly_Scores_Scrape.py used NO custom headers
    at all for table pages and only a User-Agent for per-player pages —
    and that worked reliably. So we replicate that minimal footprint here.
    """
    return {
        'User-Agent': random.choice(USER_AGENTS),
    }


def pfr_get(session: requests.Session, url: str, use_headers: bool = True) -> requests.Response | None:
    """
    GET a PFR page with retry logic for 403/429 responses.
    - 429 (rate limited): wait SLEEP_ON_429 seconds then retry
    - 403 (blocked):      wait SLEEP_ON_403 seconds then retry
      (if this keeps happening, PFR_CF_CLEARANCE has likely expired —
      get a fresh one from your browser, see the make_session() docstring)
    - Other errors:       return None after MAX_RETRIES attempts

    The session already carries the cf_clearance cookie and a matching
    User-Agent (set in make_session()), so use_headers here only controls
    whether we ALSO send the extra rotating User-Agent from USER_AGENTS —
    which we don't want to do now, since it would mismatch the UA that
    the cf_clearance cookie was issued for. Kept as a no-op parameter for
    backward compatibility with existing call sites.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, verify=False, timeout=30)
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}/{MAX_RETRIES}): {e}")
            time.sleep(SLEEP_ON_403)
            continue

        if r.status_code == 200:
            return r

        if r.status_code == 429:
            print(f"  Rate limited (429). Waiting {SLEEP_ON_429}s before retry {attempt+1}/{MAX_RETRIES}...")
            time.sleep(SLEEP_ON_429)
            continue

        if r.status_code == 403:
            print(f"  Blocked (403). Waiting {SLEEP_ON_403}s before retry {attempt+1}/{MAX_RETRIES}...")
            time.sleep(SLEEP_ON_403)
            continue

        print(f"  HTTP {r.status_code} for {url}")
        return None

    print(f"  Gave up after {MAX_RETRIES} attempts: {url}")
    return None


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


def calculate_dk_points(pass_yds=0, pass_td=0, pass_int=0,
                        rush_yds=0, rush_td=0,
                        rec=0, rec_yds=0, rec_td=0,
                        fumbles_lost=0) -> float:
    """
    Standard DraftKings NFL PPR scoring formula. Used to compute fantasy
    points ourselves from raw box-score stats, since the career gamelog
    page (unlike the per-year fantasy page) doesn't include a pre-computed
    DK points column.
    """
    pts = 0.0
    pts += pass_yds * 0.04          # 1 pt per 25 pass yds
    pts += pass_td * 4
    pts += pass_int * -1
    pts += rush_yds * 0.1           # 1 pt per 10 rush yds
    pts += rush_td * 6
    pts += rec * 1                  # PPR: 1 pt per reception
    pts += rec_yds * 0.1            # 1 pt per 10 rec yds
    pts += rec_td * 6
    pts += fumbles_lost * -1
    if pass_yds >= 300:
        pts += 3
    if rush_yds >= 100:
        pts += 3
    if rec_yds >= 100:
        pts += 3
    return round(pts, 2)


# ---------------------------------------------------------------------------
# Step 1: Get all players for a year (master list with pfr_id + player_url)
# ---------------------------------------------------------------------------

def get_players_for_year(year: int, session: requests.Session) -> pd.DataFrame:
    """
    Scrape PFR's annual fantasy table to get all players + their page URLs.
    Returns df with columns: name, pfr_id, player_url, team, position, dk_pts_season
    """
    # NOTE: table-listing pages use a bare request (no custom headers),
    # matching the original working scraper. No homepage warm-up either —
    # both were found to make PFR's bot detection MORE likely to block us,
    # not less. Less looks more human here, apparently.
    url = f"{PFR_BASE}/years/{year}/fantasy.htm"
    print(f"  Fetching player list: {url}")

    r = pfr_get(session, url, use_headers=False)
    if r is None:
        print(f"  ERROR: could not fetch {url} after retries")
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
# Step 2: Scrape a player's ENTIRE career game log in one request
#
# PFR's career gamelog page (/players/X/XXXX00/gamelog/ — no year in the
# URL) returns every regular-season game the player has ever played, all
# in one page. This replaces hitting /fantasy/{year}/ once per player per
# year — for a player active 2014-2025, that's 12 requests collapsed
# into 1. Across a full run this cuts total requests (and therefore both
# runtime and exposure to getting blocked) by roughly 3x.
#
# Trade-off: this page doesn't have a pre-computed DK points column like
# the per-year fantasy page did, so we compute it ourselves from raw
# stats via calculate_dk_points().
# ---------------------------------------------------------------------------

def get_player_career_gamelog(pfr_id: str, name: str, player_url: str,
                              position: str, years_needed: set,
                              session: requests.Session) -> list[dict]:
    """
    Hit the player's career gamelog page ONCE and return rows only for
    the years in `years_needed`. Returns list of dicts, one per game.
    """
    url = PFR_BASE + re.sub(r'\.htm$', '', player_url) + '/gamelog/'
    r   = pfr_get(session, url, use_headers=True)

    if r is None:
        return None   # None = request failed (distinct from [] = no games)

    soup  = BeautifulSoup(r.content, 'html.parser')
    table = soup.find('table', id='stats')
    if not table:
        return []

    tbody = table.find('tbody')
    if not tbody:
        return []

    rows = []
    for row in tbody.find_all('tr'):
        if row.get('class') and 'thead' in row.get('class'):
            continue

        def get(stat):
            cell = row.find('td', attrs={'data-stat': stat}) or \
                   row.find('th', attrs={'data-stat': stat})
            return cell.get_text(strip=True) if cell else ''

        try:
            year = _int(get('year_id'))
            if year not in years_needed:
                continue   # skip games outside the range we asked for

            week_num = _int(get('week_num'))
            if week_num == 0:
                continue

            team      = get('team')
            opp_raw   = get('opp') or get('game_location')
            game_loc  = get('game_location')   # '@' if away, '' if home
            home_away = 'a' if game_loc == '@' else 'h'
            opponent  = get('opp')

            pass_yds  = _int(get('pass_yds'))
            pass_td   = _int(get('pass_td'))
            pass_int  = _int(get('pass_int'))
            rush_yds  = _int(get('rush_yds'))
            rush_td   = _int(get('rush_td'))
            rec       = _int(get('rec'))
            rec_yds   = _int(get('rec_yds'))
            rec_td    = _int(get('rec_td'))
            fumbles_lost = _int(get('fumbles_lost'))

            dk_pts = calculate_dk_points(
                pass_yds=pass_yds, pass_td=pass_td, pass_int=pass_int,
                rush_yds=rush_yds, rush_td=rush_td,
                rec=rec, rec_yds=rec_yds, rec_td=rec_td,
                fumbles_lost=fumbles_lost,
            )

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
                'pass_cmp':        _int(get('pass_cmp')),
                'pass_att':        _int(get('pass_att')),
                'pass_yds':        pass_yds,
                'pass_td':         pass_td,
                'pass_int':        pass_int,
                'rush_att':        _int(get('rush_att')),
                'rush_yds':        rush_yds,
                'rush_td':         rush_td,
                'rec_tgt':         _int(get('targets')),
                'rec':             rec,
                'rec_yds':         rec_yds,
                'rec_td':          rec_td,
                'snap_pct':        _float(get('off_pct')) if get('off_pct') else None,
            })

        except Exception:
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
    r   = pfr_get(session, url, use_headers=False)

    result = {
        'boxscore_url': boxscore_url,
        'year': year, 'week': week,
        'team_home': team_home, 'team_away': team_away,
        'roof': '', 'surface': '', 'weather': '',
        'attendance': '', 'vegas_line': '', 'over_under': '',
        '_request_failed': False,
    }

    if r is None:
        result['_request_failed'] = True
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
    with make_session() as sched_session:
        r = pfr_get(sched_session, url, use_headers=False)
    if r is None:
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

    with make_session() as session:
        # -------------------------------------------------------------
        # Pass 1: build a unique player list across ALL requested years.
        # This still requires one request per year (cheap — 12 total),
        # but means each player is scraped once no matter how many of
        # our requested years they played in.
        # -------------------------------------------------------------
        unique_players = {}   # pfr_id -> {name, player_url, position, years: set}

        for year in years:
            print(f"\n{'='*50}")
            print(f"YEAR {year} — building player list")
            print(f"{'='*50}")

            players_df = get_players_for_year(year, session)
            time.sleep(SLEEP_PLAYER)

            if players_df.empty:
                print(f"  No players found for {year}, skipping")
                continue

            for _, row in players_df.iterrows():
                pfr_id = row['pfr_id']
                if pfr_id not in unique_players:
                    unique_players[pfr_id] = {
                        'name': row['name'], 'player_url': row['player_url'],
                        'position': row['position'], 'years': set(),
                    }
                unique_players[pfr_id]['years'].add(year)

        print(f"\n{len(unique_players)} unique players across {len(years)} year(s) "
              f"(vs {sum(len(p['years']) for p in unique_players.values())} "
              f"player-year combinations the old per-year approach would have hit)")

        # -------------------------------------------------------------
        # Pass 2: scrape each unique player's full career gamelog ONCE,
        # filtered down to only the years we actually need.
        # -------------------------------------------------------------
        consecutive_failures = 0
        FAILURE_CIRCUIT_BREAKER = 5   # stop entirely after this many in a row

        player_ids = list(unique_players.keys())
        for i, pfr_id in enumerate(player_ids):
            info = unique_players[pfr_id]

            if all((pfr_id, y) in done_pairs for y in info['years']):
                print(f"  Skip {info['name']} (all years already done)")
                continue

            still_needed = {y for y in info['years'] if (pfr_id, y) not in done_pairs}

            print(f"  [{i+1}/{len(player_ids)}] {info['name']} ({pfr_id}) "
                  f"years={sorted(still_needed)}", end='', flush=True)

            game_rows = get_player_career_gamelog(
                pfr_id, info['name'], info['player_url'], info['position'],
                still_needed, session
            )

            if game_rows is None:
                # Request failed after all retries — likely cookie expired
                consecutive_failures += 1
                print(f" → FAILED ({consecutive_failures}/{FAILURE_CIRCUIT_BREAKER} consecutive)")

                if consecutive_failures >= FAILURE_CIRCUIT_BREAKER:
                    print(f"\n{'!'*60}")
                    print(f"STOPPING: {FAILURE_CIRCUIT_BREAKER} consecutive requests failed.")
                    print(f"Your PFR_CF_CLEARANCE cookie has almost certainly expired.")
                    print(f"Refresh it from your browser (see the top of this file for")
                    print(f"instructions), update .env, then re-run this exact command —")
                    print(f"it will resume from {info['name']} onward, nothing is lost.")
                    print(f"{'!'*60}\n")
                    _flush_player_rows(existing, all_new_rows, final=True)
                    sys.exit(1)

                time.sleep(SLEEP_PLAYER)
                continue

            consecutive_failures = 0   # reset on any success, even 0-row success

            all_new_rows.extend(game_rows)
            for y in still_needed:
                done_pairs.add((pfr_id, y))

            _flush_player_rows(existing, all_new_rows)
            print(f" → {len(game_rows)} games [saved to {os.path.basename(PLAYERS_OUT)}]")

            time.sleep(SLEEP_PLAYER)

    # Final save (redundant with the per-player save above, kept as a safety net)
    _flush_player_rows(existing, all_new_rows, final=True)


def _flush_player_rows(existing: pd.DataFrame, new_rows: list, final: bool = False):
    """Merge new rows with existing and save to disk."""
    if not new_rows:
        return
    new_df   = pd.DataFrame(new_rows, columns=PLAYER_COLUMNS)
    final_df = pd.concat([existing, new_df], ignore_index=True)
    final_df = final_df.drop_duplicates(subset=['pfr_id','year','week'])
    final_df = final_df.sort_values(['year','week','position'])
    final_df.to_csv(PLAYERS_OUT, index=False, compression='gzip')
    if final:
        print(f"  [FINAL] Saved {len(final_df):,} rows → {PLAYERS_OUT}")


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

    def _flush_games():
        if not all_new:
            return
        new_df   = pd.DataFrame(all_new, columns=GAME_COLUMNS)
        final_df = pd.concat([existing, new_df], ignore_index=True)
        final_df = final_df.drop_duplicates(subset=['boxscore_url'])
        final_df = final_df.sort_values(['year','week'])
        final_df.to_csv(GAMES_OUT, index=False, compression='gzip')

    with make_session() as session:
        consecutive_failures = 0
        FAILURE_CIRCUIT_BREAKER = 5

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

                if info.get('_request_failed'):
                    consecutive_failures += 1
                    print(f" → FAILED ({consecutive_failures}/{FAILURE_CIRCUIT_BREAKER} consecutive)")

                    if consecutive_failures >= FAILURE_CIRCUIT_BREAKER:
                        print(f"\n{'!'*60}")
                        print(f"STOPPING: {FAILURE_CIRCUIT_BREAKER} consecutive requests failed.")
                        print(f"Your PFR_CF_CLEARANCE cookie has almost certainly expired.")
                        print(f"Refresh it, update .env, then re-run this exact command —")
                        print(f"it will resume from here, nothing is lost.")
                        print(f"{'!'*60}\n")
                        _flush_games()
                        sys.exit(1)

                    time.sleep(SLEEP_BOXSCORE)
                    continue

                consecutive_failures = 0
                info.pop('_request_failed', None)
                all_new.append(info)
                done_urls.add(url)

                # Save after every game — Ctrl-C at any point is safe
                _flush_games()
                print(f" ✓ [saved to {os.path.basename(GAMES_OUT)}]")

                time.sleep(SLEEP_BOXSCORE)

    if all_new:
        final_df = pd.read_csv(GAMES_OUT)
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
    print(f"Output folder:   {DATA_DIR}")
    print(f"Player stats output: {PLAYERS_OUT}")
    print(f"Game info output:    {GAMES_OUT}")
    if not os.path.isdir(DATA_DIR):
        print(f"  (folder doesn't exist yet — it will be created automatically)")
    print()

    if not args.skip_players:
        print("=== SCRAPING PLAYER STATS ===")
        scrape_player_stats(years)

    if not args.skip_games:
        print("\n=== SCRAPING GAME INFO ===")
        scrape_game_info(years)

    print("\nDone.")
