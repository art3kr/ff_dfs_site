"""
scrapers/scrape_weekly_weather.py
------------------------------------
Scrapes per-game weather forecasts/conditions from nflweather.com.

Confirmed via live fetch (Aug 2026) that, unlike PFR, this site:
  - Has no Cloudflare-style comment-hiding — content is plain, visible HTML
  - Its /week/{year}/week-{N} URL genuinely respects both parameters
    (verified: /week/2025/week-1 correctly returned real 2025 Week 1
    games, unlike PFR/FantasyPros where year params are silently ignored)

Each game's block (confirmed structure, in order) contains:
  date/time, status (Final/Not Started), "Forecast by NFLWeather.comTM"
  boilerplate, away team link+name, away score, '@', home score,
  home team link+name, a /games/{year}/{week}/{away}-at-{home} detail
  link, temperature ("73 °F"), condition text ("Rain"/"Clear"/etc),
  wind ("air 5 mph south_west SW"), TV info, "Read More" link.

Team slugs in URLs (e.g. /team/cowboys, /team/eagles) are nicknames,
which team_mapping.normalize_team() already resolves correctly (it was
tested against exactly this kind of nickname-only input already).

Output: data/weekly_weather.csv.gz
  Columns: year, week, game_date, status, away_team, home_team,
           away_score, home_score, temp_f, condition,
           wind_mph, wind_direction

Usage:
    python scrapers/scrape_weekly_weather.py --year 2025 --weeks 1-18
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

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
OUTPUT_FILE = os.path.join(DATA_DIR, 'weekly_weather.csv.gz')
BASE_URL    = "https://www.nflweather.com/week/{year}/week-{week}"
SLEEP_SEC   = 2.0

OUT_COLUMNS = ['year', 'week', 'game_date', 'status', 'away_team', 'home_team',
               'away_score', 'home_score', 'temp_f', 'condition',
               'wind_mph', 'wind_direction']

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
}


def _int_or_none(s):
    try:
        return int(re.sub(r'[^\d-]', '', str(s)))
    except (ValueError, TypeError):
        return None


def fetch_week(year: int, week: int) -> list[dict]:
    url = BASE_URL.format(year=year, week=week)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"  Week {week}: request error: {e}")
        return []

    if r.status_code != 200:
        print(f"  Week {week}: HTTP {r.status_code}")
        return []

    soup = BeautifulSoup(r.content, 'lxml')

    # Each game's detail link is the most reliable per-game anchor —
    # confirmed present exactly once per game, with a predictable
    # href pattern.
    game_links = soup.find_all('a', href=re.compile(rf'^/games/{year}/week-{week}/[\w-]+-at-[\w-]+'))

    if not game_links:
        print(f"  Week {week}: no game links found — dumping page structure for diagnosis:")
        all_links = [a.get('href') for a in soup.find_all('a', href=True)][:20]
        print(f"  First 20 links on page: {all_links}")
        return []

    rows = []
    for link in game_links:
        try:
            # Walk up to a reasonably-sized container holding this
            # game's full card — try a few ancestor levels until we
            # find one that also contains team links and weather text.
            container = link
            for _ in range(6):
                container = container.parent
                if container is None:
                    break
                text = container.get_text(' ', strip=True)
                if '/team/' in str(container) and '°F' in text and 'air' in text.lower():
                    break

            if container is None:
                continue

            block_text = container.get_text(' ', strip=True)
            team_links = container.find_all('a', href=re.compile(r'^/team/[\w-]+$'))

            if len(team_links) < 2:
                continue

            away_slug = team_links[0]['href'].split('/team/')[1]
            home_slug = team_links[1]['href'].split('/team/')[1]
            away_team = normalize_team(away_slug)
            home_team = normalize_team(home_slug)

            if not away_team or not home_team:
                failed = []
                if not away_team:
                    failed.append(f"away='{away_slug}'")
                if not home_team:
                    failed.append(f"home='{home_slug}'")
                print(f"    WARNING: couldn't normalize team slug(s): "
                      f"{', '.join(failed)} — skipping this game")
                continue

            # Date/time: e.g. "09/04/25 08:20 PM EDT"
            date_match = re.search(r'\d{2}/\d{2}/\d{2}\s+\d{1,2}:\d{2}\s*[AP]M\s*\w+', block_text)
            game_date = date_match.group(0) if date_match else ''

            status = 'Final' if 'Final' in block_text else (
                     'Not Started' if 'Not Started' in block_text else '')

            # Scores: two numbers immediately around '@' in the raw text.
            # Absent entirely for games that haven't been played yet.
            score_match = re.search(r'(\d+)\s*@\s*(\d+)', block_text)
            away_score = _int_or_none(score_match.group(1)) if score_match else None
            home_score = _int_or_none(score_match.group(2)) if score_match else None

            temp_match = re.search(r'(\d+)\s*°F', block_text)
            temp_f = _int_or_none(temp_match.group(1)) if temp_match else None

            wind_match = re.search(r'air\s+(\d+)\s*mph\s+\w+\s+([A-Z]+)', block_text)
            wind_mph = _int_or_none(wind_match.group(1)) if wind_match else None
            wind_dir = wind_match.group(2) if wind_match else ''

            # Condition text sits between the temperature and "air" —
            # extract it directly rather than guessing a fixed word list.
            condition = ''
            if temp_match and wind_match:
                start = temp_match.end()
                end   = wind_match.start()
                condition = block_text[start:end].strip()

            rows.append({
                'year': year, 'week': week,
                'game_date': game_date, 'status': status,
                'away_team': away_team, 'home_team': home_team,
                'away_score': away_score, 'home_score': home_score,
                'temp_f': temp_f, 'condition': condition,
                'wind_mph': wind_mph, 'wind_direction': wind_dir,
            })

        except Exception as e:
            print(f"    Error parsing a game block: {e}")
            continue

    if rows:
        print(f"  Week {week}: {len(rows)} games parsed")
    else:
        print(f"  Week {week}: 0 games parsed despite finding {len(game_links)} game links — "
              f"container-detection logic may need adjusting")

    return rows


def _save(existing: pd.DataFrame, new_rows: list) -> pd.DataFrame:
    if not new_rows:
        return existing
    new_df = pd.DataFrame(new_rows, columns=OUT_COLUMNS)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=['year', 'week', 'away_team', 'home_team'], keep='last')
    combined = combined.sort_values(['year', 'week', 'away_team'])
    combined.to_csv(OUTPUT_FILE, index=False, compression='gzip')
    return combined


def main(year: int, weeks: list[int]):
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_csv(OUTPUT_FILE)
        print(f"Loaded existing file: {len(existing):,} rows")
    else:
        existing = pd.DataFrame(columns=OUT_COLUMNS)

    for week in weeks:
        print(f"\nWeek {week}:")
        rows = fetch_week(year, week)
        existing = _save(existing, rows)
        time.sleep(SLEEP_SEC)

    print(f"\nDone. {len(existing):,} rows total → {OUTPUT_FILE}")


def parse_weeks(s: str) -> list[int]:
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    elif ',' in s:
        return [int(w) for w in s.split(',')]
    else:
        return [int(s)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--weeks', default='1-18',
                        help='Week(s) to scrape, e.g. "1-18" or "1,2,3" or "5"')
    args = parser.parse_args()

    weeks = parse_weeks(args.weeks)
    main(args.year, weeks)
