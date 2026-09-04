"""
scrapers/scrape_pfr.py
----------------------
Scrapes Pro Football Reference for detailed weekly player stats + game info
for seasons 2014-2025.

Two output files:
  data/pfr_player_stats_2014_2025.csv.gz   — one row per player per week
  data/pfr_game_info_2014_2025.csv.gz      — one row per game (weather, Vegas, etc.)

Player stats columns:
  pfr_id, name, name_normalized, year, week, game_date, team, opponent,
  home_away, position, dk_pts,
  pass_cmp, pass_att, pass_yds, pass_td, pass_int,
  rush_att, rush_yds, rush_td,
  rec_tgt, rec, rec_yds, rec_td, fumbles_lost,
  snap_pct

dk_pts is computed here via calculate_dk_points() (standard DraftKings
PPR formula) from real raw stats, since the source page has no
pre-computed DK points column of its own.

Game info columns (from boxscore):
  boxscore_url, year, week, team_home, team_away,
  roof, surface, weather, attendance, vegas_line, over_under

Run:
    python scrapers/scrape_pfr.py --years 2014-2025
    python scrapers/scrape_pfr.py --years 2024-2025   # incremental update

NOTE on what --years actually controls: it decides which players get
DISCOVERED (a player only gets found if they appear in at least one of
the requested years' season list). But once a player is found, EVERY
season on their career page gets captured, not just the years you
asked for — the page already contains their whole career at no extra
request cost, so there's no reason to throw the rest away. This means
running --years 2024 alone will still backfill a long-tenured player's
entire history for free, and a later run with a different --years
range will correctly skip anyone already fully captured.

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

DATA SOURCE (confirmed via live debug output, Aug 2026): player stats
come from each player's career gamelog page —
    /players/X/XXXX00/gamelog/
— which covers their ENTIRE career in ONE request, with real full-game
box score numbers. This superseded two earlier wrong assumptions:
  - /fantasy/{year}/ only has redzone (inside-20/inside-10) splits, not
    full-game totals, despite plain-looking field names.
  - /gamelog/advanced/ has full pass_cmp/pass_att/rush_att/rush_yds/
    targets/rec/rec_yds, but NO touchdown, interception, or pass_yds
    columns at all.
A player active across all 12 requested years is fetched once, not 12
times, cutting total requests roughly proportional to career length.

RELIABILITY: if 5 requests in a row fail, the scraper assumes your
PFR_CF_CLEARANCE cookie has expired and stops itself immediately with
instructions, rather than grinding through hundreds of players uselessly
while blocked. Refresh the cookie and re-run the same command — it
resumes exactly where it stopped.

Rate limiting:
  - 4-second sleep between player pages
  - 3-second sleep between boxscore pages
  - Retries with backoff on 403/429 responses
  - Saves to disk after every player and every game — Ctrl-C at any
    point leaves a valid, fully up-to-date file, and re-running the
    same command picks up exactly where you left off.

Run it overnight — if the cookie expires partway through, refresh it
and re-run; nothing already scraped is lost.

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
SLEEP_PLAYER     = 4.0      # seconds between player pages (matches your original proven cadence — the earlier 403 blocking turned out to be Cloudflare cookie expiry, not rate limiting, so 429 backoff below is sufficient safety net)
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
    'pfr_id', 'name', 'name_normalized', 'year', 'week', 'game_date',
    'team', 'opponent', 'home_away', 'position', 'dk_pts',
    'pass_cmp', 'pass_att', 'pass_yds', 'pass_td', 'pass_int',
    'rush_att', 'rush_yds', 'rush_td',
    'rec_tgt', 'rec', 'rec_yds', 'rec_td', 'fumbles_lost', 'fumbles_rec_td',
    'kick_ret', 'kick_ret_yds', 'kick_ret_td',
    'punt_ret', 'punt_ret_yds', 'punt_ret_td',
    'snap_pct',
]

GAME_COLUMNS = [
    'boxscore_url', 'year', 'week', 'team_home', 'team_away', 'date', 'time', 'location',
    'won_toss', 'won_ot_toss', 'roof', 'surface', 'duration',
    'attendance', 'vegas_line', 'over_under',
    'temp', 'humidity', 'wind',
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
    - 429 (rate limited): respects a Retry-After header if PFR sends
      one; otherwise waits SLEEP_ON_429 seconds, DOUBLING on each
      successive retry within this call (exponential backoff) — a
      flat wait repeated 3 times isn't long enough if the real
      throttle window is longer than that, so each retry gives the
      rate limit progressively more room to actually clear.
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
    backoff_429 = SLEEP_ON_429
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
            # PFR may tell us exactly how long to wait — more reliable
            # than our own guess if it's present.
            retry_after = r.headers.get('Retry-After')
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = backoff_429
            else:
                wait = backoff_429
            print(f"  Rate limited (429). Waiting {wait:.0f}s before retry {attempt+1}/{MAX_RETRIES}"
                  f"{' (server-specified via Retry-After)' if retry_after else ' (exponential backoff)'}...")
            time.sleep(wait)
            backoff_429 *= 2   # double for next attempt, in case this one is still too short
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


def find_table(soup: BeautifulSoup, table_id: str):
    """
    Find a table by id, checking both the live HTML and inside HTML
    comments. PFR frequently wraps supplementary tables (anything beyond
    the single "main" table on a page) inside <!-- --> comments — this
    is presumably to make the raw page lighter and slightly harder to
    scrape naively. Our boxscore game_info parsing already had to work
    around this; the per-year player fantasy table does too.
    """
    table = soup.find('table', id=table_id)
    if table:
        return table

    # Not in live HTML — search inside HTML comments
    comment_soup = BeautifulSoup(
        "\n".join(soup.find_all(string=Comment)), 'lxml'
    )
    return comment_soup.find('table', id=table_id)


def calculate_dk_points(pass_yds=0, pass_td=0, pass_int=0,
                        rush_yds=0, rush_td=0,
                        rec=0, rec_yds=0, rec_td=0,
                        fumbles_lost=0,
                        kick_ret_td=0, punt_ret_td=0,
                        fumbles_rec_td=0) -> float:
    """
    Standard DraftKings NFL PPR scoring formula. Used to compute fantasy
    points ourselves from raw box-score stats, since the career gamelog
    page (unlike the per-year fantasy page) doesn't include a pre-computed
    DK points column.

    kick_ret_td / punt_ret_td: return touchdowns, worth 6 pts each, same
    as any other TD — confirmed necessary by comparing against PFR's own
    historical dk_pts (compare_dk_points.py): a cluster of -6.0
    discrepancies belonged to known return specialists (Rashid Shaheed,
    Marvin Mims, Kalif Raymond, Xavier Gipson, Tyler Lockett, etc.) — the
    return TD data was present in the 'stats' table all along
    (kick_ret_td/punt_ret_td fields) but wasn't being extracted or scored.

    fumbles_rec_td: offensive fumble recovery touchdown (also 6 pts) —
    same discovery process, confirmed as the source of a second, smaller
    cluster of -6.0 discrepancies (Trey McBride, Travis Kelce, Keenan
    Allen games) not explained by return TDs.

    No yardage bonus applies to return yards under standard DK rules —
    the 100+ yard bonus is rushing/receiving only.

    KNOWN REMAINING GAP: 2-point conversions (worth 2 pts each) are NOT
    captured here. A second discrepancy pattern in compare_dk_points.py —
    almost entirely QB rows, off by amounts landing very close to
    multiples of 2 (e.g. -2.04, -4.02, -4.04) — strongly suggests missed
    2-point conversions. That data isn't available on the player gamelog
    page this scraper uses; it typically lives in a game's scoring/
    play-by-play log instead, a genuinely different data source. This
    formula does not attempt to account for it.
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
    pts += kick_ret_td * 6
    pts += punt_ret_td * 6
    pts += fumbles_rec_td * 6
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

    soup = BeautifulSoup(r.content, 'lxml')
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
#
# NOTE: an earlier version of this function tried to fetch a player's
# entire career in one request via /gamelog/ (no year in the URL), to
# cut total requests. That page's row data didn't reliably expose a
# 'year_id' column the way assumed, and returned 0 rows for every
# player when tested. Reverted to the proven per-year approach, which
# was verified working with real data in earlier testing.
# ---------------------------------------------------------------------------

