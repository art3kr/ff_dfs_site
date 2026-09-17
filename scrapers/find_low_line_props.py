"""
scrapers/find_low_line_props.py
--------------------------------------------------
Every very low line (0.5 / 1.0 by default) at the books you can bet
(default DraftKings and Caesars), with an analysis alongside each.

The angle: a receiving yards 0.5 or receptions 0.5 line is really
"does he catch one pass", and a rushing yards 0.5 is "does he get a
positive carry". Those are yes/no events that game logs answer
directly, so each quote is compared against the player's own record
instead of a yardage model (find_ev_bets.py handles normal lines).

Categories that are ALWAYS at 0.5 (anytime TD, passing TDs, INTs) are
left out: they're standard markets, not the low-usage-player angle.

Per quote, both sides:
  - hist_rate: the player's recent games (2025 + 2026, last
    LOOKBACK_GAMES he appeared in) that would have gone Over / Under,
    as a raw count. weighted_rate is the same thing with each game's
    weight halving every HALF_LIFE_GAMES games back: a receiver who was
    a starter in September and a rotational guy by December (Keon
    Coleman 2025: 11 of 13 games with 2+ catches, but 28-56% snaps and
    0-1 targets in his last three) would otherwise look like a lock.
    Longest reception/rush at 0.5 use receiving/rushing yards > 0.5 as
    a stand-in (a positive-yardage catch or carry is the same event;
    PFR logs don't carry per-play yardage).
  - market_prob: no-vig P(side) from OTHER real sportsbooks quoting the
    same line (pick'em apps never count). When no other book hangs the
    line, it's the target book's own price de-vigged, flagged
    market_source='own book' (so it carries no outside information).
  - model_prob: weighted_rate shrunk toward market_prob, counting the
    market as SHRINK_GAMES extra (weighted) games. Small samples on
    backups are noisy, and the market knows about role changes that
    old game logs don't.
  - ev_pct at the book's actual price, from model_prob.

Team context (shown, not modeled): team pass attempts per game and the
average number of different players with a catch per game, 2025 + 2026.
That's the "teams that pass a lot and spread it around" angle.

Caveats, not glossed over:
  - Role changes break history: team_changed flags a new team, and
    last3_rate shows whether the recent role matches the long sample.
  - A catch for 0 or negative yards loses a 0.5 receiving-yards Over.
  - An active player with a tiny snap share just loses the Over; an
    inactive one usually has the bet voided.
  - ScoresAndOdds only carries each book's MAIN line, not alt/milestone
    ladders ("1+ receptions"), so those never appear here.

Usage:
    python scrapers/find_low_line_props.py
    python scrapers/find_low_line_props.py --books draftkings,caesars --max-line 1.0
"""

import argparse
import re

import pandas as pd

PICKEM_BOOKS = {'prizepicks', 'underdog', 'sleeper'}

# category -> function of a PFR game-log frame giving the stat compared to the line
CATEGORY_STATS = {
    'receiving-yards':             lambda g: g['rec_yds'],
    'longest-reception':           lambda g: g['rec_yds'],
    'receptions':                  lambda g: g['rec'],
    'rushing-yards':               lambda g: g['rush_yds'],
    'longest-rush':                lambda g: g['rush_yds'],
    'rush-attempts':               lambda g: g['rush_att'],
    'rushing-and-receiving-yards': lambda g: g['rush_yds'] + g['rec_yds'],
    'completions':                 lambda g: g['pass_cmp'],
    'pass-attempts':               lambda g: g['pass_att'],
    'passing-yards':               lambda g: g['pass_yds'],
}
SHRINK_GAMES = 6
LOOKBACK_GAMES = 16
HALF_LIFE_GAMES = 4

_NAME_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv|v)$")


def normalize_name(name: str) -> str:
    # Same rule as app.normalize_name(); copied rather than imported, since
    # importing app.py connects to whatever DATABASE_URL .env points at.
    key = re.sub(r"[^a-z0-9 ]", "", str(name).lower().strip())
    key = re.sub(r"\s+", " ", key)
    stripped = _NAME_SUFFIX_RE.sub("", key).strip()
    return stripped if " " in stripped else key


def implied_prob(odds: float) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def decimal_odds(odds: float) -> float:
    return 1 + odds / 100 if odds > 0 else 1 + 100 / -odds


def novig_over(over_odds: float, under_odds: float) -> float:
    po, pu = implied_prob(over_odds), implied_prob(under_odds)
    return po / (po + pu)


def team_context(stats: pd.DataFrame) -> pd.DataFrame:
    games = stats.groupby(['team', 'year', 'week']).agg(
        pass_att=('pass_att', 'sum'),
        players_with_catch=('rec', lambda s: int((s > 0).sum())))
    return games.groupby('team').agg(
        team_pass_att_pg=('pass_att', 'mean'),
        players_with_catch_pg=('players_with_catch', 'mean')).round(1)


