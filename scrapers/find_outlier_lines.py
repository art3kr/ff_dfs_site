"""
scrapers/find_outlier_lines.py
--------------------------------------------------
Flags a book's line when it's a significant outlier against the
consensus of every OTHER book offering the same prop — broader than
middling (find_middling_opportunities.py) since it doesn't need a
matched min/max pair, just flags anything that stands out, and
broader than arbitrage (find_arbitrage_opportunities.py) since it
doesn't require a guaranteed payout structure, just a mispriced-
looking line worth a second look.

Key detail: the book being checked is EXCLUDED from its own
comparison baseline. Including it would let a genuine outlier pull the
consensus mean toward itself, understating how far off it actually is
— e.g. one book at 30 yards against four books clustered at 20 would
show as a much smaller gap if that 30 were folded into its own
average.

This is a "worth a second look" signal, not a certainty — an outlier
line can mean a book is slow to update, has different information, or
made an error, and there's no way to tell which from this data alone.

Usage:
    python scrapers/find_outlier_lines.py \\
        --input data/scoresandodds_market_comparison.csv.gz \\
        --min-diff-pct 10.0 --min-books 3
"""

import argparse
import pandas as pd


def find_outliers(df: pd.DataFrame, min_diff_pct: float, min_books: int,
                  exclude_books: set = None) -> pd.DataFrame:
    if exclude_books:
        before = len(df)
        df = df[~df['book'].str.lower().isin(exclude_books)]
        print(f"Excluding books {sorted(exclude_books)}: {before - len(df)} rows removed, "
              f"{len(df)} remain")

    df = df.dropna(subset=['line'])
    df = df[df['line'] != 0]

    results = []
    for (player_name, category), group in df.groupby(['player_name', 'category']):
        if len(group) < min_books:
            continue

        for idx, row in group.iterrows():
            # Consensus mean EXCLUDING this book's own line - the key
            # detail that keeps this methodologically sound (see
            # module docstring).
            other_lines = group.loc[group.index != idx, 'line']
            if other_lines.empty:
                continue
            consensus_mean = other_lines.mean()
            if consensus_mean == 0:
                continue

            diff = row['line'] - consensus_mean
            diff_pct = diff / consensus_mean * 100

            if abs(diff_pct) < min_diff_pct:
                continue

            results.append({
                'player_name': player_name, 'category': category, 'team': row['team'],
                'book': row['book'], 'line': row['line'],
                'consensus_mean': round(consensus_mean, 2),
                'other_books_count': len(other_lines),
                'diff': round(diff, 2), 'diff_pct': round(diff_pct, 2),
                'direction': 'Higher than consensus' if diff > 0 else 'Lower than consensus',
            })

    result = pd.DataFrame(results)
    if not result.empty:
        result = result.sort_values('diff_pct', key=lambda s: s.abs(), ascending=False)
    return result


def main(input_path: str, min_diff_pct: float, min_books: int, top_n: int,
        exclude_books: set = None):
    df = pd.read_csv(input_path)
    print(f"Loaded {len(df)} book-level rows across "
          f"{df.groupby(['player_name', 'category']).ngroups} (player, category) pairs")

    result = find_outliers(df, min_diff_pct, min_books, exclude_books)

    if result.empty:
        print(f"\nNo outlier lines found (>= {min_diff_pct}% off consensus, requiring "
              f">= {min_books} books quoting the same prop).")
        return

    print(f"\n{len(result)} outlier lines found (>= {min_diff_pct}% off the consensus of "
          f"the other books)")
    print(f"\nTop {min(top_n, len(result))} by absolute deviation:\n")
    print(result.head(top_n).to_string(index=False))

    base = input_path.replace('.csv.gz', '').replace('.csv', '')
    out_path = base + '_outlier_lines.csv'
    result.to_csv(out_path, index=False)
    print(f"\nFull results saved -> {out_path}")

    print(f"\nAn outlier line isn't automatically a good bet — it can mean the book is "
          f"slow to update, has different info, or made a pricing error. Worth checking "
          f"whether the outlier book still lets you bet at that price before assuming "
          f"it's exploitable.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_market_comparison.csv.gz")
    parser.add_argument("--min-diff-pct", type=float, default=10.0,
                        help="Minimum absolute %% a book's line must differ from the "
                             "consensus of every OTHER book to flag (default 10%%).")
    parser.add_argument("--min-books", type=int, default=3,
                        help="Minimum number of books quoting the same prop required "
                             "before checking for outliers (default 3 — need enough "
                             "other books for 'consensus' to mean something).")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--exclude-books", default=None,
                        help="Comma-separated book slugs to exclude entirely, e.g. "
                             "'bet365,underdog,prizepicks'.")
    args = parser.parse_args()

    exclude_set = None
    if args.exclude_books:
        exclude_set = {b.strip().lower() for b in args.exclude_books.split(',')}

    main(args.input, args.min_diff_pct, args.min_books, args.top, exclude_set)
