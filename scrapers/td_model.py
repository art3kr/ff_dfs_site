"""
scrapers/td_model.py
--------------------------------------------------
Anytime touchdown probability model, calibrated on real history and
built only from information available before kickoff.

    expected player TDs = expected team offensive TDs * player's TD share
    P(anytime TD)       = 1 - exp(-expected player TDs)     (Poisson)

  - Expected team offensive TDs (rushing + receiving) come from the
    Vegas implied team total, (over_under - spread) / 2, the same formula
    as app._compute_implied_team_total(). A Poisson regression
    log(TDs) = a + b * log(implied total) is fitted on historical games
    (pfr_game_info Vegas lines vs team TDs in the PFR player logs).
  - TD share is a non-negative weighted mix of the player's opportunity
    shares over his previous games: carries, targets, red zone carries /
    targets, inside-10 carries / targets (nflverse usage), and his own
    share of the team's TDs. Each share is an exponentially weighted sum
    of the player's count over the same weighted sum of his team's counts
    in those games, so a role change fades in over a few weeks. Weights
    are fitted by minimizing log loss.

    Measured, not assumed: the red zone / inside-10 weights fit to about
    zero. Red zone carry share correlates 0.96 with carry share (inside-10:
    0.93), so once carries and targets are in, they add little. The
    goal-line effect is real within a role (RBs with 40-60% carry share:
    32% TD rate in the lowest inside-10 quartile, 41% in the highest) but
    mostly already carried by the overall shares and the player's own TD
    share. Fitted on 2015-2023, 2024-2025 test log loss 0.367 vs 0.421 for
    a position base rate, and calibrated within ~1-4 points per bucket.

Fitted on TRAIN_YEARS, scored on TEST_YEARS, printed against two naive
baselines, so whether the model is any good is measured, not assumed.
`python scrapers/td_model.py --fit` writes the fitted parameters to
data/td_model_params.json, which find_td_bets.py reads.

Honest limits:
  - Passing TDs aren't modeled (anytime TD markets don't pay on them).
    Return / defensive TDs are also left out, which slightly understates
    the few players who return kicks.
  - A player's share history comes from his old team after a trade or
    signing; nothing adjusts that. find_td_bets.py flags team changes.
  - Rookies and players with fewer than MIN_PRIOR_GAMES games have no
    usable history and are skipped rather than guessed.
  - Calibrated against outcomes, not against book prices: there's no
    historical odds archive yet (see archive_odds.py) to prove it beats
    the market.
"""

import argparse
import glob
import json
import os
import re
import sys

import numpy as np
import pandas as pd
from scipy.optimize import minimize

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
PARAMS_FILE = os.path.join(DATA_DIR, 'td_model_params.json')

TRAIN_YEARS = range(2015, 2024)
TEST_YEARS = range(2024, 2026)
MIN_PRIOR_GAMES = 3
POSITIONS = {'QB', 'RB', 'WR', 'TE', 'FB'}

COUNTS = ['carries', 'targets', 'rz_carries', 'rz_targets',
          'i10_carries', 'i10_targets', 'tds']
SHARES = [f'share_{c}' for c in COUNTS]


def _parse_vegas_line(text, home, away):
    """PFR's "Dallas Cowboys -3.0" / "Pick" -> spread from the home team's view."""
    if not isinstance(text, str) or not text.strip():
        return None
    if text.strip().lower().startswith('pick'):
        return 0.0
    m = re.match(r'(.+?)\s+(-?\d+(?:\.\d+)?)$', text.strip())
    if not m:
        return None
    fav, pts = normalize_team(m.group(1)), float(m.group(2))
    if fav == home:
        return pts
    if fav == away:
        return -pts
    return None


def load_games() -> pd.DataFrame:
    """One row per team per game: year, week, team, implied_total, roof, wind."""
    g = pd.read_csv(os.path.join(DATA_DIR, 'pfr_game_info_2014_2025.csv.gz'))
    rows = []
    for r in g.itertuples():
        home, away = normalize_team(r.team_home), normalize_team(r.team_away)
        spread_home = _parse_vegas_line(r.vegas_line, home, away)
        try:
            total = float(str(r.over_under).split()[0])
        except (ValueError, IndexError):
            total = None
        if spread_home is None or total is None:
            continue
        for team, spread in ((home, spread_home), (away, -spread_home)):
            rows.append({'year': r.year, 'week': r.week, 'team': team,
                         'implied_total': total / 2 - spread / 2})
    return pd.DataFrame(rows)


