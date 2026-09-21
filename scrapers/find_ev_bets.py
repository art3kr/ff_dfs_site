"""
scrapers/find_ev_bets.py
--------------------------------------------------
Finds positive expected value (+EV) bets at specific target books
(default: DraftKings and Caesars), priced against a no-vig "fair"
probability built from every OTHER real sportsbook's quote on the
same prop.

The idea: you can only bet at the target books, so the question per
prop is "is this book's price better than the rest of the market says
it should be?" Unlike arbitrage (find_arbitrage_opportunities.py) this
is a single bet with variance; unlike middling it needs no second leg.
It pays off over many bets IF the consensus of the other books is a
better estimate of the truth than the target book's own price. That's
the whole assumption, and it's a real one (see caveats below).

How fair probability is built, per (player, category):
  1. Reference books = real sportsbooks quoting both sides, excluding
     the target book itself. DFS pick'em apps (PrizePicks, Underdog,
     Sleeper) are never references: their "odds" are fixed payout
     multipliers (Underdog/PrizePicks show -137/-137 on everything),
     not prices.
  2. Each reference quote is de-vigged (multiplicative: p_over /
     (p_over + p_under)).
  3. That de-vigged probability, at that book's line, is turned into a
     distribution parameter: a mean for a normal model, or a lambda
     for a Poisson model (see CATEGORY_MODELS). The median across
     reference books is the consensus (a median so one stale quote
     can't drag it, e.g. bet365 sitting at -115/-115 on an INT prop
     every other book has at -200/+150).
  4. The target book's own line is evaluated under that consensus
     distribution, pushes included for whole-number lines, and EV is
     computed for both Over and Under at the target's actual prices.

Step 3 is what lets a DraftKings 264.5 be compared against a FanDuel
267.5: line differences are converted to probability using how spread
out the stat really is. The spreads in CATEGORY_MODELS were measured
from PFR game logs (2018-2025, player-seasons with 8+ games and 20%+
snaps): per-player game-to-game std dev fits sd = a * mean^b well
(roughly a square root), so a 12.5 QB rushing line is far wider,
relative to its size, than a 75.5 RB line. A single std/mean ratio per
stat got that badly wrong on low lines. They're unconditional season
spreads, so a bit WIDER than the true uncertainty around a given
week's line, which makes line-difference edges come out slightly
smaller, not bigger.
Categories with no historical data to calibrate against (longest
reception/rush/completion, kicking points) are compared at the same
line only; no line adjustment.

One-way markets (anytime / first / last TD scorer) have no "No" price
to de-vig with, so they're handled differently (_one_way_rows()) and
hidden unless --one-way is passed. Measured 2026-09-16: one flat assumed
margin is badly wrong there, because books' margin grows toward longshots
(players the market prices at 3.7% average a 1.1% ScoresAndOdds
projection; at 14.8%, 11.1%), so that section fills up with fake longshot
"edges". Use find_td_bets.py for anytime TDs instead; it prices them from
a model, not from other books.

Every bet is marked 'clean' or 'check' (check_reason says why), and the
check ones print separately:
  - the other books' lines are spread over REF_SPREAD_SD or more standard
    deviations. That's what a market mid-move looks like. On 2026-09-21
    Davante Adams' receptions were 3.5 at some books and 5.5 at others after
    Puka Nacua was ruled out; DraftKings had already moved to 5.5, and this
    script called its Under a 14% edge because the lagging books were
    "consensus". The book that moves first on news is usually right.
  - the player, or a key teammate (market_context.key_players()), is on the
    injury report. Only as good as the last injury scrape.

Real caveats, not glossed over:
  - "Fair" here means "consensus of the other books", not truth. None
    of the books ScoresAndOdds shows is a sharp, market-making book
    (no Pinnacle / Circa), so if everyone is wrong together this
    can't see it.
  - Edges of 2-5% are normal noise-level for props. The bigger the
    flagged EV, the more likely it's stale data, a line that moved
    after the scrape, or a player-status change, not a free lunch.
    Check the live price before betting.
  - Books limit accounts that consistently beat their closing lines.
  - EV is per bet in expectation; any single bet is close to a coin
    flip. Kelly sizing is shown at 1/4 Kelly for that reason.

Usage:
    python scrapers/find_ev_bets.py
    python scrapers/find_ev_bets.py --books draftkings,caesars --min-ev-pct 3
"""

