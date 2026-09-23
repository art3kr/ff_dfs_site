"""
scrapers/bet_tracker.py
--------------------------------------------------
Keeps the betting research honest: archives every odds scrape, logs every
bet a finder script flags, and grades them once games are played. Without
this, there's no way to tell whether find_ev_bets.py / find_td_bets.py /
find_low_line_props.py actually find edges or just find noise.

Three commands:

  snapshot   Copy the current market comparison + game odds files into
             data/odds_archive/<year>_wk<NN>/, stamped with the scrape
             time (the file's modified time). Run it after every scrape.
             The last snapshot before a game's kickoff is its CLOSING line.

  log        Append the rows of one or more finder outputs to
             data/bet_log/<year>_wk<NN>.csv with the time they were flagged.
             Re-logging the same bet at the same price is ignored, so the
             earliest flag of a price is what gets graded.

  grade      For a week (or the whole season), grade every logged bet
             against PFR player stats and compare each price to the
             closing line:
               result     win / loss / push / void / pending / ungradeable
               profit     per 1 unit staked
               clv_pct    your decimal odds / the same book's closing decimal
                          odds, minus 1, at the same line. Beating the close
                          consistently is the best early evidence of a real
                          edge, long before win/loss records mean anything.
               close_fair_prob / ev_at_close
                          no-vig consensus of every real sportsbook at the
                          close (two-way markets only), and your price's EV
                          against it.

Grading rules, stated rather than implied:
  - No PFR stats row for the player once his team's game is in
    team_points_by_week = void. PFR game logs include every game a player
    was active for, even with all zeros, so no row means he didn't play,
    and books void props on players who don't play.
  - Anytime TD counts rushing, receiving, kick/punt return and fumble
    recovery TDs; not passing TDs.
  - Longest reception / rush / completion and kicking points can't be
    graded from box score totals: 'ungradeable'.

Usage:
    python scrapers/bet_tracker.py snapshot
    python scrapers/bet_tracker.py log data/scoresandodds_market_comparison_ev_bets.csv \\
        data/scoresandodds_market_comparison_td_bets.csv
    python scrapers/bet_tracker.py grade --year 2026 --week 2
    python scrapers/bet_tracker.py grade --year 2026          (whole season)
"""

import argparse
import glob
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
ARCHIVE_DIR = os.path.join(DATA_DIR, 'odds_archive')
LOG_DIR = os.path.join(DATA_DIR, 'bet_log')
PICKEM_BOOKS = {'prizepicks', 'underdog', 'sleeper'}

LOG_COLUMNS = ['logged_at', 'source', 'year', 'week', 'book', 'player_name', 'team',
               'category', 'side', 'line', 'odds', 'model_prob', 'ev_pct', 'confidence']
BET_KEY = ['source', 'book', 'player_name', 'category', 'side', 'line', 'odds']

STAT_FOR_CATEGORY = {
    'passing-yards':               lambda g: g['pass_yds'],
    'passing-tds':                 lambda g: g['pass_td'],
    'completions':                 lambda g: g['pass_cmp'],
    'pass-attempts':               lambda g: g['pass_att'],
    'interceptions':               lambda g: g['pass_int'],
    'rushing-yards':               lambda g: g['rush_yds'],
    'rush-attempts':               lambda g: g['rush_att'],
    'receiving-yards':             lambda g: g['rec_yds'],
    'receptions':                  lambda g: g['rec'],
    'rushing-and-receiving-yards': lambda g: g['rush_yds'] + g['rec_yds'],
    'passing-and-rushing-yards':   lambda g: g['pass_yds'] + g['rush_yds'],
    'touchdowns':                  lambda g: (g['rush_td'] + g['rec_td'] + g['kick_ret_td']
                                              + g['punt_ret_td'] + g['fumbles_rec_td']),
}

_NAME_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv|v)$")


def normalize_name(name: str) -> str:
    # Same rule as app.normalize_name(); not imported, since importing app.py
    # connects to whatever DATABASE_URL .env points at.
    key = re.sub(r"[^a-z0-9 ]", "", str(name).lower().strip())
    key = re.sub(r"\s+", " ", key)
    stripped = _NAME_SUFFIX_RE.sub("", key).strip()
    return stripped if " " in stripped else key


def implied_prob(odds: float) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def decimal_odds(odds: float) -> float:
    return 1 + odds / 100 if odds > 0 else 1 + 100 / -odds