def get_player_career_stats(pfr_id: str, name: str, player_url: str, position: str,
                            date_to_week_cache: dict,
                            session: requests.Session) -> list[dict] | None:
    """
    Hit the player's career gamelog page ONCE (all years, one request)
    and return EVERY regular-season game found — not just the years
    the current run happened to request via --years.

    Why: this page already contains the player's entire career in one
    HTTP response, at no extra request cost. Throwing away years
    outside the current --years range would mean re-fetching this same
    page again later if a different year range is ever requested for
    this player — wasteful, since nothing about the response changes.
    Capturing everything now means later runs (even with a completely
    different --years range) skip this player entirely once they're
    already in the output file, for any year found on their career page.

    Confirmed via live debug output (Aug 2026): the PLAIN (non-advanced)
    gamelog page — /players/X/XXXX00/gamelog/ — has a 'stats' table with
    real, full-game box score numbers: pass_cmp, pass_att, pass_yds,
    pass_td, pass_int, rush_att, rush_yds, rush_td, targets, rec,
    rec_yds, rec_td, fumbles_lost, snap_counts_off_pct. This superseded
    two earlier wrong assumptions:
      1. /fantasy/{year}/ — only has redzone (inside-20/inside-10) splits,
         not full-game totals, despite plain-looking field names.
      2. /gamelog/advanced/ — has full pass_cmp/pass_att/rush_att/
         rush_yds/targets/rec/rec_yds, but NO touchdown, interception,
         or pass_yds columns at all.

    There's also a separate 'stats_playoffs' table on this same page —
    intentionally ignored, since we only want the regular season
    (weeks 1-18), consistent with the rest of this codebase.

    Since this page has no pre-computed DK points column, dk_pts is
    computed here via calculate_dk_points() using the real raw stats.

    `date_to_week_cache` is a dict of {year: {date_str: week}}, built
    lazily per year via load_date_to_week() as different years are
    encountered across this player's career — including years the
    caller never explicitly asked for.

    Returns None if the request itself failed after retries (signals
    the circuit breaker), [] if the page had no usable games at all.
    """
    url = PFR_BASE + re.sub(r'\.htm$', '', player_url) + '/gamelog/'
    r   = pfr_get(session, url, use_headers=True)

    if r is None:
        return None

    soup  = BeautifulSoup(r.content, 'lxml')
    table = find_table(soup, 'stats')   # regular season only — NOT 'stats_playoffs'
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
            cell = row.find('td', attrs={'data-stat': stat})
            return cell.get_text(strip=True) if cell else ''

        try:
            game_date_full = get('date')
            if not game_date_full:
                continue
            game_year = int(game_date_full[:4])

            if game_year not in date_to_week_cache:
                date_to_week_cache[game_year] = load_date_to_week(game_year)

            week_num = date_to_week_cache[game_year].get(game_date_full[:10], 0)
            if week_num == 0:
                continue   # not in our regular-season schedule (preseason, no schedule file for that year, etc.)

            team      = get('team_name_abbr')
            opponent  = get('opp_name_abbr')
            game_loc  = get('game_location')     # '@' = away, '' = home
            home_away = 'a' if game_loc.strip() == '@' else 'h'

            pass_cmp  = _int(get('pass_cmp'))
            pass_att  = _int(get('pass_att'))
            pass_yds  = _int(get('pass_yds'))
            pass_td   = _int(get('pass_td'))
            pass_int  = _int(get('pass_int'))

            rush_att  = _int(get('rush_att'))
            rush_yds  = _int(get('rush_yds'))
            rush_td   = _int(get('rush_td'))

            rec_tgt   = _int(get('targets'))
            rec       = _int(get('rec'))
            rec_yds   = _int(get('rec_yds'))
            rec_td    = _int(get('rec_td'))

            fumbles_lost = _int(get('fumbles_lost'))
            fumbles_rec_td = _int(get('fumbles_rec_td'))

            kick_ret     = _int(get('kick_ret'))
            kick_ret_yds = _int(get('kick_ret_yds'))
            kick_ret_td  = _int(get('kick_ret_td'))
            punt_ret     = _int(get('punt_ret'))
            punt_ret_yds = _int(get('punt_ret_yds'))
            punt_ret_td  = _int(get('punt_ret_td'))

            snap_raw  = get('snap_counts_off_pct')
            snap_pct  = _float(snap_raw) if snap_raw else None

            dk_pts = calculate_dk_points(
                pass_yds=pass_yds, pass_td=pass_td, pass_int=pass_int,
                rush_yds=rush_yds, rush_td=rush_td,
                rec=rec, rec_yds=rec_yds, rec_td=rec_td,
                fumbles_lost=fumbles_lost,
                kick_ret_td=kick_ret_td, punt_ret_td=punt_ret_td,
                fumbles_rec_td=fumbles_rec_td,
            )

            rows.append({
                'pfr_id':          pfr_id,
                'name':            name,
                'name_normalized': normalize_name(name),
                'year':            game_year,
                'week':            week_num,
                'game_date':       game_date_full[:10],
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
                'fumbles_lost':    fumbles_lost,
                'fumbles_rec_td':  fumbles_rec_td,
                'kick_ret':        kick_ret,
                'kick_ret_yds':    kick_ret_yds,
                'kick_ret_td':     kick_ret_td,
                'punt_ret':        punt_ret,
                'punt_ret_yds':    punt_ret_yds,
                'punt_ret_td':     punt_ret_td,
                'snap_pct':        snap_pct,
            })

        except Exception:
            continue

    return rows


