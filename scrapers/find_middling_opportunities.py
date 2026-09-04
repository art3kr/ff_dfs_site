"""
scrapers/find_middling_opportunities.py
---------------------------------------------
Finds "middling" opportunities from scrape_scoresandodds_market_comparison.py's
output — different from pure arbitrage. You bet the OVER at whichever
book has the LOWEST line for a player/category, and the UNDER at
whichever book has the HIGHEST line. Most of the time, one leg wins
and one loses (a small net loss from vig on both sides). But if the
actual result lands strictly between the two lines, BOTH legs win — a
large payout. The wider the gap between the lowest and highest line
across books, the bigger that "middle" window, and the better the
opportunity (all else equal).

This does NOT model the actual probability of landing in the middle
(that would need a real distribution/variance estimate per stat,
which we don't have solid grounds for) — it ranks by middle width
and shows the real odds on both legs, so you can judge risk/reward
yourself rather than trusting a made-up probability model.

Usage:
    python scrapers/find_middling_opportunities.py \\
        --input data/scoresandodds_market_comparison.csv.gz \\
        --min-width 2 \\
        --top 20
"""

import argparse
import pandas as pd


def find_middles(df: pd.DataFrame, min_width: float) -> pd.DataFrame:
    opportunities = []

    for (player, category), group in df.groupby(['player_name', 'category']):
        if len(group) < 2:
            continue   # need at least 2 books to have a middle at all

        group = group.dropna(subset=['line'])
        if len(group) < 2:
            continue

        low_row = group.loc[group['line'].idxmin()]
        high_row = group.loc[group['line'].idxmax()]

        # Same book can't offer both ends of a middle (degenerate case,
        # also protects against a group with just one distinct line)
        if low_row['book'] == high_row['book']:
            continue

        middle_width = high_row['line'] - low_row['line']
        if middle_width < min_width:
            continue

        opportunities.append({
            'player_name': player,
            'category': category,
            'team': low_row.get('team'),
            'low_book': low_row['book'], 'low_line': low_row['line'], 'low_over_odds': low_row['over_odds'],
            'high_book': high_row['book'], 'high_line': high_row['line'], 'high_under_odds': high_row['under_odds'],
            'middle_width': round(middle_width, 2),
            'books_compared': len(group),
        })

    result = pd.DataFrame(opportunities)
    if not result.empty:
        result = result.sort_values('middle_width', ascending=False)
    return result


def main(input_path: str, min_width: float, top_n: int):
    df = pd.read_csv(input_path)
    print(f"Loaded {len(df)} book-level rows across "
          f"{df.groupby(['player_name', 'category']).ngroups} (player, category) pairs")

    opportunities = find_middles(df, min_width)

    if opportunities.empty:
        print(f"\nNo middling opportunities found with width >= {min_width}.")
        return

    print(f"\n{len(opportunities)} opportunities found with width >= {min_width}")
    print(f"\nTop {min(top_n, len(opportunities))} by middle width:\n")

    display_cols = ['player_name', 'category', 'low_book', 'low_line', 'low_over_odds',
                    'high_book', 'high_line', 'high_under_odds', 'middle_width']
    print(opportunities[display_cols].head(top_n).to_string(index=False))

    out_path = input_path.replace('.csv.gz', '').replace('.csv', '') + '_middling_opportunities.csv'
    opportunities.to_csv(out_path, index=False)
    print(f"\nFull results saved -> {out_path}")

    print(f"\nReminder: this ranks by middle WIDTH only, not a modeled probability "
          f"of landing in it — check the odds on both legs before betting, and "
          f"remember middling is a variance play (usually a small net loss, "
          f"occasionally a big win), not guaranteed profit like pure arbitrage.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_market_comparison.csv.gz")
    parser.add_argument("--min-width", type=float, default=1.0,
                        help="Minimum gap between the lowest and highest line to count "
                             "as an opportunity (in the stat's own units, e.g. yards).")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()
    main(args.input, args.min_width, args.top)