def current_week(today: date = None) -> tuple:
    """Earliest week whose last game falls on or after the current week's
    rollover, i.e. the most recent Tuesday. Mirrors
    app._get_current_nfl_week() / app._week_rollover_cutoff(), but reads the
    schedule CSVs so this script never has to import app.py.

    One deliberate difference: app.py rolls over at 4 AM ET Tuesday, and the
    schedule CSVs only carry dates, so this rolls at the start of Tuesday
    Eastern. The gap is the small hours of Tuesday morning, when nothing is
    being scraped anyway.

    Dates are ET and the two seasons spell them differently (2025 writes
    9/4/25, 2026 writes 2026-09-09), hence format="mixed".
    """
    from zoneinfo import ZoneInfo
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    # Monday=0, Tuesday=1: step back to the most recent Tuesday (today, if
    # today is Tuesday).
    rollover = today - timedelta(days=(today.weekday() - 1) % 7)
    for path in sorted(glob.glob(os.path.join(DATA_DIR, 'schedules', '*_schedule_df.csv')))[-2:]:
        sched = pd.read_csv(path)
        last_game = sched.groupby(['year', 'week'])['date'].max().reset_index()
        upcoming = last_game[pd.to_datetime(last_game['date'], format='mixed').dt.date >= rollover]
        if not upcoming.empty:
            row = upcoming.sort_values(['year', 'week']).iloc[0]
            return int(row['year']), int(row['week'])
    raise SystemExit("Couldn't work out the current week from data/schedules; pass --year/--week.")