# ---------------------------------------------------------------------------
# Step 3: Scrape game info from boxscores (weather, Vegas, etc.)
# ---------------------------------------------------------------------------

def parse_weather(weather_raw: str) -> tuple:
    """
    Split PFR's single 'Weather' string into (temp, humidity, wind).
    Typical raw format: '60 degrees, relative humidity 74%, wind 5 mph'
    (separators and presence of each part can vary — indoor games often
    have no weather row at all, which is handled upstream by leaving
    weather_raw empty).
    Returns (temp, humidity, wind) as strings, '' for any part not found.
    """
    if not weather_raw:
        return '', '', ''

    temp_match = re.search(r'(-?\d+)\s*degrees', weather_raw, re.IGNORECASE)
    humidity_match = re.search(r'relative humidity\s*(\d+)%', weather_raw, re.IGNORECASE)
    wind_match = re.search(r'wind\s*(\d+)\s*mph', weather_raw, re.IGNORECASE)

    temp     = f"{temp_match.group(1)} degrees" if temp_match else ''
    humidity = f"relative humidity {humidity_match.group(1)}%" if humidity_match else ''
    wind     = f"wind {wind_match.group(1)} mph" if wind_match else ''

    return temp, humidity, wind


def get_game_info(boxscore_url: str, year: int, week: int,
                  team_home: str, team_away: str,
                  date: str, time: str, location: str,
                  session: requests.Session) -> dict:
    """
    Hit one PFR boxscore page and extract the game_info table.

    date/time/location are passed straight through from the local
    schedule CSV (they don't need to be scraped) so callers get a
    complete row without a second lookup.

    Field parsing: each row's own <th> (title) is paired directly with
    its own <td> (value). Tested against two plausible table layouts
    (with and without a leading header-only row) and confirmed correct
    in both — safer than the flat titles/values list with a fixed index
    offset from an earlier version, which broke under both layouts once
    actually tested.
    """
    url = PFR_BASE + boxscore_url
    r   = pfr_get(session, url, use_headers=False)

    result = {
        'boxscore_url': boxscore_url,
        'year': year, 'week': week,
        'team_home': team_home, 'team_away': team_away,
        'date': date, 'time': time, 'location': location,
        'won_toss': '', 'won_ot_toss': '', 'roof': '', 'surface': '',
        'duration': '', 'attendance': '', 'vegas_line': '', 'over_under': '',
        'temp': '', 'humidity': '', 'wind': '',
        '_request_failed': False,
    }

    if r is None:
        result['_request_failed'] = True
        return result

    soup  = BeautifulSoup(r.content, 'lxml')
    # game_info table is inside an HTML comment on PFR
    soup2 = BeautifulSoup("\n".join(soup.find_all(string=Comment)), 'lxml')
    table = soup2.find('table', id='game_info')
    if not table:
        return result

    # Pair each row's own <th> (title) with its own <td> (value) directly.
    # This is immune to indexing bugs from a flat titles/values list —
    # a leading header-only row (<th colspan=2>Game Info</th> with no
    # <td>) is simply skipped since it has no value to pair, rather than
    # silently shifting every subsequent title/value pair out of sync
    # (which is what broke an earlier version of this function, and what
    # a flat-list-plus-fixed-offset approach also turned out not to
    # reliably fix once tested against more than one table layout).
    field_map = {
        'Won Toss':    'won_toss',
        'Won OT Toss': 'won_ot_toss',
        'Roof':        'roof',
        'Surface':     'surface',
        'Duration':    'duration',
        'Attendance':  'attendance',
        'Vegas Line':  'vegas_line',
        'Over/Under':  'over_under',
        # 'Weather' handled separately below — gets split into temp/humidity/wind
    }

    weather_raw = ''
    for row in table.find_all('tr'):
        th = row.find('th')
        td = row.find('td')
        if th is None or td is None:
            continue   # header-only row or malformed row — skip
        title = th.get_text(strip=True)
        value = td.get_text(' ', strip=True)

        if title == 'Weather':
            weather_raw = value
        elif title in field_map:
            result[field_map[title]] = value

    result['temp'], result['humidity'], result['wind'] = parse_weather(weather_raw)

    # Safety net: if the table was found but every field still came back
    # empty, something about the table structure differs from what we
    # expect — dump the raw rows so we can diagnose with real data
    # instead of guessing again.
    non_meta_keys = [k for k in result if not k.startswith('_') and
                     k not in ('boxscore_url', 'year', 'week', 'team_home',
                               'team_away', 'date', 'time', 'location')]
    if all(not result[k] for k in non_meta_keys):
        print(f"\n    [DEBUG] game_info table found for {url} but every field "
              f"came back empty.")
        for row in table.find_all('tr'):
            print(f"    [DEBUG] row: {row.get_text(' | ', strip=True)}")

    return result


