"""
scrapers/find_td_bets.py
--------------------------------------------------
Prices this week's anytime TD props at the books you can bet (default
DraftKings and Caesars) with td_model.py's calibrated probability, and
ranks them by expected value.

Inputs:
  - data/td_model_params.json      (python scrapers/td_model.py --fit)
  - data/scoresandodds_game_odds.csv.gz   spread + total -> implied team total
  - data/scoresandodds_market_comparison.csv.gz  'touchdowns' rows, per book
  - nflverse usage + PFR player logs, through the latest loaded week

Columns worth reading before betting:
  - model_prob / fair_odds: the model's probability and its no-vig price.
  - other_books_implied: median implied probability at every OTHER real
    sportsbook, vig INCLUDED. If model_prob is above even that, the
    model disagrees with the whole market, not just this book.
  - gap_pp: model_prob minus other_books_implied, in percentage points.
    A huge gap is more often something the model can't see (injury,
    new role, goal-line back signed this week) than a free 40% edge.
    Quotes wildly out of line with the other books are left out of that
    median (OUTLIER_RATIO) — a broken feed otherwise becomes "the market"
    once the sane books go unavailable near kickoff.
  - new_team / last_snap_pct / games: the model already discounts a new
    team, a falling snap share and a new season (td_model.ADJUSTMENTS,
    each measured on held-out years), but it still can't see this week's
    depth chart or injuries.
  - team: from the props feed (current); the implied total is looked up
    by that team, so a traded player still gets his new team's total.

Players with fewer than the model's min_prior_games aren't priced, and
neither is anyone whose last game is older than last season (Brevin
Jordan's latest game on file was 2024 week 2; the model priced that role).

Every row gets a `confidence`: 'clean' when the player played this
season, took more than LOW_SNAP_PCT of offensive snaps in his latest game,
and is on the same team; otherwise 'check', with `check_reason`. The
model's real blind spot is offseason role change: Week 2 shares still lean
on last year, so a back who lost his job in the offseason (Bam Knight:
46% snaps late 2025, 2% in his last game) still carries an old share.
The 'check' rows are printed separately and shouldn't be bet on the EV
number alone. A player who is himself on the injury report, or whose key
teammate is out or doubtful (market_context.NewsContext), is also 'check'.

Usage:
    python scrapers/find_td_bets.py
    python scrapers/find_td_bets.py --books draftkings,caesars --min-ev-pct 5
"""

import argparse
import json
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import td_model
from bet_tracker import drop_started
from market_context import NewsContext
from team_mapping import normalize_team

DATA_DIR = td_model.DATA_DIR
PICKEM_BOOKS = {'prizepicks', 'underdog', 'sleeper'}
LOW_SNAP_PCT = 20
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


def american(p: float):
    if p <= 0 or p >= 1:
        return None
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


# A book whose implied probability is this many times the median of the
# others is a broken feed, not a price. Confirmed 2026-09-20: Hard Rock
# listed Troy Franklin's anytime TD at -1600 (94%) while all five other
# books were +750/+800 (~12%), and RJ Harvey at -350 against +210 to +290.
# Left in the market median it turns a normal price into a fake 38-point
# "disagreement", especially near kickoff when the sane books have already
# gone available: false.
OUTLIER_RATIO = 2.5


def market_implied(probs: pd.Series):
    """Median implied probability of the other books, ignoring quotes that
    are wildly out of line with the rest (see OUTLIER_RATIO)."""
    probs = probs.dropna()
    if probs.empty:
        return None, 0
    if len(probs) >= 3:
        med = probs.median()
        keep = probs[(probs <= med * OUTLIER_RATIO) & (probs >= med / OUTLIER_RATIO)]
        if not keep.empty:
            probs = keep
    return float(probs.median()), len(probs)


