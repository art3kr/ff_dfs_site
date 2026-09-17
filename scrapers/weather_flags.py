"""
scrapers/weather_flags.py
--------------------------------------------------
Adds this week's weather forecast to finder outputs (find_ev_bets.py,
find_low_line_props.py, find_td_bets.py) and flags bets that lean with or
against it.

What weather actually does, measured on 2014-2025 PFR games (team-game
totals vs what the Vegas implied team total predicts; standard error
about +-4 pass yards and +-0.5 points per bucket):

    outdoor wind   games   points   pass yds   rush yds
    0-9 mph        2,924    +0.2       +1         +0
    10-14            868    -0.8       -6         -0
    15-19            344    -1.1      -21         +4
    20+               90    +0.5      -22        +15
    freezing (<=32F) 198    -1.0      -18
    dome           1,617    +0.3       +7         -2

So Vegas totals already price weather into SCORING (points and TDs barely
move once the implied total is known), and the TD model needs no weather
term. What a total can't express is the pass/run SPLIT: in 15+ mph wind
or freezing cold, a team throws for about 20 fewer yards at the same
expected score and runs more. That's the lean this script flags:
  - weather_lean = 'against' for Overs on passing props (passing yards,
    completions, pass attempts, longest completion, passing + rushing
    yards) and Unders on rushing props in those games;
  - 'with' for the opposite side.
Whether DraftKings/Caesars already shade those lines isn't known yet;
bet_tracker.py grading is how that gets answered.

Caveats: PFR's weather is what was measured at the game, a forecast days
out is less certain (re-scrape weather closer to kickoff), and
retractable roofs count as domes here (app.TEAM_ROOF_TYPE), even on days
they're open.

Usage:
    python scrapers/weather_flags.py                         (this week's games)
    python scrapers/weather_flags.py data/scoresandodds_market_comparison_ev_bets.csv
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from bet_tracker import current_week

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# Copied from app.TEAM_ROOF_TYPE (importing app.py connects to the database).
TEAM_ROOF_TYPE = {
    'ari': 'dome', 'atl': 'dome', 'bal': 'outdoor', 'buf': 'outdoor',
    'car': 'outdoor', 'chi': 'outdoor', 'cin': 'outdoor', 'cle': 'outdoor',
    'dal': 'dome', 'den': 'outdoor', 'det': 'dome', 'gnb': 'outdoor',
    'hou': 'dome', 'ind': 'dome', 'jax': 'outdoor', 'kan': 'outdoor',
    'lac': 'dome', 'lar': 'dome', 'lvr': 'dome', 'mia': 'outdoor',
    'min': 'dome', 'nwe': 'outdoor', 'nor': 'dome', 'nyg': 'outdoor',
    'nyj': 'outdoor', 'phi': 'outdoor', 'pit': 'outdoor', 'sea': 'outdoor',
    'sfo': 'outdoor', 'tam': 'outdoor', 'ten': 'outdoor', 'was': 'outdoor',
}

WINDY_MPH = 15
FREEZING_F = 32
PASSING_CATEGORIES = {'passing-yards', 'completions', 'pass-attempts', 'longest-completion',
                      'passing-and-rushing-yards'}
RUSHING_CATEGORIES = {'rushing-yards', 'rush-attempts', 'longest-rush'}


def game_weather(year: int, week: int) -> pd.DataFrame:
    """One row per team: home team, roof, forecast wind/temp, and effect note."""
    w = pd.read_csv(os.path.join(DATA_DIR, 'weekly_weather.csv.gz'))
    w = w[(w['year'] == year) & (w['week'] == week)]
    rows = []
    for g in w.itertuples():
        roof = TEAM_ROOF_TYPE.get(g.home_team, 'outdoor')
        wind = None if roof == 'dome' or pd.isna(g.wind_mph) else float(g.wind_mph)
        temp = None if roof == 'dome' or pd.isna(g.temp_f) else float(g.temp_f)
        if roof == 'dome':
            note, bad_passing = 'dome: pass yds +7', False
        elif wind is not None and wind >= 20:
            note, bad_passing = f'{wind:.0f} mph wind: pass yds -22, rush yds +15', True
        elif wind is not None and wind >= WINDY_MPH:
            note, bad_passing = f'{wind:.0f} mph wind: pass yds -21, rush yds +4', True
        elif temp is not None and temp <= FREEZING_F:
            note, bad_passing = f'{temp:.0f}F: pass yds -18', True
        elif wind is not None and wind >= 10:
            note, bad_passing = f'{wind:.0f} mph wind: pass yds -6 (minor)', False
        else:
            note, bad_passing = '', False
        for team in (g.home_team, g.away_team):
            rows.append({'team': team, 'home_team': g.home_team, 'roof': roof,
                         'forecast_wind_mph': wind, 'forecast_temp_f': temp,
                         'condition': g.condition, 'weather_note': note,
                         'bad_passing_weather': bad_passing})
    return pd.DataFrame(rows)


def annotate(bets: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    drop = [c for c in weather.columns if c != 'team' and c in bets.columns] + ['weather_lean']
    out = bets.drop(columns=[c for c in drop if c in bets.columns]).merge(weather, on='team', how='left')
    category = out['category'] if 'category' in out else pd.Series('touchdowns', index=out.index)
    side = out['side'] if 'side' in out else pd.Series('Over', index=out.index)
    passing_over = category.isin(PASSING_CATEGORIES) & (side == 'Over')
    passing_under = category.isin(PASSING_CATEGORIES) & (side == 'Under')
    rushing_over = category.isin(RUSHING_CATEGORIES) & (side == 'Over')
    rushing_under = category.isin(RUSHING_CATEGORIES) & (side == 'Under')
    bad = out['bad_passing_weather'].fillna(False).astype(bool)
    out['weather_lean'] = ''
    out.loc[bad & (passing_over | rushing_under), 'weather_lean'] = 'against'
    out.loc[bad & (passing_under | rushing_over), 'weather_lean'] = 'with'
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bets', nargs='*', help='Finder output CSVs to annotate in place.')
    parser.add_argument('--year', type=int)
    parser.add_argument('--week', type=int)
    args = parser.parse_args()
    year, week = (args.year, args.week) if args.week else current_week()

    weather = game_weather(year, week)
    if weather.empty:
        print(f"No weather rows for {year} week {week} in weekly_weather.csv.gz "
              f"(run scrape_weekly_weather.py).")
        return
    games = weather.drop_duplicates('home_team')
    print(f"{year} week {week} forecasts ({len(games)} games):")
    print(games[['home_team', 'roof', 'forecast_wind_mph', 'forecast_temp_f', 'condition',
                 'weather_note']].to_string(index=False))
    flagged = games[games['bad_passing_weather']]
    print(f"\n{len(flagged)} game(s) with {WINDY_MPH}+ mph wind or <= {FREEZING_F}F forecast"
          + (": " + ", ".join(flagged['home_team']) if len(flagged) else "."))

    for path in args.bets:
        bets = pd.read_csv(path)
        if bets.empty or 'team' not in bets:
            continue
        out = annotate(bets, weather)
        out.to_csv(path, index=False)
        leaning = out[out['weather_lean'] != '']
        print(f"\n{os.path.basename(path)}: {len(leaning)} bet(s) lean with/against the weather")
        if not leaning.empty:
            cols = [c for c in ('book', 'player_name', 'category', 'side', 'line', 'odds', 'ev_pct',
                                'weather_note', 'weather_lean') if c in leaning.columns]
            print(leaning[cols].to_string(index=False))


if __name__ == '__main__':
    main()