# ---------------------------------------------------------------------------
# Step 4: Load schedule from local CSV files (faster than scraping PFR)
# ---------------------------------------------------------------------------

SCHEDULES_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'schedules')


def load_date_to_week(year: int) -> dict:
    """
    Build a {date_string: week_number} lookup from the local schedule CSV.

    PFR's per-player fantasy page has a 'game_num' field (the player's Nth
    game played that season) but NO actual NFL week number field. game_num
    silently diverges from the real week after a player's bye — e.g. their
    5th game played might be Week 6 if they had a Week 3 bye. Rather than
    trust PFR's internal numbering, we derive the real week from the game's
    calendar date against our own trusted schedule files (the same ones
    used for game info scraping).
    """
    path = os.path.join(SCHEDULES_DIR, f'{year}_schedule_df.csv')
    if not os.path.exists(path):
        print(f"  WARNING: no schedule file for {year} — cannot map game "
              f"dates to weeks, all games for this year will be skipped")
        return {}

    df = pd.read_csv(path)
    df = df[df['team_2'] != 'BYE']
    df = df[df['week'].apply(lambda w: str(w).isdigit() or isinstance(w, int))]
    df['week'] = df['week'].astype(int)
    df = df[df['week'] <= 18]

    mapping = {}
    skipped_unparseable = 0
    for _, r in df.iterrows():
        # Parse properly instead of naive string slicing — different
        # years' schedule CSVs use different raw date formats (most are
        # ISO 'YYYY-MM-DD', but at least one year's file uses 'M/D/YY',
        # e.g. '9/4/25'). Naive [:10] slicing silently produced the wrong
        # key for any non-ISO format, causing every lookup for that year
        # to fail with zero errors or warnings. Explicit parsing handles
        # whatever format is actually in the file.
        parsed = pd.to_datetime(r['date'], errors='coerce')
        if pd.isna(parsed):
            skipped_unparseable += 1
            continue
        date_str = parsed.strftime('%Y-%m-%d')
        mapping[date_str] = int(r['week'])

    if skipped_unparseable:
        print(f"  WARNING: {skipped_unparseable} rows in {year} schedule had "
              f"unparseable dates and were skipped")

    return mapping


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

    return df[['year', 'week', 'boxscore_url', 'team_home', 'team_away', 'date', 'time', 'location']].copy()