import argparse
import math
import os
import statistics
import sys

import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(__file__))
from bet_tracker import drop_started
from market_context import NewsContext

DEFAULT_INPUT = "data/scoresandodds_market_comparison.csv.gz"

# Not sportsbooks: fixed-payout pick'em apps. Never a reference price.
PICKEM_BOOKS = {'prizepicks', 'underdog', 'sleeper'}

# model: 'normal' with sd = a * line**b, or 'poisson'.
# (a, b) fitted from PFR game logs, see module docstring.
# 'same_line' = no calibrated spread, only compare quotes at the target's
# exact line.
CATEGORY_MODELS = {
    'passing-yards':               ('normal', (7.02, 0.41)),
    'passing-and-rushing-yards':   ('normal', (9.88, 0.35)),
    'pass-attempts':               ('normal', (1.26, 0.51)),
    'completions':                 ('normal', (0.74, 0.64)),
    'rushing-yards':               ('normal', (3.33, 0.55)),
    'rush-attempts':               ('normal', (1.09, 0.57)),
    'receiving-yards':             ('normal', (3.14, 0.59)),
    'rushing-and-receiving-yards': ('normal', (3.33, 0.57)),
    'receptions':                  ('poisson', None),
    'passing-tds':                 ('poisson', None),
    'interceptions':               ('poisson', None),
    'longest-reception':           ('same_line', None),
    'longest-rush':                ('same_line', None),
    'longest-completion':          ('same_line', None),
    'kicking-points':              ('same_line', None),
}

# Below this consensus line a normal model means nothing (e.g. a QB's
# "rushing yards 0.5" is really "any rushing yards at all"), so those
# are compared at the same line only.
MIN_NORMAL_LINE = 5.0

ONE_WAY_CATEGORIES = {'touchdowns', 'first-touchdown-scorer', 'last-touchdown-scorer'}

# Reference lines spread over this many standard deviations = a market that's
# mid-move on news, where "consensus" is partly stale. See module docstring.
REF_SPREAD_SD = 0.75


def ref_spread_sd(category: str, ref_lines: str) -> float:
    """Max minus min of the other books' lines, in the stat's standard
    deviations (the same spreads the pricing uses)."""
    try:
        lines = [float(x) for x in str(ref_lines).split(',') if x]
    except ValueError:
        return 0.0
    if len(lines) < 2:
        return 0.0
    model, spread = CATEGORY_MODELS.get(category, ('same_line', None))
    mid = statistics.median(lines)
    if model == 'normal' and mid >= MIN_NORMAL_LINE:
        sd = spread[0] * mid ** spread[1]
    elif model == 'poisson':
        sd = max(mid, 0.5) ** 0.5
    else:
        return 0.0
    return (max(lines) - min(lines)) / sd


def implied_prob(odds: float) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def decimal_odds(odds: float) -> float:
    return 1 + odds / 100 if odds > 0 else 1 + 100 / -odds


def _is_whole(line: float) -> bool:
    return abs(line - round(line)) < 1e-9


def outcome_probs(model: str, param: float, sd: float, line: float):
    """(P(over), P(under), P(push)) at `line`. Stats are integers, so a
    half-point line can't push and a whole line pushes on exactly that
    value (normal model uses a +-0.5 continuity correction)."""
    if model == 'poisson':
        lam = max(param, 1e-6)
        if _is_whole(line):
            k = int(round(line))
            push = stats.poisson.pmf(k, lam)
            under = stats.poisson.cdf(k - 1, lam)
        else:
            push = 0.0
            under = stats.poisson.cdf(math.floor(line), lam)
        return 1 - under - push, under, push

    if _is_whole(line):
        under = stats.norm.cdf(line - 0.5, param, sd)
        over = 1 - stats.norm.cdf(line + 0.5, param, sd)
        return over, under, 1 - over - under
    under = stats.norm.cdf(line, param, sd)
    return 1 - under, under, 0.0


