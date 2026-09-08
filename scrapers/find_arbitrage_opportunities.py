"""
scrapers/find_arbitrage_opportunities.py
--------------------------------------------------
Finds SAME-LINE arbitrage across books: when the best Over price at
one book and the best Under price at another combine to less than
100% implied probability, betting both sides locks in a guaranteed
profit regardless of the actual result — no variance at all, unlike
middling (find_middling_opportunities.py), which specifically needs
DIFFERENT lines and only pays off big if the result lands between
them (small loss otherwise).

To execute an opportunity: bet OVER at 'over_book' using the
'stake_over' amount, and UNDER at 'under_book' using 'stake_under'.
Since it's the same line, exactly one of those two bets must win, and
the stake sizing is calculated so either outcome pays the same
guaranteed profit.

Deliberately scoped to same-line only. A different-line combination
(Over at a lower line + Under at a higher line) can also sometimes
guarantee at-worst-one-side-wins, but that's the same territory
middling already covers from a different angle, and mixing the two
here would blur what's a genuinely risk-free arb versus what's really
a favorable middle — kept separate so "arbitrage" in this script's
output actually means guaranteed profit, full stop.

Standard American-odds arbitrage math, verified against a textbook
example (+110/+105 -> guaranteed ~3.7% profit) before writing this.

Real caveats, not glossed over:
  - Whole-number lines can push (neither side settles as a loss) —
    this is a 3-outcome case the simple 2-way math here doesn't model.
    Half-point lines (the vast majority of these props) can't push.
  - Books can and do limit or reduce max bet size on players who
    consistently place +EV/arbitrage action — this finds the
    opportunity, not a guarantee any book will let you bet it at
    size indefinitely.
  - No line-movement risk modeled — this assumes both bets can be
    placed near-simultaneously, before either book moves.

Usage:
    python scrapers/find_arbitrage_opportunities.py \\
        --input data/scoresandodds_market_comparison.csv.gz \\
        --min-profit-pct 1.0 --total-stake 100 \\
        --exclude-books bet365,underdog,prizepicks
"""

import argparse
import pandas as pd


def american_to_implied_prob(odds: float) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def american_to_decimal(odds: float) -> float:
    if odds > 0:
        return 1 + odds / 100
    else:
        return 1 + 100 / abs(odds)


def find_arbitrage(df: pd.DataFrame, min_profit_pct: float, total_stake: float,
                   exclude_books: set = None) -> pd.DataFrame:
    if exclude_books:
        before = len(df)
        df = df[~df['book'].str.lower().isin(exclude_books)]
        print(f"Excluding books {sorted(exclude_books)}: {before - len(df)} rows removed, "
              f"{len(df)} remain")

    opportunities = []

    # Same LINE required (not just same player/category) - grouping on
    # all three is what keeps this genuinely risk-free rather than a
    # middle in disguise.
    for (player_name, category, line), group in df.groupby(['player_name', 'category', 'line']):
        if len(group) < 2:
            continue

        valid_over = group.dropna(subset=['over_odds'])
        valid_under = group.dropna(subset=['under_odds'])
        if valid_over.empty or valid_under.empty:
            continue

        # Best price for each side - lowest implied probability, i.e.
        # the most favorable odds you could actually get.
        over_probs = valid_over['over_odds'].apply(american_to_implied_prob)
        under_probs = valid_under['under_odds'].apply(american_to_implied_prob)

        best_over_idx = over_probs.idxmin()
        best_under_idx = under_probs.idxmin()
        best_over_row = valid_over.loc[best_over_idx]
        best_under_row = valid_under.loc[best_under_idx]

        if best_over_row['book'] == best_under_row['book']:
            # Same book offering both sides isn't a cross-book arb -
            # that's just that book's own vig, always >= 100% by
            # construction (how they make money).
            continue

        p_over = over_probs[best_over_idx]
        p_under = under_probs[best_under_idx]
        combined = p_over + p_under

        if combined >= 1.0:
            continue

        profit_pct = (1 - combined) / combined * 100
        if profit_pct < min_profit_pct:
            continue

        d_over = american_to_decimal(best_over_row['over_odds'])
        d_under = american_to_decimal(best_under_row['under_odds'])
        stake_over = round(total_stake * (1 / d_over) / combined, 2)
        stake_under = round(total_stake * (1 / d_under) / combined, 2)
        guaranteed_profit = round(stake_over * d_over - total_stake, 2)

        opportunities.append({
            'player_name': player_name, 'category': category, 'team': group['team'].iloc[0],
            'line': line,
            'over_book': best_over_row['book'], 'over_odds': best_over_row['over_odds'],
            'under_book': best_under_row['book'], 'under_odds': best_under_row['under_odds'],
            'profit_pct': round(profit_pct, 2),
            'stake_over': stake_over, 'stake_under': stake_under,
            'guaranteed_profit': guaranteed_profit,
        })

    result = pd.DataFrame(opportunities)
    if not result.empty:
        result = result.sort_values('profit_pct', ascending=False)
    return result