def implied_totals(game_odds_path: str) -> dict:
    g = pd.read_csv(game_odds_path)
    out = {}
    for r in g.itertuples():
        try:
            total = float(str(r.over_under).lstrip('ou'))
            out[normalize_team(r.team)] = total / 2 - float(r.spread) / 2
        except (TypeError, ValueError):
            continue
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default='data/scoresandodds_market_comparison.csv.gz')
    parser.add_argument('--game-odds', default='data/scoresandodds_game_odds.csv.gz')
    parser.add_argument('--books', default='draftkings,caesars')
    parser.add_argument('--min-ev-pct', type=float, default=0.0)
    parser.add_argument('--top', type=int, default=40)
    parser.add_argument('--include-started', action='store_true',
                        help="Keep props on games that already kicked off.")
    parser.add_argument('--year', type=int, default=2026,
                        help='Season of the games being priced (for the new-season adjustment).')
    parser.add_argument('--output', default=None,
                        help='Default: <input>_td_bets.csv')
    args = parser.parse_args()

    with open(td_model.PARAMS_FILE) as f:
        params = json.load(f)
    target_books = {b.strip().lower() for b in args.books.split(',')}

    market = pd.read_csv(args.input)
    market['book'] = market['book'].str.lower()
    if 'available' in market.columns:
        market = market[market['available'].astype(str).str.lower() != 'false']
    market = market[(market['category'] == 'touchdowns')
                    & ~market['book'].isin(PICKEM_BOOKS)].dropna(subset=['over_odds'])
    if market.empty:
        print(f"No anytime TD quotes in {args.input} (has the scrape reached them?).")
        return
    market['key'] = market['player_name'].map(normalize_name)
    market['team'] = market['team'].map(lambda t: normalize_team(t) if isinstance(t, str) else t)
    if not args.include_started:
        market = drop_started(market, game_odds_path=args.game_odds)

    feats = td_model.current_features(td_model.load_player_games(), params['halflife'])
    feats['key'] = feats['name_normalized'].map(normalize_name)
    feats['last_name'] = feats['key'].str.split().str[-1]
    totals = implied_totals(args.game_odds)

    news = NewsContext()
    rows, unmatched, stale = [], set(), set()
    for (key, team), group in market.groupby(['key', 'team'], dropna=False):
        f = feats[feats['key'] == key]
        if len(f) != 1 and isinstance(team, str):
            # Nickname mismatches ("Joshua" vs "Josh"): last name + team, if unique.
            f = feats[(feats['last_name'] == key.split()[-1]) & (feats['team'] == team)]
        if len(f) != 1:
            unmatched.add(group['player_name'].iloc[0])
            continue
        f = f.iloc[0]
        if f['prior_games'] < params['min_prior_games'] or team not in totals:
            continue
        if f['year'] < args.year - 1:
            stale.add(group['player_name'].iloc[0])
            continue
        adj = td_model.next_game_adjustments(f, team, args.year, params['halflife'])
        row = pd.DataFrame([{**{k: f[k] for k in td_model.SHARES}, **adj,
                             'implied_total': totals[team]}])
        p = float(td_model.predict(row, params)[0])
        reasons = []
        if f['year'] < args.year:
            reasons.append('no game this season')
        if pd.isna(f['offense_pct']) or f['offense_pct'] <= LOW_SNAP_PCT:
            reasons.append(f"{f['offense_pct']:.0f}% snaps last game")
        # adj_new_team = 0.5 ** (games with this team / halflife): > 0.25 means
        # 8 or fewer games here, when the shares still mostly reflect the old team.
        if adj['adj_new_team'] > 0.25:
            reasons.append('new team')
        # A key teammate out moves this player's share in a way the model's
        # history can't see (Puka Nacua out, 2026-09-21: Davante Adams went
        # +115 -> -120, Konata Mumpfield +1600 -> +500).
        reasons += news.news_for(group['player_name'].iloc[0], team)

        for t in group[group['book'].isin(target_books)].itertuples():
            others = group[group['book'] != t.book]['over_odds'].map(implied_prob)
            other_implied, other_n = market_implied(others)
            rows.append({
                'book': t.book, 'player_name': t.player_name, 'team': team,
                'position': f['position'], 'odds': int(t.over_odds),
                'implied_prob': round(implied_prob(t.over_odds), 3),
                'model_prob': round(p, 3), 'fair_odds': american(p),
                'ev_pct': round((p * decimal_odds(t.over_odds) - 1) * 100, 1),
                'other_books_implied': None if other_implied is None else round(other_implied, 3),
                'other_books': other_n,
                'gap_pp': None if other_implied is None else round((p - other_implied) * 100, 1),
                'implied_team_total': round(totals[team], 1),
                'carry_share': round(f['share_carries'], 3),
                'target_share': round(f['share_targets'], 3),
                'td_share': round(f['share_tds'], 3),
                'games': int(f['prior_games']),
                'last_game': f"{int(f['year'])} wk{int(f['week'])} {f['team']}",
                'last_snap_pct': f['offense_pct'],
                'new_team': adj['adj_new_team'] > 0.25,
                'confidence': 'check' if reasons else 'clean',
                'check_reason': '; '.join(reasons),
            })

    result = pd.DataFrame(rows)
    if result.empty:
        print("No anytime TD quotes at the target books could be priced.")
        return
    result = result[result['ev_pct'] >= args.min_ev_pct].sort_values('ev_pct', ascending=False)
    out_path = args.output or (args.input.replace('.csv.gz', '').replace('.csv', '') + '_td_bets.csv')
    result.to_csv(out_path, index=False)

    cols = ['book', 'player_name', 'team', 'position', 'odds', 'fair_odds', 'model_prob',
            'ev_pct', 'other_books_implied', 'gap_pp', 'implied_team_total', 'last_snap_pct']
    clean = result[result['confidence'] == 'clean']
    check = result[result['confidence'] == 'check']
    print(f"=== Clean: {len(clean)} anytime TD quotes at {sorted(target_books)} with "
          f"EV >= {args.min_ev_pct}% ===\n")
    print(clean[cols].head(args.top).to_string(index=False))
    print(f"\n=== Check first (model can't see the current role): {len(check)} ===\n")
    print(check[cols + ['check_reason']].head(args.top).to_string(index=False))
    if stale:
        print(f"\n{len(stale)} players skipped, no game since before {args.year - 1}: "
              f"{sorted(stale)[:8]}")
    if unmatched:
        print(f"\n{len(unmatched)} players not matched to usage history (rookies / name "
              f"mismatches), e.g. {sorted(unmatched)[:8]}")
    print(f"\nSaved -> {out_path}")


if __name__ == '__main__':
    main()