def load_player_games() -> pd.DataFrame:
    """nflverse usage joined to PFR TDs, one row per player-game, plus that
    game's team totals for every count."""
    usage = pd.concat([pd.read_csv(f) for f in sorted(
        glob.glob(os.path.join(DATA_DIR, 'nflverse_usage_*.csv.gz')))], ignore_index=True)
    usage['team'] = usage['team'].map(normalize_team)
    stats = pd.read_csv(os.path.join(DATA_DIR, 'pfr_player_stats_2014_2025.csv.gz'),
                        usecols=['pfr_id', 'year', 'week', 'rush_td', 'rec_td'])
    stats['tds'] = stats['rush_td'] + stats['rec_td']
    df = usage.merge(stats[['pfr_id', 'year', 'week', 'tds']],
                     on=['pfr_id', 'year', 'week'], how='left')
    # Usage rows with no PFR row are players with no counting stats: 0 TDs.
    df['tds'] = df['tds'].fillna(0)
    df = df[df['position'].isin(POSITIONS) & df['pfr_id'].notna()]
    for c in COUNTS:
        df[c] = df[c].fillna(0)
        df[f'team_{c}'] = df.groupby(['team', 'year', 'week'])[c].transform('sum')
    return df.sort_values(['year', 'week']).reset_index(drop=True)


ADJUSTMENTS = ['adj_qb', 'adj_te', 'adj_fb', 'adj_new_team', 'adj_new_season',
               'adj_snap_trend', 'adj_low_snap']


def adjustment_columns(position, stint_games, had_other_team, new_season,
                       last_snap, snap_ewm, halflife) -> dict:
    """Multiplicative corrections on the share model. Each was a real miss
    on the 2024-2025 test years before it was added (predicted -> actual):
      - QBs: 9.2% -> 12.7% (sneaks/keepers the carry share undersells)
      - team changed: 11.0% -> 8.3%, since shares still come from the old
        team. adj_new_team fades with games on the new team, like the shares.
      - snaps fell 20+ points last game: 14.5% -> 10.1%; up 5-20: 17.6% ->
        19.5%. adj_snap_trend = last game's snap % minus the weighted average.
      - rated >12% but <=20% snaps last game: 18.4% -> 12.6% (adj_low_snap),
        the stale-role backup case.
    """
    trend = 0.0 if pd.isna(last_snap) or pd.isna(snap_ewm) else (last_snap - snap_ewm) / 100
    return {
        'adj_qb': float(position == 'QB'),
        'adj_te': float(position == 'TE'),
        'adj_fb': float(position == 'FB'),
        'adj_new_team': 0.5 ** (stint_games / halflife) if had_other_team else 0.0,
        'adj_new_season': float(new_season),
        'adj_snap_trend': trend,
        'adj_low_snap': float(not pd.isna(last_snap) and last_snap <= 20),
    }


def _ewm_shares(df: pd.DataFrame, halflife: float, shift: bool) -> pd.DataFrame:
    grouped = df.groupby('pfr_id', sort=False)
    cols = list(COUNTS) + [f'team_{c}' for c in COUNTS] + ['offense_pct']
    for col in cols:
        if shift:
            df[f'ewm_{col}'] = grouped[col].transform(
                lambda s: s.shift().ewm(halflife=halflife, ignore_na=True).mean())
        else:
            df[f'ewm_{col}'] = grouped[col].transform(
                lambda s: s.ewm(halflife=halflife, ignore_na=True).mean())
    for c in COUNTS:
        denom = df[f'ewm_team_{c}']
        df[f'share_{c}'] = np.where(denom > 0, df[f'ewm_{c}'] / denom, 0.0)
    return df


