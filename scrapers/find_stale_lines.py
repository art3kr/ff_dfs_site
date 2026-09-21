"""
scrapers/find_stale_lines.py
--------------------------------------------------
Finds DraftKings / Caesars quotes that haven't caught up with news the
rest of the market already priced in, by comparing the latest scrape to an
earlier snapshot from bet_tracker.py's archive.

The case it's built from (Week 2, 2026-09-21): Puka Nacua was ruled out
for MNF. Between Wednesday and Monday night the other books shortened
Davante Adams' anytime TD +115 -> -120, raised his receptions line 3.5 ->
5.5 and his receiving yards 44.5 -> 63.5, and shortened Konata Mumpfield
+1600 -> +500. Most books then pulled Nacua's own props, while DraftKings
still listed him at +190. A book that is late to that kind of move is
where a real edge lives, and find_ev_bets.py can't see it: it compares
books to each other at one moment, so a half-moved market looks like
disagreement, not like news.

For every prop and book, both snapshots are turned into one comparable
number:
  - over/under props: the distribution center the quote implies (the same
    de-vig + normal/Poisson fit as find_ev_bets.py), so a line move and a
    price move at the same line both count;
  - anytime TD: the implied probability.
A target book is flagged when the other books' median move is at least
MIN_MOVE_SD standard deviations (MIN_TD_PP points for TDs) and the target
moved less than LAG_RATIO of that. Flags:
  - 'lagging': the side the market moved toward is the value side at the
    lagging book, priced with the other books' CURRENT consensus
    (find_ev_bets' model), EV included;
  - 'lagging, avoid': the market moved AGAINST an anytime TD (the player's
    role shrank) and the book is still at the old, shorter price; there's no
    "No" side to bet, so it's a warning, not a bet;
  - 'pulled elsewhere': at least PULLED_SHARE of the other books (and at
    least PULLED_MIN_BOOKS of them) pulled this prop (available: false)
    while the target still offers it. That's usually injury or status news.
    Bets on a player who doesn't play are voided, so it's a signal to go
    check the news, not a bet by itself. Printed one line per player, with
    players on a team-news team first: the Sunday 12:43 PM run had Nico
    Collins' props pulled at every other book while all his Houston
    teammates' TD prices rose, and Michael Pittman the same in Pittsburgh.
    Books also pull props routinely right before kickoff, so a lone pull on
    a team with no other movement is weak evidence.

Also prints team news clusters: teams where several props moved at once
(the whole Rams passing game after Nacua was ruled out), with the biggest
movers, since those teams are where lagging books are most likely.

Caveats, not glossed over:
  - "The market moved" isn't the same as "the market is right". Most of
    the time it is, on news; sometimes it's one big bettor.
  - The baseline snapshot must be from the same week. Default: the
    earliest snapshot in this week's archive folder.
  - A book can lag on purpose (it has a limit on the prop, or took a
    position). That's still a price you can bet, until it moves.

Usage:
    python scrapers/find_stale_lines.py
    python scrapers/find_stale_lines.py --baseline data/odds_archive/2026_wk02/market_comparison_20260916_2336.csv.gz
"""

import argparse
import glob
import os
import statistics
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import find_ev_bets as fev
from bet_tracker import ARCHIVE_DIR, current_week, drop_started

MIN_MOVE_SD = 0.5
MIN_TD_PP = 4.0
LAG_RATIO = 0.34
PULLED_SHARE = 0.67
PULLED_MIN_BOOKS = 3
TEAM_NEWS_MIN_PROPS = 3


def _sd(category: str, line: float):
    model, spread = fev.CATEGORY_MODELS.get(category, ('same_line', None))
    if model == 'normal' and line >= fev.MIN_NORMAL_LINE:
        return 'normal', spread[0] * line ** spread[1]
    if model == 'poisson':
        return 'poisson', max(line, 0.5) ** 0.5
    return None, None


def _center(model: str, sd: float, row) -> float:
    """Distribution center implied by one book's quote (line + both prices)."""
    po, pu = fev.implied_prob(row['over_odds']), fev.implied_prob(row['under_odds'])
    return fev.fit_param(model, sd if model == 'normal' else None, row['line'], po / (po + pu))


def _load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df['book'] = df['book'].str.lower()
    df = df[~df['book'].isin(fev.PICKEM_BOOKS)]
    if 'available' not in df.columns:
        df['available'] = True
    df['available'] = df['available'].astype(str).str.lower() != 'false'
    return df