def fit_param(model: str, sd: float, line: float, p_over_novig: float) -> float:
    """Distribution parameter (normal mean / Poisson lambda) at which a
    no-push P(over) at `line` equals the de-vigged probability.
    Bisection: P(over) is monotone increasing in either parameter."""
    lo, hi = (1e-6, max(3 * line, 5.0) + 10) if model == 'poisson' \
        else (line - 8 * sd, line + 8 * sd)
    for _ in range(80):
        mid = (lo + hi) / 2
        over, under, _ = outcome_probs(model, mid, sd, line)
        if over / (over + under) < p_over_novig:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _kelly_quarter(p_win: float, p_push: float, dec: float) -> float:
    # Kelly for a bet that can push (stake returned): f = (p_win*b - p_lose) / b.
    b = dec - 1
    p_lose = 1 - p_win - p_push
    return max(0.0, (p_win * b - p_lose) / b) * 0.25


def _two_way_rows(df: pd.DataFrame, target_books: set, min_refs: int,
                  max_shift_sd: float) -> list:
    out = []
    two_way = df.dropna(subset=['line', 'over_odds', 'under_odds'])
    two_way = two_way[~two_way['book'].isin(PICKEM_BOOKS)
                      & ~two_way['category'].isin(ONE_WAY_CATEGORIES)]

    for (player, category), group in two_way.groupby(['player_name', 'category']):
        category_model, spread = CATEGORY_MODELS.get(category, ('same_line', None))

        for _, target in group[group['book'].isin(target_books)].iterrows():
            model = category_model
            refs = group[group['book'] != target['book']]
            if model == 'normal' and refs['line'].median() < MIN_NORMAL_LINE:
                model = 'same_line'
            if model == 'same_line':
                refs = refs[refs['line'] == target['line']]
            if len(refs) < min_refs:
                continue

            ref_line_median = float(refs['line'].median())
            sd = spread[0] * ref_line_median ** spread[1] if model == 'normal' else None

            params = []
            for _, r in refs.iterrows():
                po, pu = implied_prob(r['over_odds']), implied_prob(r['under_odds'])
                p_novig = po / (po + pu)
                if model == 'same_line':
                    # No spread needed: every ref is at the target's line.
                    params.append(p_novig)
                else:
                    params.append(fit_param(model, sd, r['line'], p_novig))
            consensus = statistics.median(params)

            line = target['line']
            if model == 'same_line':
                p_over, p_under, p_push = consensus, 1 - consensus, 0.0
                line_adjusted = False
            else:
                if model == 'normal' and abs(line - ref_line_median) > max_shift_sd * sd:
                    continue  # too far off market to trust the extrapolation
                if model == 'poisson' and abs(line - ref_line_median) > 1.5:
                    continue
                p_over, p_under, p_push = outcome_probs(model, consensus, sd, line)
                line_adjusted = bool((refs['line'] != line).any())

            for side, odds, p_win, p_lose in (
                    ('Over', target['over_odds'], p_over, p_under),
                    ('Under', target['under_odds'], p_under, p_over)):
                dec = decimal_odds(odds)
                ev = p_win * (dec - 1) - p_lose
                same_line_refs = refs[refs['line'] == line]
                best_other = None
                if not same_line_refs.empty:
                    col = 'over_odds' if side == 'Over' else 'under_odds'
                    best_other = same_line_refs.loc[
                        same_line_refs[col].map(implied_prob).idxmin()]
                out.append({
                    'book': target['book'], 'player_name': player, 'category': category,
                    'team': target['team'], 'side': side, 'line': line, 'odds': int(odds),
                    'fair_prob': round(p_win / (1 - p_push), 4) if p_push < 1 else None,
                    'implied_prob': round(implied_prob(odds), 4),
                    'ev_pct': round(ev * 100, 2),
                    'quarter_kelly_pct': round(_kelly_quarter(p_win, p_push, dec) * 100, 2),
                    'fair_odds': _fair_american(p_win / (1 - p_push)) if p_push < 1 else None,
                    'ref_books': len(refs),
                    'ref_lines': ','.join(sorted({f'{l:g}' for l in refs['line']})),
                    'same_line_refs': len(same_line_refs),
                    'best_other_same_line': (f"{best_other['book']} {int(best_other[col]):+d}"
                                             if best_other is not None else ''),
                    'line_adjusted': line_adjusted,
                    'market': 'two-way', 'model': model,
                })
    return out