def main(input_path: str, min_profit_pct: float, top_n: int, total_stake: float,
        exclude_books: set = None):
    df = pd.read_csv(input_path)
    print(f"Loaded {len(df)} book-level rows across "
          f"{df.groupby(['player_name', 'category']).ngroups} (player, category) pairs")

    opportunities = find_arbitrage(df, min_profit_pct, total_stake, exclude_books)

    if opportunities.empty:
        print(f"\nNo same-line arbitrage found with profit >= {min_profit_pct}%.")
        print("This is expected most of the time — same-line, cross-book price gaps big "
              "enough to clear the vig entirely are rare. Consider also checking "
              "find_middling_opportunities.py for different-line opportunities.")
        return

    print(f"\n{len(opportunities)} arbitrage opportunities found with profit >= {min_profit_pct}%")
    print(f"\nTop {min(top_n, len(opportunities))} by guaranteed profit % "
          f"(sized for a ${total_stake:.0f} total stake per opportunity):\n")

    display_cols = ['player_name', 'category', 'line', 'over_book', 'over_odds',
                    'under_book', 'under_odds', 'profit_pct', 'stake_over', 'stake_under',
                    'guaranteed_profit']
    print(opportunities[display_cols].head(top_n).to_string(index=False))

    base = input_path.replace('.csv.gz', '').replace('.csv', '')
    suffix = '_arbitrage_opportunities_filtered.csv' if exclude_books else '_arbitrage_opportunities.csv'
    out_path = base + suffix
    opportunities.to_csv(out_path, index=False)
    print(f"\nFull results saved -> {out_path}")

    print(f"\nUnlike middling, this profit is guaranteed regardless of the actual result — "
          f"both bets are on the SAME line, so exactly one must win (barring a push on a "
          f"whole-number line, which this doesn't model separately). Bet OVER at "
          f"'over_book' (stake_over amount) and UNDER at 'under_book' (stake_under "
          f"amount) — real risk here is execution: books limiting bet size or account "
          f"access for players who consistently place this kind of action, and line "
          f"movement between placing the two bets.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_market_comparison.csv.gz")
    parser.add_argument("--min-profit-pct", type=float, default=1.0,
                        help="Minimum guaranteed profit percentage to count as an "
                             "opportunity worth listing (default 1.0%%).")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--total-stake", type=float, default=100.0,
                        help="Total $ across both legs, used to compute stake sizing "
                             "(default $100).")
    parser.add_argument("--exclude-books", default=None,
                        help="Comma-separated book slugs to exclude entirely, e.g. "
                             "'bet365,underdog,prizepicks' — for books unavailable in "
                             "your state.")
    args = parser.parse_args()

    exclude_set = None
    if args.exclude_books:
        exclude_set = {b.strip().lower() for b in args.exclude_books.split(',')}

    main(args.input, args.min_profit_pct, args.top, args.total_stake, exclude_set)
