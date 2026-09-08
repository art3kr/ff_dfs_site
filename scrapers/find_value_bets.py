"""
scrapers/find_value_bets.py
--------------------------------------------------
Flags props where scoresandodds' own projection (site_projection)
disagrees significantly with the market line — a signal that their
model sees something the betting market's consensus price doesn't
(or vice versa), not a guaranteed opportunity like arbitrage.

Confirmed real (via scrape_scoresandodds_props.py): site_projection
comes from the page's own data-proj attribute, and is on a DIFFERENT
SCALE for moneyline-shaped categories (0-1 probability, e.g. touchdowns)
than for line-based categories (same units as the line itself, e.g.
yards or receptions). Comparing site_projection to line directly would
be meaningless for the moneyline-shaped ones — this is naturally
avoided rather than specially handled, since those categories also
have no `line` value at all (both over_line/under_line are null), so
filtering on "line is present" already excludes them.

This is a directional signal, not a certainty: it just means
scoresandodds' own number and the market's number disagree by more
than the threshold. It says nothing about which one is actually right.

Usage:
    python scrapers/find_value_bets.py \\
        --input data/scoresandodds_market_comparison.csv.gz \\
        --min-diff-pct 8.0
"""

import argparse
import pandas as pd


def find_value_bets(df: pd.DataFrame, min_diff_pct: float,
                    exclude_books: set = None) -> pd.DataFrame:
    if exclude_books:
        before = len(df)
        df = df[~df['book'].str.lower().isin(exclude_books)]
        print(f"Excluding books {sorted(exclude_books)}: {before - len(df)} rows removed, "
              f"{len(df)} remain")

    # Line-based categories only - see module docstring for why
    # moneyline-shaped ones (no `line` at all) are naturally excluded
    # here rather than specially handled.
    df = df.dropna(subset=['line', 'site_projection'])
    df = df[df['line'] != 0]

    df = df.copy()
    df['diff'] = df['site_projection'] - df['line']
    df['diff_pct'] = (df['diff'] / df['line'] * 100).round(2)
    df['direction'] = df['diff'].apply(lambda d: 'Over' if d > 0 else 'Under')

    result = df[df['diff_pct'].abs() >= min_diff_pct].copy()
    result = result.sort_values('diff_pct', key=lambda s: s.abs(), ascending=False)
    return result[['player_name', 'category', 'team', 'book', 'line', 'site_projection',
                   'diff', 'diff_pct', 'direction']]


def main(input_path: str, min_diff_pct: float, top_n: int, exclude_books: set = None):
    df = pd.read_csv(input_path)
    print(f"Loaded {len(df)} book-level rows")

    result = find_value_bets(df, min_diff_pct, exclude_books)

    if result.empty:
        print(f"\nNo props found where site_projection differs from the line by "
              f">= {min_diff_pct}%.")
        return

    print(f"\n{len(result)} props found where site_projection differs from the line "
          f"by >= {min_diff_pct}%")
    print(f"\nTop {min(top_n, len(result))} by absolute difference:\n")
    print(result.head(top_n).to_string(index=False))

    base = input_path.replace('.csv.gz', '').replace('.csv', '')
    out_path = base + '_value_bets.csv'
    result.to_csv(out_path, index=False)
    print(f"\nFull results saved -> {out_path}")

    print(f"\nThis flags a DISAGREEMENT between scoresandodds' own projection and the "
          f"market line — it doesn't tell you which one is right. A large gap could mean "
          f"their model is genuinely finding an edge the market missed, or it could mean "
          f"their projection is stale, uses different injury/usage assumptions, or is "
          f"just wrong. Worth checking the specific player's situation before betting on "
          f"the disagreement alone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_market_comparison.csv.gz")
    parser.add_argument("--min-diff-pct", type=float, default=8.0,
                        help="Minimum absolute %% difference between site_projection and "
                             "the line to flag (default 8%%).")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--exclude-books", default=None,
                        help="Comma-separated book slugs to exclude entirely, e.g. "
                             "'bet365,underdog,prizepicks'.")
    args = parser.parse_args()

    exclude_set = None
    if args.exclude_books:
        exclude_set = {b.strip().lower() for b in args.exclude_books.split(',')}

    main(args.input, args.min_diff_pct, args.top, exclude_set)