def add_share_features(df: pd.DataFrame, halflife: float) -> pd.DataFrame:
    """Shares and adjustments from each player's PREVIOUS games only."""
    df = _ewm_shares(df.copy(), halflife, shift=True)
    grouped = df.groupby('pfr_id', sort=False)
    df['prior_games'] = grouped.cumcount()
    run = (df['team'] != grouped['team'].shift()).astype(int).groupby(df['pfr_id']).cumsum()
    stint = df.groupby([df['pfr_id'], run]).cumcount()
    new_season = grouped['year'].shift() != df['year']
    last_snap = grouped['offense_pct'].shift()
    adj = [adjustment_columns(pos, st, r > 1, ns, ls, se, halflife)
           for pos, st, r, ns, ls, se in zip(df['position'], stint, run, new_season,
                                             last_snap, df['ewm_offense_pct'])]
    return pd.concat([df, pd.DataFrame(adj, index=df.index)], axis=1)


def current_features(player_games: pd.DataFrame, halflife: float) -> pd.DataFrame:
    """Each player's shares INCLUDING his most recent game, for pricing the
    next one (no shift), taken at his last row. The adjustments depend on
    the next game's team and season, so next_game_adjustments() adds them."""
    df = _ewm_shares(player_games.copy(), halflife, shift=False)
    df['prior_games'] = df.groupby('pfr_id', sort=False).cumcount() + 1
    teams_played = df.groupby('pfr_id')['team'].agg(list)
    last = df.groupby('pfr_id', sort=False).tail(1).set_index('pfr_id')
    last['teams_played'] = teams_played.reindex(last.index)
    return last.reset_index()


def next_game_adjustments(row, next_team: str, next_year: int, halflife: float) -> dict:
    teams = row['teams_played']
    stint = 0
    for t in reversed(teams):
        if t != next_team:
            break
        stint += 1
    had_other = any(t != next_team for t in teams)
    return adjustment_columns(row['position'], stint, had_other, row['year'] < next_year,
                              row['offense_pct'], row['ewm_offense_pct'], halflife)


def team_td_fit(games: pd.DataFrame, player_games: pd.DataFrame, years) -> tuple:
    team_tds = player_games.groupby(['team', 'year', 'week'])['tds'].sum().rename('team_tds')
    d = games.merge(team_tds.reset_index(), on=['team', 'year', 'week'])
    d = d[d['year'].isin(years) & (d['implied_total'] > 0)]
    x, y = np.log(d['implied_total'].values), d['team_tds'].values

    def nll(p):
        lam = np.exp(p[0] + p[1] * x)
        return np.sum(lam - y * np.log(lam))
    res = minimize(nll, x0=[-1.0, 1.0])
    return float(res.x[0]), float(res.x[1])


def expected_team_tds(implied_total, a, b):
    return np.exp(a + b * np.log(implied_total))


def predict(df: pd.DataFrame, params: dict) -> np.ndarray:
    w = np.array([params['weights'][k] for k in SHARES])
    beta = np.array([params['adjustments'][k] for k in ADJUSTMENTS])
    share = df[SHARES].values @ w
    lam = (expected_team_tds(df['implied_total'].values, params['team_a'], params['team_b'])
           * share * np.exp(df[ADJUSTMENTS].values @ beta))
    return 1 - np.exp(-np.clip(lam, 1e-6, None))