def analyze(market: pd.DataFrame, stats: pd.DataFrame, target_books: set,
            max_line: float) -> pd.DataFrame:
    market = market.copy()
    market['book'] = market['book'].str.lower()
    if 'available' in market.columns:
        market = market[market['available'].astype(str).str.lower() != 'false']
    market = market[market['category'].isin(CATEGORY_STATS)
                    & ~market['book'].isin(PICKEM_BOOKS)].dropna(subset=['line', 'over_odds'])
    market['key'] = market['player_name'].map(normalize_name)

    stats = stats[stats['year'] >= 2025].sort_values(['year', 'week']).copy()
    stats['key'] = stats['name_normalized'].map(normalize_name)
    stats['last_name'] = stats['key'].str.split().str[-1]
    teams = team_context(stats)

    targets = market[market['book'].isin(target_books) & (market['line'] <= max_line)]
    rows = []
    for t in targets.itertuples():
        refs = market[(market['key'] == t.key) & (market['category'] == t.category)
                      & (market['line'] == t.line) & (market['book'] != t.book)].dropna(subset=['under_odds'])
        if not refs.empty:
            market_over = refs.apply(lambda r: novig_over(r.over_odds, r.under_odds), axis=1).median()
            market_source = f"{len(refs)} other book(s)"
        elif not pd.isna(t.under_odds):
            market_over = novig_over(t.over_odds, t.under_odds)
            market_source = 'own book'
        else:
            market_over, market_source = None, 'none'

        logs = stats[stats['key'] == t.key]
        if logs.empty and isinstance(t.team, str):
            # "Joshua Palmer" at the books is "Josh Palmer" on PFR: fall back
            # to last name + team, only when that's a single player.
            same = stats[(stats['last_name'] == t.key.split()[-1]) & (stats['team'] == t.team)]
            if same['key'].nunique() == 1:
                logs = stats[stats['key'] == same['key'].iloc[0]]
        logs = logs.tail(LOOKBACK_GAMES)
        n = len(logs)
        weights = pd.Series([0.5 ** ((n - 1 - i) / HALF_LIFE_GAMES) for i in range(n)],
                            index=logs.index)
        if n:
            over_hits = CATEGORY_STATS[t.category](logs) > t.line
            team_now = logs['team'].iloc[-1]
            by_year = logs.groupby('year')['team'].last()
            team_changed = bool(2025 in by_year and 2026 in by_year and by_year[2025] != by_year[2026])
        else:
            over_hits, team_now, team_changed = None, t.team, None
        ctx = teams.loc[team_now] if team_now in teams.index else None

        for side, odds in (('Over', t.over_odds), ('Under', t.under_odds)):
            if pd.isna(odds):
                continue
            hits = None if over_hits is None else (over_hits if side == 'Over' else ~over_hits)
            m = None if market_over is None else (market_over if side == 'Over' else 1 - market_over)
            weighted = None if hits is None else (weights * hits).sum() / weights.sum()
            if hits is not None and m is not None:
                model = ((weights * hits).sum() + SHRINK_GAMES * m) / (weights.sum() + SHRINK_GAMES)
            elif hits is not None:
                model = weighted
            else:
                model = m
            rows.append({
                'book': t.book, 'player_name': t.player_name, 'team': team_now,
                'category': t.category, 'line': t.line, 'side': side, 'odds': int(odds),
                'implied_prob': round(implied_prob(odds), 3),
                'model_prob': None if model is None else round(model, 3),
                'ev_pct': None if model is None else round((model * decimal_odds(odds) - 1) * 100, 1),
                'hist_rate': '' if hits is None else f"{int(hits.sum())}/{n}",
                'weighted_rate': None if weighted is None else round(weighted, 3),
                'last3_rate': '' if hits is None else f"{int(hits.tail(3).sum())}/{min(3, n)}",
                'market_prob': None if m is None else round(m, 3),
                'market_source': market_source,
                'avg_targets': round(logs['rec_tgt'].mean(), 1) if n else None,
                'avg_carries': round(logs['rush_att'].mean(), 1) if n else None,
                'last_snap_pct': logs['snap_pct'].iloc[-1] if n else None,
                'team_changed': team_changed,
                'team_pass_att_pg': ctx['team_pass_att_pg'] if ctx is not None else None,
                'players_with_catch_pg': ctx['players_with_catch_pg'] if ctx is not None else None,
            })

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values('ev_pct', ascending=False, na_position='last')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_market_comparison.csv.gz")
    parser.add_argument("--stats", default="data/pfr_player_stats_2014_2025.csv.gz")
    parser.add_argument("--books", default="draftkings,caesars")
    parser.add_argument("--max-line", type=float, default=1.0)
    parser.add_argument("--output", default=None,
                        help="Default: <input>_low_line_props.csv")
    args = parser.parse_args()

    target_books = {b.strip().lower() for b in args.books.split(',')}
    result = analyze(pd.read_csv(args.input), pd.read_csv(args.stats), target_books, args.max_line)
    out_path = args.output or (args.input.replace('.csv.gz', '').replace('.csv', '')
                               + '_low_line_props.csv')

    if result.empty:
        print(f"No lines <= {args.max_line} at {sorted(target_books)} in {args.input}.")
        pd.DataFrame().to_csv(out_path, index=False)
        return
    result.to_csv(out_path, index=False)
    print(f"{len(result)} priced sides on lines <= {args.max_line} at {sorted(target_books)}\n")
    print(result.to_string(index=False))
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