def default_baseline(current_path: str) -> str:
    year, week = current_week()
    snaps = sorted(glob.glob(os.path.join(ARCHIVE_DIR, f'{year}_wk{week:02d}', 'market_comparison_*.csv.gz')))
    if not snaps:
        raise SystemExit(f"No archived snapshots for {year} week {week}; pass --baseline.")
    return snaps[0]


def analyze(base: pd.DataFrame, cur: pd.DataFrame, target_books: set) -> tuple:
    flags, moves = [], []
    base = base[base['available']]
    key = ['player_name', 'category']
    cur_groups = dict(tuple(cur.groupby(key)))
    for (player, category), b in base.groupby(key):
        c = cur_groups.get((player, category))
        if c is None:
            continue
        team = c['team'].dropna().iloc[0] if c['team'].notna().any() else None
        live = c[c['available']]

        # --- pulled elsewhere: others took it down, a target book still offers it.
        others_all = c[~c['book'].isin(target_books)]
        if len(others_all) >= PULLED_MIN_BOOKS:
            pulled = (~others_all['available']).mean()
            if pulled >= PULLED_SHARE:
                for t in live[live['book'].isin(target_books)].itertuples():
                    flags.append({'flag': 'pulled elsewhere', 'book': t.book, 'player_name': player,
                                  'team': team, 'category': category, 'side': 'Over',
                                  'line': t.line, 'odds': t.over_odds, 'ev_pct': None,
                                  'note': f"{pulled:.0%} of other books pulled this prop; check news"})

        both = b.merge(live, on='book', suffixes=('_b', '_c'))
        if category == 'touchdowns':
            both = both.dropna(subset=['over_odds_b', 'over_odds_c'])
            if both.empty:
                continue
            both['move'] = 100 * (both['over_odds_c'].map(fev.implied_prob)
                                  - both['over_odds_b'].map(fev.implied_prob))
            unit, threshold = 'pp', MIN_TD_PP
        else:
            both = both.dropna(subset=['line_b', 'line_c', 'over_odds_b', 'under_odds_b',
                                       'over_odds_c', 'under_odds_c'])
            if both.empty or category in fev.ONE_WAY_CATEGORIES:
                continue
            model, sd = _sd(category, float(both['line_b'].median()))
            if model is None:
                continue
            moves_sd = []
            for r in both.itertuples():
                cb = _center(model, sd, {'line': r.line_b, 'over_odds': r.over_odds_b, 'under_odds': r.under_odds_b})
                cc = _center(model, sd, {'line': r.line_c, 'over_odds': r.over_odds_c, 'under_odds': r.under_odds_c})
                moves_sd.append((cc - cb) / sd)
            both['move'] = moves_sd
            unit, threshold = 'sd', MIN_MOVE_SD

        others = both[~both['book'].isin(target_books)]
        if len(others) < 2:
            continue
        consensus = float(others['move'].median())
        if abs(consensus) >= threshold:
            moves.append({'team': team, 'player_name': player, 'category': category,
                          'move': round(consensus, 2), 'unit': unit})
        if abs(consensus) < threshold:
            continue
        for t in both[both['book'].isin(target_books)].itertuples():
            if t.move * (1 if consensus > 0 else -1) >= LAG_RATIO * abs(consensus):
                continue  # it moved with the market
            row = {'book': t.book, 'player_name': player, 'team': team, 'category': category,
                   'market_move': round(consensus, 2), 'book_move': round(t.move, 2), 'unit': unit}
            if category == 'touchdowns':
                row.update(line=0.5, odds=int(t.over_odds_c))
                if consensus > 0:
                    fair = statistics.median(others['over_odds_c'].map(fev.implied_prob))
                    row.update(flag='lagging', side='Over',
                               ev_pct=round((fair * fev.decimal_odds(t.over_odds_c) - 1) * 100, 1),
                               note=f"other books +{consensus:.1f} pts, this book {t.move:+.1f}; "
                                    f"EV vs other books' vig-included price")
                else:
                    row.update(flag='lagging, avoid', side='Over', ev_pct=None,
                               note=f"other books {consensus:.1f} pts (role shrank?), this book "
                                    f"{t.move:+.1f}; still priced for the old role")
            else:
                side = 'Over' if consensus > 0 else 'Under'
                priced = pd.DataFrame(fev._two_way_rows(c[c['available']], {t.book}, 2, 10.0))
                ev = None
                if not priced.empty:
                    m = priced[priced['side'] == side]
                    ev = float(m['ev_pct'].iloc[0]) if not m.empty else None
                row.update(flag='lagging', side=side, line=t.line_c,
                           odds=int(t.over_odds_c if side == 'Over' else t.under_odds_c), ev_pct=ev,
                           note=f"other books moved {consensus:+.2f} sd, this book {t.move:+.2f} sd "
                                f"(line {t.line_b:g} -> {t.line_c:g})")
            flags.append(row)
    return pd.DataFrame(flags), pd.DataFrame(moves)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--current', default='data/scoresandodds_market_comparison.csv.gz')
    parser.add_argument('--baseline', default=None,
                        help="Earlier snapshot to compare against (default: this week's first).")
    parser.add_argument('--books', default='draftkings,caesars')
    parser.add_argument('--as-of', default=None,
                        help="UTC time 'YYYY-MM-DDTHH:MM' to treat as now for skipping started "
                             "games (for re-running against an archived snapshot).")
    parser.add_argument('--include-started', action='store_true')
    parser.add_argument('--output', default=None, help='Default: <current>_stale_lines.csv')
    args = parser.parse_args()

    baseline = args.baseline or default_baseline(args.current)
    target_books = {b.strip().lower() for b in args.books.split(',')}
    base, cur = _load(baseline), _load(args.current)
    if not args.include_started:
        now = (datetime.strptime(args.as_of, '%Y-%m-%dT%H:%M').replace(tzinfo=timezone.utc)
               if args.as_of else None)
        cur = drop_started(cur, now=now)
    print(f"Baseline: {os.path.basename(baseline)}   Current: {os.path.basename(args.current)}")

    flags, moves = analyze(base, cur, target_books)

    if not moves.empty:
        news = moves.groupby('team').size().sort_values(ascending=False)
        news = news[news >= TEAM_NEWS_MIN_PROPS]
        print(f"\n=== Teams with news-sized moves ({TEAM_NEWS_MIN_PROPS}+ props moved) ===")
        for team, n in news.items():
            top = moves[moves['team'] == team].assign(a=lambda d: d['move'].abs()).sort_values('a', ascending=False).head(4)
            desc = ', '.join(f"{r.player_name} {r.category} {r.move:+g}{r.unit}" for r in top.itertuples())
            print(f"  {team}: {n} props  ({desc})")

    out_path = args.output or (args.current.replace('.csv.gz', '').replace('.csv', '') + '_stale_lines.csv')
    if flags.empty:
        print("\nNo DraftKings/Caesars quotes lagging a market move.")
        pd.DataFrame(columns=['book', 'player_name', 'team', 'category', 'side', 'line', 'odds',
                              'ev_pct', 'flag', 'note']).to_csv(out_path, index=False)
        return
    flags['confidence'] = 'stale'
    flags['check_reason'] = flags['note']
    news_teams = set(news.index) if not moves.empty else set()
    flags['team_news'] = flags['team'].isin(news_teams)
    order = {'lagging': 0, 'pulled elsewhere': 1, 'lagging, avoid': 2}
    flags = flags.sort_values(['flag', 'ev_pct'], key=lambda s: s.map(order) if s.name == 'flag' else -s.fillna(-999))
    flags.to_csv(out_path, index=False)
    cols = ['book', 'player_name', 'team', 'category', 'side', 'line', 'odds', 'ev_pct', 'note']
    for flag, label in (('lagging', 'Lagging a market move (bet the side the market moved toward)'),
                        ('lagging, avoid', 'Still priced for an old role (avoid)')):
        part = flags[flags['flag'] == flag]
        print(f"\n=== {label}: {len(part)} ===")
        if not part.empty:
            print(part[cols].to_string(index=False))
    pulled = flags[flags['flag'] == 'pulled elsewhere']
    if not pulled.empty:
        by_player = pulled.groupby(['player_name', 'team']).agg(
            books=('book', lambda b: ', '.join(sorted(set(b)))),
            markets=('category', 'nunique'),
            team_news=('team_news', 'first')).reset_index()
        by_player = by_player.sort_values(['team_news', 'markets'], ascending=False)
        print(f"\n=== Pulled at other books, still up at yours (check news): {len(by_player)} players ===")
        for r in by_player.itertuples():
            tag = '  <- team news' if r.team_news else ''
            print(f"  {r.player_name} ({r.team}): {r.markets} market(s) still up at {r.books}{tag}")
    print(f"\nSaved -> {out_path}")


if __name__ == '__main__':
    main()