def _fair_american(p: float):
    if p <= 0 or p >= 1:
        return None
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def _one_way_rows(df: pd.DataFrame, target_books: set, min_refs: int,
                  one_way_hold: dict) -> list:
    """Yes-only markets (anytime / first / last TD). There's no "No" price
    per player, so each reference book's implied probability is shrunk by
    an assumed margin (`one_way_hold[category]`) and the median taken.
    The margin is the weak point: see --print-td-overround, which measures
    it for first/last TD from each book's full per-game price list."""
    out = []
    one_way = df[df['category'].isin(ONE_WAY_CATEGORIES)].dropna(subset=['over_odds'])
    one_way = one_way[~one_way['book'].isin(PICKEM_BOOKS)]

    for (player, category), group in one_way.groupby(['player_name', 'category']):
        hold = one_way_hold[category]
        for _, target in group[group['book'].isin(target_books)].iterrows():
            refs = group[group['book'] != target['book']]
            if len(refs) < min_refs:
                continue
            fair = statistics.median(implied_prob(o) for o in refs['over_odds']) / (1 + hold)
            dec = decimal_odds(target['over_odds'])
            ev = fair * dec - 1
            best = refs.loc[refs['over_odds'].map(implied_prob).idxmin()]
            out.append({
                'book': target['book'], 'player_name': player, 'category': category,
                'team': target['team'], 'side': 'Yes', 'line': target['line'],
                'odds': int(target['over_odds']),
                'fair_prob': round(fair, 4),
                'implied_prob': round(implied_prob(target['over_odds']), 4),
                'ev_pct': round(ev * 100, 2),
                'quarter_kelly_pct': round(_kelly_quarter(fair, 0.0, dec) * 100, 2),
                'fair_odds': _fair_american(fair),
                'ref_books': len(refs), 'ref_lines': '', 'same_line_refs': len(refs),
                'best_other_same_line': f"{best['book']} {int(best['over_odds']):+d}",
                'line_adjusted': False, 'market': 'one-way', 'model': f'hold={hold:.0%}',
            })
    return out