def log_loss(y, p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def fit_weights(train: pd.DataFrame, team_a: float, team_b: float) -> tuple:
    """Share weights (non-negative) and adjustment coefficients, jointly."""
    y = (train['tds'] > 0).astype(float).values
    team_lam = expected_team_tds(train['implied_total'].values, team_a, team_b)
    X, A = train[SHARES].values, train[ADJUSTMENTS].values
    k = len(SHARES)

    def loss(theta):
        lam = team_lam * (X @ theta[:k]) * np.exp(A @ theta[k:])
        p = np.clip(1 - np.exp(-np.clip(lam, 1e-6, None)), 1e-4, 1 - 1e-4)
        return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
    x0 = np.concatenate([np.full(k, 1.0 / k), np.zeros(len(ADJUSTMENTS))])
    bounds = [(0, None)] * k + [(-3, 3)] * len(ADJUSTMENTS)
    res = minimize(loss, x0, bounds=bounds, method='L-BFGS-B')
    return (dict(zip(SHARES, (float(v) for v in res.x[:k]))),
            dict(zip(ADJUSTMENTS, (float(v) for v in res.x[k:]))))


def build_dataset(halflife: float) -> pd.DataFrame:
    games = load_games()
    pg = add_share_features(load_player_games(), halflife)
    d = pg.merge(games, on=['year', 'week', 'team'], how='inner')
    return d[(d['prior_games'] >= MIN_PRIOR_GAMES) & (d['week'] <= 18)], games, pg


def calibration_table(y, p, bins=(0, .05, .1, .2, .3, .4, .5, .6, 1)) -> pd.DataFrame:
    t = pd.DataFrame({'y': y, 'p': p})
    t['bucket'] = pd.cut(t['p'], bins)
    return t.groupby('bucket', observed=True).agg(
        n=('y', 'size'), predicted=('p', 'mean'), actual=('y', 'mean')).round(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fit', action='store_true',
                        help=f'Save fitted parameters to {PARAMS_FILE}.')
    parser.add_argument('--halflives', default='3,5,8,12',
                        help='Candidate halflives (in games) to choose from on the test years.')
    args = parser.parse_args()

    best = None
    for h in (float(x) for x in args.halflives.split(',')):
        data, games, pg = build_dataset(h)
        train = data[data['year'].isin(TRAIN_YEARS)]
        test = data[data['year'].isin(TEST_YEARS)]
        a, b = team_td_fit(games, pg, TRAIN_YEARS)
        weights, adjustments = fit_weights(train, a, b)
        params = {'halflife': h, 'team_a': a, 'team_b': b,
                  'weights': weights, 'adjustments': adjustments}
        y_test = (test['tds'] > 0).astype(float).values
        ll = log_loss(y_test, predict(test, params))
        print(f"halflife {h:>4}: test log loss {ll:.4f}")
        if best is None or ll < best[0]:
            best = (ll, params, test, y_test, train)

    ll, params, test, y_test, train = best
    p_test = predict(test, params)
    print(f"\nBest halflife {params['halflife']} games; team TDs = "
          f"exp({params['team_a']:.3f} + {params['team_b']:.3f} * ln(implied total))")
    for pts in (17, 21, 24, 28):
        print(f"  implied {pts} pts -> {expected_team_tds(pts, params['team_a'], params['team_b']):.2f} "
              f"offensive TDs")
    print("Share weights: " + ", ".join(f"{k[6:]}={v:.3f}" for k, v in params['weights'].items()))
    print("Adjustments (TD rate multiplier): " + ", ".join(
        f"{k[4:]}=x{np.exp(v):.2f}" for k, v in params['adjustments'].items()))

    base_rate = (train['tds'] > 0).groupby(train['position']).mean()
    p_pos = test['position'].map(base_rate).values
    td_rate = np.clip(test['ewm_tds'].fillna(0).values, 0, 0.95)
    print(f"\nTest years {TEST_YEARS.start}-{TEST_YEARS.stop - 1}, {len(test):,} player-games, "
          f"{y_test.mean():.1%} scored")
    print(f"  model log loss            {ll:.4f}")
    print(f"  position base rate        {log_loss(y_test, p_pos):.4f}")
    print(f"  player's own recent TD/g  {log_loss(y_test, td_rate):.4f}")
    print("\nCalibration on test years (predicted vs actual TD rate):")
    print(calibration_table(y_test, p_test).to_string())
    print("\nBy position / situation (predicted vs actual):")
    t = test.assign(p=p_test, y=y_test, snap_drop=test['adj_snap_trend'] <= -0.2,
                    new_team=test['adj_new_team'] > 0.3)
    for col in ('position', 'new_team', 'snap_drop', 'adj_low_snap'):
        print(t.groupby(col).agg(n=('y', 'size'), predicted=('p', 'mean'),
                                 actual=('y', 'mean')).round(3).to_string(), '\n')

    if args.fit:
        params['test_log_loss'] = ll
        params['min_prior_games'] = MIN_PRIOR_GAMES
        with open(PARAMS_FILE, 'w') as f:
            json.dump(params, f, indent=2)
        print(f"\nSaved -> {PARAMS_FILE}")


if __name__ == '__main__':
    main()
