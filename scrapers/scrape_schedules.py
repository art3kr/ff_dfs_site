import random
import re

from bs4 import BeautifulSoup, Comment
import pandas as pd
import requests
import argparse
import os
from scrape_pfr import pfr_get, make_session

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR         = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data'))
SCHEDULES_DIR    = os.path.join(DATA_DIR, 'schedules')


'''request headers'''
user_agent_list = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
    ]

headers = {
    'User-Agent': random.choice(user_agent_list),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0'
            }

PFR_CF_CLEARANCE = os.environ.get("PFR_CF_CLEARANCE", "")
PFR_CF_BM        = os.environ.get("PFR_CF_BM", "")
PFR_USER_AGENT   = os.environ.get(
    "PFR_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
)


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------
schedule_SCHEMA = [
    'year',
    'week',
    'team_1',
    'team_2',
    'date',
    'time',
    'location',
    'boxscore_url',
]

def scrape_schedule(year: int) -> pd.DataFrame:
     
    '''function to get the table of all games scheduled from pro football reference

	input: year [int]
	output: pandas dataframe
	'''
 
    path = os.path.join(SCHEDULES_DIR, f'{year}_schedule_df.csv')
 
    if os.path.exists(path):
        print(f"  Schedule exists for {year}, skipping scrape")
        return

    '''send request, get html table of game schedule'''
    base_url = f'https://www.pro-football-reference.com/years/{str(year)}/games.htm'
    print(f"  Scraping {base_url}")
    
    headers['User-Agent'] = random.choice(user_agent_list)
    
    with make_session() as sched_session:
        r = pfr_get(sched_session, base_url, use_headers=False)

        
        soup = BeautifulSoup(r.content, 'lxml')
        table = soup.find_all('table')[0]
        
        # print(table)

        '''create lists'''
        years = []
        weeks = []
        team_1s = []
        team_2s = []
        dates = []
        times = []
        locations = []
        boxscore_urls = []

        missing_time_count = 0

        '''iterate through html table of games'''
        for index, row in enumerate(table.find('tbody').find_all('tr')):
            
            # print(row)
            # break

            try: 
                '''get teams and weeks'''
                week = int(row.find('th', attrs={'data-stat': 'week_num'}).get_text())
                if week == 0 or week > 18:
                    continue
                try:
                    team_1 = row.find('td', attrs={'data-stat': 'winner'}).get_text()
                    team_2 = row.find('td', attrs={'data-stat': 'loser'}).get_text()
                except:
                    team_1 = row.find('td', attrs={'data-stat': 'visitor_team'}).get_text()
                    team_2 = row.find('td', attrs={'data-stat': 'home_team'}).get_text()
                else:
                    pass
                try:
                    boxscore_element = row.find('td', attrs={'data-stat': 'boxscore_word'})
                    boxscore_url = boxscore_element.a.get('href')
                except:
                    boxscore_url = ''
                
                # Date extraction — PFR changed their schedule page
                # template starting with the 2026 season. The OLD
                # template (2014-2025, confirmed via the uploaded 2025
                # HTML) has an explicit 'game_date' column. The NEW
                # template (confirmed via the uploaded 2026 HTML) has
                # NO 'game_date' column at all — instead, for games not
                # yet played, the date shows up as the boxscore_word
                # cell's link TEXT (e.g. "September 9", no year). But
                # that same cell's text flips to the generic word
                # "boxscore" once the game has actually been played
                # (confirmed: this is also true on the OLD 2025
                # template for already-played games) — so relying on
                # link text isn't reliable across a whole season as
                # games transition from scheduled to played.
                #
                # The robust fix: boxscore_url itself always encodes
                # the date as YYYYMMDD (e.g. /boxscores/202609090sea.htm
                # -> 2026-09-09), regardless of whether the game has
                # been played yet or what page template PFR is using.
                # Confirmed this pattern holds identically on both the
                # 2025 and 2026 uploaded pages. Try this first; only
                # fall back to the old 'game_date' column (still needed
                # for years where boxscore_url might be empty, e.g. a
                # postponed/cancelled game) if the URL doesn't parse.
                date = ''
                if boxscore_url:
                    m = re.search(r'/boxscores/(\d{4})(\d{2})(\d{2})', boxscore_url)
                    if m:
                        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                if not date:
                    try:
                        date = row.find('td', attrs={'data-stat': 'game_date'}).get_text()
                    except:
                        date = ''

                # FIX: gametime wasn't wrapped in its own try/except, so a
                # missing time (very common for games PFR hasn't scheduled
                # an exact kickoff for yet, which is exactly the situation
                # before a season starts) silently dropped the ENTIRE row
                # via the outer bare except below — losing the matchup,
                # date, and boxscore_url too, not just the time. Now a
                # missing time just leaves 'time' blank and the row is
                # still kept, so Week 1 (usually scheduled early) doesn't
                # have to wait on Week 18 (often finalized much later,
                # especially with flex scheduling) before any of it is
                # usable.
                try:
                    time = row.find('td', attrs={'data-stat': 'gametime'}).get_text()
                    if not time.strip():
                        missing_time_count += 1
                except:
                    time = ''
                    missing_time_count += 1

                location = row.find('td', attrs={'data-stat': 'game_location'}).get_text()  

                '''location is not just @, but the city'''
                if location == "@":
                    location = team_2[:team_2.rindex(" ")]
                else:
                    location = team_1[:team_1.rindex(" ")]

                '''append to lists'''
                years.append(year)
                weeks.append(week)
                team_1s.append(team_1)
                team_2s.append(team_2)
                dates.append(date)
                times.append(time)
                locations.append(location)
                boxscore_urls.append(boxscore_url)

            except:
                pass

        if missing_time_count:
            print(f"  NOTE: {missing_time_count} game(s) have no kickoff time yet "
                  f"(PFR hasn't scheduled/announced it) — those rows were still "
                  f"kept with time='', but flask load-schedule will skip them "
                  f"until you re-run this scraper closer to those weeks.")

        '''create dataframe from lists'''
        schedule_df = pd.DataFrame(
            list(zip(years,weeks,team_1s,team_2s,dates,times,locations,boxscore_urls)),
            columns=schedule_SCHEMA)

        return schedule_df