def print_td_overround(df: pd.DataFrame, props_path: str):
    """First/last TD scorer is mutually exclusive within a game, so summing a
    book's implied probabilities over every listed player estimates its
    margin. It's a LOWER bound on the true overround: the listed players
    plus 'no TD scorer' plus unlisted players (defense/special teams TDs,
    deep backups) would sum to 1 at fair prices."""
    props = pd.read_csv(props_path)
    games = props.dropna(subset=['team', 'opponent'])[['player_name', 'team', 'opponent']]
    games = games.drop_duplicates('player_name')
    games['game'] = games.apply(lambda r: '-'.join(sorted([r['team'], r['opponent']])), axis=1)
    for category in ('first-touchdown-scorer', 'last-touchdown-scorer'):
        sub = df[(df['category'] == category) & ~df['book'].isin(PICKEM_BOOKS)].dropna(subset=['over_odds'])
        sub = sub.merge(games[['player_name', 'game']], on='player_name', how='inner')
        sub['p'] = sub['over_odds'].map(implied_prob)
        sums = sub.groupby(['book', 'game'])['p'].agg(['sum', 'size'])
        sums = sums[sums['size'] >= 10]
        print(f"\n{category}: sum of implied probabilities per game (median across games)")
        print(sums.groupby('book')['sum'].median().round(3).to_string())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--books", default="draftkings,caesars",
                        help="Comma-separated target books to find bets at (default "
                             "draftkings,caesars).")
    parser.add_argument("--min-ev-pct", type=float, default=2.0)
    parser.add_argument("--max-ev-pct", type=float, default=25.0,
                        help="Hide results above this EV%% from the printed table (they're "
                             "almost always stale/bad data); still saved to the CSV.")
    parser.add_argument("--min-refs", type=int, default=3,
                        help="Minimum other sportsbooks quoting the prop (default 3).")
    parser.add_argument("--max-shift-sd", type=float, default=0.5,
                        help="Skip when the target's line is more than this many standard "
                             "deviations from the reference median line (default 0.5).")
    parser.add_argument("--anytime-td-hold", type=float, default=0.06,
                        help="Assumed per-player margin on anytime TD prices (default 6%%, "
                             "an assumption, not measured).")
    parser.add_argument("--first-last-td-hold", type=float, default=0.20,
                        help="Assumed per-player margin on first/last TD prices (default "
                             "20%%; check --print-td-overround).")
    parser.add_argument("--print-td-overround", action="store_true")
    parser.add_argument("--include-started", action="store_true",
                        help="Keep props on games that already kicked off (they aren't "
                             "bettable, and their quotes go stale book by book).")
    parser.add_argument("--one-way", action="store_true",
                        help="Also score TD scorer props with an assumed margin (unreliable "
                             "for longshots; see module docstring). find_td_bets.py is better.")
    parser.add_argument("--props", default="data/scoresandodds_props_all.csv.gz",
                        help="Used by --print-td-overround for each player's game.")
    parser.add_argument("--top", type=int, default=40)
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    df['book'] = df['book'].str.lower()
    if not args.include_started:
        df = drop_started(df)
    if 'available' in df.columns:
        unavailable = df['available'].astype(str).str.lower() == 'false'
        if unavailable.any():
            print(f"Dropping {unavailable.sum():,} quotes the API marks unavailable")
        df = df[~unavailable]
    else:
        print("WARNING: no 'available' column (older scrape) - suspended/stale "
              "quotes can't be filtered and may show up as fake edges")
    target_books = {b.strip().lower() for b in args.books.split(',')}
    print(f"Loaded {len(df):,} book-level rows; target books: {sorted(target_books)}")

    if args.print_td_overround:
        print_td_overround(df, args.props)

    hold = {'touchdowns': args.anytime_td_hold,
            'first-touchdown-scorer': args.first_last_td_hold,
            'last-touchdown-scorer': args.first_last_td_hold}
    rows = _two_way_rows(df, target_books, args.min_refs, args.max_shift_sd)
    if args.one_way:
        rows += _one_way_rows(df, target_books, args.min_refs, hold)
    result = pd.DataFrame(rows)
    if result.empty:
        print("No quotes at the target books with enough reference books.")
        return

    result = result[result['ev_pct'] >= args.min_ev_pct].sort_values('ev_pct', ascending=False)
    news = NewsContext()
    reasons = []
    for r in result.itertuples():
        why = []
        spread_sd = ref_spread_sd(r.category, r.ref_lines)
        if spread_sd >= REF_SPREAD_SD:
            why.append(f"books disagree ({r.ref_lines}, {spread_sd:.1f} sd)")
        why += news.news_for(r.player_name, r.team)
        reasons.append('; '.join(why))
    result['check_reason'] = reasons
    result['confidence'] = ['check' if w else 'clean' for w in reasons]
    base = args.input.replace('.csv.gz', '').replace('.csv', '')
    out_path = base + '_ev_bets.csv'
    result.to_csv(out_path, index=False)

    cols = ['book', 'player_name', 'category', 'side', 'line', 'odds', 'fair_odds',
            'ev_pct', 'quarter_kelly_pct', 'ref_books', 'ref_lines', 'best_other_same_line']
    shown = result[result['ev_pct'] <= args.max_ev_pct]
    sections = [('two-way', 'clean', 'Over/Under props'),
                ('two-way', 'check', 'Over/Under props, check first (market mid-move or injury news)')]
    if args.one_way:
        sections.append(('one-way', None, 'TD scorer props (margin ASSUMED, unreliable for longshots)'))
    for market, conf, label in sections:
        part = shown[shown['market'] == market]
        if conf:
            part = part[part['confidence'] == conf]
        print(f"\n=== {label}: {len(part)} bets >= {args.min_ev_pct}% EV ===")
        if not part.empty:
            extra = ['check_reason'] if conf == 'check' else []
            print(part[cols + extra].head(args.top).to_string(index=False))
    hidden = len(result) - len(shown)
    if hidden:
        print(f"\n{hidden} results above {args.max_ev_pct}% EV hidden (likely stale/bad data).")
    print(f"\nFull results saved -> {out_path}")


if __name__ == "__main__":
    main()