def _scrape_schedule_from_pfr(year: int) -> pd.DataFrame:
    """Fallback: scrape schedule from PFR if no local file available."""
    url = f"{PFR_BASE}/years/{year}/games.htm"
    print(f"  Fetching schedule from PFR: {url}")
    with make_session() as sched_session:
        r = pfr_get(sched_session, url, use_headers=False)
    if r is None:
        return pd.DataFrame()

    soup  = BeautifulSoup(r.content, 'lxml')
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
    """Scrape player stats for all given years using each player's career
    gamelog page — one request per unique player, not one per player-year."""
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
    consecutive_failures = 0
    FAILURE_CIRCUIT_BREAKER = 5   # stop entirely after this many failures in a row
    date_to_week_cache = {}       # {year: {date_str: week}} — built lazily as needed

    with make_session() as session:
        # -------------------------------------------------------------
        # Pass 1: build a unique player list across ALL requested years.
        # One request per year (cheap), but each player is then only
        # scraped once total, no matter how many of our years they
        # played in — their career page covers every year in one shot.
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

            # Free speed win: the year-list page already gives us each
            # player's season DK point total (dk_pts_season) at no extra
            # request cost. Players with 0 points (inactive squad
            # players, long snappers, etc.) are never going to matter
            # for a DFS site, so skip fetching their detail page.
            before = len(players_df)
            players_df = players_df[players_df['dk_pts_season'] > 0].reset_index(drop=True)
            skipped = before - len(players_df)
            if skipped:
                print(f"  Skipping {skipped} players with 0 season DK points "
                      f"(inactive/irrelevant) — {len(players_df)} remain")

            for _, row in players_df.iterrows():
                pfr_id = row['pfr_id']
                if pfr_id not in unique_players:
                    unique_players[pfr_id] = {
                        'name': row['name'], 'player_url': row['player_url'],
                        'position': row['position'], 'years': set(),
                    }
                unique_players[pfr_id]['years'].add(year)

        total_player_years = sum(len(p['years']) for p in unique_players.values())
        print(f"\n{len(unique_players)} unique players across {len(years)} year(s) "
              f"({total_player_years} player-year combinations — old approach "
              f"would have made {total_player_years} requests, this makes "
              f"{len(unique_players)})")

        # -------------------------------------------------------------
        # Pass 2: scrape each unique player's career page ONCE.
        # -------------------------------------------------------------
        player_ids = list(unique_players.keys())
        for i, pfr_id in enumerate(player_ids):
            info = unique_players[pfr_id]

            if all((pfr_id, y) in done_pairs for y in info['years']):
                print(f"  Skip {info['name']} (all requested years already done)")
                continue

            print(f"  [{i+1}/{len(player_ids)}] {info['name']} ({pfr_id})",
                  end='', flush=True)

            game_rows = get_player_career_stats(
                pfr_id, info['name'], info['player_url'], info['position'],
                date_to_week_cache, session
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
            # Mark every year actually found in this player's career page
            # as done — not just the years this run originally asked for.
            # Since the page already contains their whole career, a later
            # run requesting a totally different --years range will
            # correctly skip re-fetching this player.
            #
            # IMPORTANT: we deliberately do NOT also mark every year in
            # info['years'] as done regardless of whether it produced
            # rows. Doing that (an earlier version of this function did)
            # silently hides real gaps — if a year's games failed to
            # parse for any reason (a week-mapping miss, a page quirk,
            # an interrupted run), that year gets permanently marked
            # "complete" with zero actual data, and every future re-run
            # skips it forever. The small cost of occasionally re-checking
            # a player who legitimately had 0 games in a requested year
            # (e.g. injured reserve all season) is far cheaper than
            # silently losing a real season of data.
            years_found = {row['year'] for row in game_rows}
            for y in years_found:
                done_pairs.add((pfr_id, y))

            # Save after EVERY player — Ctrl-C at any point leaves a
            # valid, fully up-to-date file.
            _flush_player_rows(existing, all_new_rows)
            print(f" → {len(game_rows)} games across {len(years_found)} season(s) "
                  f"[saved to {os.path.basename(PLAYERS_OUT)}]")

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
                                     game['team_home'], game['team_away'],
                                     game.get('date', ''), game.get('time', ''), game.get('location', ''),
                                     session)

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