def add_bye_weeks_to_schedule(schedule_df, year):
    pass
    '''function to add bye weeks to schedule dataframe
    input: schedule_df [pandas dataframe], year [int]
    output: pandas dataframe'''

    # get mapping table
    team_mapping_df = pd.read_csv(f'data/mapping_table/team_name_mapping_table.csv')
    teams_list = team_mapping_df['pfr_team'].tolist()

    # iterate through each week to find bye weeks
    for week in schedule_df['week'].unique().tolist():
        week_df = schedule_df[schedule_df['week'] == week]
        if len(week_df) < 16:
            teams_in_week = week_df['team_1'].tolist() + week_df['team_2'].tolist()
            teams_on_bye = [team for team in teams_list if team not in teams_in_week]
            # print(f'Week {week} bye teams: {teams_on_bye}')
            # add bye week rows to schedule_df
            for team in teams_on_bye:
                new_row = {
                    'year': year,
                    'week': week,
                    'team_1': team,
                    'team_2': 'BYE',
                    'date': '',
                    'time': '',
                    'location': '',
                    'boxscore_url': ''
                }
                schedule_df = pd.concat([schedule_df, pd.DataFrame([new_row])], ignore_index=True)

    return schedule_df


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
    parser.add_argument('--years', default='2014-2026',
                        help='Year range, e.g. "2014-2026" or "2024" or "2022,2023"')
    args = parser.parse_args()

    years = parse_years(args.years)
    print(f"Years to scrape: {years}")
    print(f"Schedule output: {SCHEDULES_DIR}")


    # if not os.path.isdir(DATA_DIR):
    #     print(f"  (folder doesn't exist yet — it will be created automatically)")
    # print()
    
    # if not os.path.isdir(SCHEDULES_DIR):
    #     print(f"  (folder doesn't exist yet — it will be created automatically)")
    # print()

    # if not args.skip_players:
    #     print("=== SCRAPING SCHEDULE ===")
    for year in years:
        schedule_df = scrape_schedule(year)
        if schedule_df is not None:
            # schedule_df.to_csv(os.path.join(SCHEDULES_DIR, f'{year}_schedule_df.csv'), index=False)
            # print(f"  Schedule for {year} saved to {os.path.join(SCHEDULES_DIR, f'{year}_schedule_df.csv')}")
            # print(schedule_df)
            schedule_df = add_bye_weeks_to_schedule(schedule_df, year)
            schedule_df.to_csv(f'data/schedules/{year}_schedule_df.csv', index=False)

    print("\nDone.")