def kickoffs_by_team(game_odds_path: str = None) -> dict:
    """team -> kickoff (UTC), from the scraped game odds file."""
    path = game_odds_path or os.path.join(DATA_DIR, 'scoresandodds_game_odds.csv.gz')
    if not os.path.exists(path):
        return {}
    odds = pd.read_csv(path)
    out = {}
    for r in odds.itertuples():
        try:
            out[r.team] = datetime.strptime(r.kickoff, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return out


def started_teams(now: datetime = None, game_odds_path: str = None) -> set:
    """Teams whose game has already kicked off. A prop on one of those is not
    bettable, and near kickoff its quotes go stale or get pulled one book at a
    time, which is exactly when a single bad price looks like the whole market
    (Hard Rock had Kendre Miller at -1800 anytime TD on 2026-09-20 while every
    other book was +750 to +1000 and ScoresAndOdds projected 0.0)."""
    now = now or datetime.now(timezone.utc)
    return {team for team, kick in kickoffs_by_team(game_odds_path).items() if kick <= now}


def drop_started(market: pd.DataFrame, label: str = '', game_odds_path: str = None,
                 now: datetime = None) -> pd.DataFrame:
    started = started_teams(now=now, game_odds_path=game_odds_path)
    if not started or 'team' not in market.columns:
        return market
    mask = market['team'].isin(started)
    if mask.any():
        print(f"Dropping {int(mask.sum()):,} quotes on games already kicked off"
              f"{(' (' + label + ')') if label else ''}: {', '.join(sorted(started))}")
    return market[~mask]


def cmd_snapshot(args):
    year, week = (args.year, args.week) if args.week else current_week()
    out_dir = os.path.join(ARCHIVE_DIR, f'{year}_wk{week:02d}')
    os.makedirs(out_dir, exist_ok=True)
    for label, path in (('market_comparison', args.market), ('game_odds', args.game_odds)):
        if not os.path.exists(path):
            print(f"  skipped {label}: {path} not found")
            continue
        scraped = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        dest = os.path.join(out_dir, f"{label}_{scraped:%Y%m%d_%H%M}.csv.gz")
        if os.path.exists(dest):
            print(f"  {label}: already archived ({os.path.basename(dest)})")
            continue
        shutil.copy2(path, dest)
        print(f"  {label} -> {dest}")


def _normalize_finder_output(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """The three finders write slightly different columns; map them onto
    LOG_COLUMNS."""
    out = pd.DataFrame({
        'book': df['book'].str.lower(),
        'player_name': df['player_name'],
        'team': df.get('team'),
        'category': df['category'] if 'category' in df else 'touchdowns',
        'side': df['side'] if 'side' in df else 'Over',
        'line': df['line'] if 'line' in df else 0.5,
        'odds': df['odds'],
        'model_prob': df['model_prob'] if 'model_prob' in df else df.get('fair_prob'),
        'ev_pct': df['ev_pct'],
        'confidence': df['confidence'] if 'confidence' in df else '',
    })
    # Anytime TD 'Yes' is the Over 0.5 of the touchdowns market.
    out.loc[out['side'] == 'Yes', 'side'] = 'Over'
    out['source'] = source
    return out


def cmd_log(args):
    year, week = (args.year, args.week) if args.week else current_week()
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f'{year}_wk{week:02d}.csv')
    existing = pd.read_csv(log_path) if os.path.exists(log_path) else pd.DataFrame(columns=LOG_COLUMNS)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    new = []
    for path in args.bets:
        df = pd.read_csv(path)
        if df.empty:
            continue
        df = df[df['ev_pct'] >= args.min_ev_pct]
        source = re.sub(r'^scoresandodds_market_comparison_?|\.csv$', '', os.path.basename(path)) or 'bets'
        rows = _normalize_finder_output(df, source)
        rows['logged_at'], rows['year'], rows['week'] = now, year, week
        new.append(rows)
    if not new:
        print("Nothing to log.")
        return
    new = pd.concat(new, ignore_index=True)[LOG_COLUMNS]
    combined = pd.concat([existing, new], ignore_index=True)
    before = len(existing)
    combined = combined.drop_duplicates(subset=BET_KEY, keep='first')
    combined.to_csv(log_path, index=False)
    print(f"Logged {len(combined) - before} new bets ({len(new) - (len(combined) - before)} "
          f"already logged at the same price) -> {log_path}")


def _closing_snapshot(year: int, week: int, kickoff_utc: datetime):
    """The last archived market comparison scraped before kickoff."""
    snaps = sorted(glob.glob(os.path.join(ARCHIVE_DIR, f'{year}_wk{week:02d}', 'market_comparison_*.csv.gz')))
    best = None
    for s in snaps:
        stamp = datetime.strptime(os.path.basename(s)[len('market_comparison_'):-len('.csv.gz')],
                                  '%Y%m%d_%H%M').replace(tzinfo=timezone.utc)
        if stamp < kickoff_utc:
            best = s
    return best


def _kickoffs(year: int, week: int) -> dict:
    snaps = sorted(glob.glob(os.path.join(ARCHIVE_DIR, f'{year}_wk{week:02d}', 'game_odds_*.csv.gz')))
    if not snaps:
        return {}
    g = pd.read_csv(snaps[-1])
    return {r.team: datetime.strptime(r.kickoff, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            for r in g.itertuples()}


def _grade_row(bet, stats_week: pd.DataFrame, teams_final: set) -> tuple:
    category = bet['category']
    if category not in STAT_FOR_CATEGORY:
        return 'ungradeable', None
    key = normalize_name(bet['player_name'])
    rows = stats_week[stats_week['key'] == key]
    if rows.empty and isinstance(bet['team'], str):
        rows = stats_week[(stats_week['last_name'] == key.split()[-1])
                          & (stats_week['team'] == bet['team'])]
    if rows.empty:
        if isinstance(bet['team'], str) and bet['team'] in teams_final:
            return 'void', None
        return 'pending', None
    actual = float(STAT_FOR_CATEGORY[category](rows.iloc[0]))
    line = float(bet['line'])
    if actual == line:
        return 'push', actual
    won = actual > line if bet['side'] == 'Over' else actual < line
    return ('win' if won else 'loss'), actual


def _closing_prices(bet, close: pd.DataFrame) -> dict:
    out = {'close_odds': None, 'clv_pct': None, 'close_fair_prob': None, 'ev_at_close': None}
    if close is None:
        return out
    same = close[(close['key'] == normalize_name(bet['player_name']))
                 & (close['category'] == bet['category'])
                 & (close['line'] == bet['line'])]
    col = 'over_odds' if bet['side'] == 'Over' else 'under_odds'
    at_book = same[same['book'] == bet['book']].dropna(subset=[col])
    if not at_book.empty:
        close_odds = float(at_book[col].iloc[0])
        out['close_odds'] = int(close_odds)
        out['clv_pct'] = round((decimal_odds(bet['odds']) / decimal_odds(close_odds) - 1) * 100, 2)
    two_way = same[~same['book'].isin(PICKEM_BOOKS)].dropna(subset=['over_odds', 'under_odds'])
    if len(two_way) >= 2:
        p_over = (two_way['over_odds'].map(implied_prob)
                  / (two_way['over_odds'].map(implied_prob) + two_way['under_odds'].map(implied_prob))).median()
        fair = p_over if bet['side'] == 'Over' else 1 - p_over
        out['close_fair_prob'] = round(float(fair), 4)
        out['ev_at_close'] = round((fair * decimal_odds(bet['odds']) - 1) * 100, 2)
    return out


def cmd_grade(args):
    pattern = f'{args.year}_wk{args.week:02d}.csv' if args.week else f'{args.year}_wk[0-9][0-9].csv'
    logs = sorted(glob.glob(os.path.join(LOG_DIR, pattern)))
    if not logs:
        print(f"No bet logs matching {os.path.join(LOG_DIR, pattern)}")
        return

    stats = pd.read_csv(os.path.join(DATA_DIR, 'pfr_player_stats_2014_2025.csv.gz'))
    stats = stats[stats['year'] == args.year]
    stats['key'] = stats['name_normalized'].map(normalize_name)
    stats['last_name'] = stats['key'].str.split().str[-1]
    team_points = pd.read_csv(os.path.join(DATA_DIR, 'team_points_by_week.csv.gz'))

    graded = []
    for log_path in logs:
        bets = pd.read_csv(log_path)
        if bets.empty:
            continue
        week = int(bets['week'].iloc[0])
        stats_week = stats[stats['week'] == week]
        teams_final = set(team_points[(team_points['year'] == args.year)
                                      & (team_points['week'] == week)]['team'])
        kickoffs = _kickoffs(args.year, week)
        close_cache = {}
        for _, bet in bets.iterrows():
            result, actual = _grade_row(bet, stats_week, teams_final)
            profit = {'win': decimal_odds(bet['odds']) - 1, 'loss': -1.0}.get(result, 0.0)
            close = None
            kickoff = kickoffs.get(bet['team']) if isinstance(bet['team'], str) else None
            snap = _closing_snapshot(args.year, week, kickoff) if kickoff else None
            if snap:
                if snap not in close_cache:
                    c = pd.read_csv(snap)
                    c['book'] = c['book'].str.lower()
                    if 'available' in c.columns:
                        c = c[c['available'].astype(str).str.lower() != 'false']
                    c['key'] = c['player_name'].map(normalize_name)
                    close_cache[snap] = c
                close = close_cache[snap]
            graded.append({**bet.to_dict(), 'result': result, 'actual': actual,
                           'profit': round(profit, 4) if result in ('win', 'loss') else 0.0,
                           'closing_snapshot': os.path.basename(snap) if snap else '',
                           **_closing_prices(bet, close)})

    result = pd.DataFrame(graded)
    suffix = f'wk{args.week:02d}' if args.week else 'season'
    out_path = os.path.join(LOG_DIR, f'{args.year}_{suffix}_graded.csv')
    result.to_csv(out_path, index=False)

    settled = result[result['result'].isin(['win', 'loss', 'push'])]
    print(f"{len(result)} logged bets: " + ", ".join(
        f"{k} {v}" for k, v in result['result'].value_counts().items()))
    if not settled.empty:
        summary = settled.groupby(['source', 'book']).agg(
            bets=('result', 'size'),
            wins=('result', lambda s: int((s == 'win').sum())),
            units=('profit', 'sum'),
            avg_claimed_ev=('ev_pct', 'mean'),
            avg_clv_pct=('clv_pct', 'mean'),
            avg_ev_at_close=('ev_at_close', 'mean'))
        summary['roi_pct'] = (summary['units'] / summary['bets'] * 100)
        print("\n" + summary.round(2).to_string())
        print("\nSmall samples: a few dozen bets can't separate skill from luck. "
              "avg_clv_pct and avg_ev_at_close settle much faster than ROI.")
    print(f"\nSaved -> {out_path}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)

    s = sub.add_parser('snapshot', help='Archive the current odds files.')
    s.add_argument('--market', default=os.path.join(DATA_DIR, 'scoresandodds_market_comparison.csv.gz'))
    s.add_argument('--game-odds', default=os.path.join(DATA_DIR, 'scoresandodds_game_odds.csv.gz'))
    s.add_argument('--year', type=int)
    s.add_argument('--week', type=int)

    l = sub.add_parser('log', help='Log flagged bets from finder outputs.')
    l.add_argument('bets', nargs='+')
    l.add_argument('--min-ev-pct', type=float, default=0.0)
    l.add_argument('--year', type=int)
    l.add_argument('--week', type=int)

    g = sub.add_parser('grade', help='Grade logged bets and measure closing line value.')
    g.add_argument('--year', type=int, required=True)
    g.add_argument('--week', type=int)

    args = parser.parse_args()
    {'snapshot': cmd_snapshot, 'log': cmd_log, 'grade': cmd_grade}[args.command](args)


if __name__ == '__main__':
    main()
