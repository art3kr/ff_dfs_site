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


def _american_to_decimal(odds: float) -> float:
    """Decimal odds = total return per $1 staked, including the stake back."""
    if odds < 0:
        return 1 + 100 / abs(odds)
    else:
        return 1 + odds / 100


def _balanced_stakes(over_odds: float, under_odds: float, total_stake: float) -> dict:
    """
    Sizes the two legs so that losing EITHER side alone produces the
    SAME net result, regardless of which one misses — the standard
    way to size a middle, rather than flat equal stakes on both sides
    (which only happens to be balanced when the two legs' odds are
    identical). Bet MORE on whichever leg has the worse (lower)
    decimal odds.
    """
    dec_over = _american_to_decimal(over_odds)
    dec_under = _american_to_decimal(under_odds)

    ratio = dec_under / dec_over   # stake_over : stake_under
    stake_under = total_stake / (1 + ratio)
    stake_over = total_stake - stake_under

    loss_if_one_side_wins = stake_over * (dec_over - 1) - stake_under
    profit_if_middle_hits = stake_over * (dec_over - 1) + stake_under * (dec_under - 1)

    return {
        'stake_over': round(stake_over, 2),
        'stake_under': round(stake_under, 2),
        'loss_if_miss': round(loss_if_one_side_wins, 2),
        'profit_if_middle_hits': round(profit_if_middle_hits, 2),
    }


def find_middles(df: pd.DataFrame, min_width: float, total_stake: float,
                 exclude_books: set = None, exclude_book_categories: set = None) -> pd.DataFrame:
    if exclude_books:
        before = len(df)
        df = df[~df['book'].str.lower().isin(exclude_books)]
        print(f"Excluding books {sorted(exclude_books)}: {before - len(df)} rows removed, "
              f"{len(df)} remain")

    if exclude_book_categories:
        # Excludes a specific (book, category) pair only — e.g. Caesars
        # not offering rushing-and-receiving-yards lines shouldn't
        # remove Caesars from every OTHER category, or remove that
        # category from every OTHER book.
        before = len(df)
        book_lower = df['book'].str.lower()
        category_lower = df['category'].str.lower()
        pair_lower = book_lower + '|' + category_lower
        df = df[~pair_lower.isin(exclude_book_categories)]
        print(f"Excluding specific (book, category) pairs {sorted(exclude_book_categories)}: "
              f"{before - len(df)} rows removed, {len(df)} remain")

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

        sizing = _balanced_stakes(low_row['over_odds'], high_row['under_odds'], total_stake)

        opportunities.append({
            'player_name': player,
            'category': category,
            'team': low_row.get('team'),
            'low_book': low_row['book'], 'low_line': low_row['line'], 'low_over_odds': low_row['over_odds'],
            'high_book': high_row['book'], 'high_line': high_row['line'], 'high_under_odds': high_row['under_odds'],
            'middle_width': round(middle_width, 2),
            'books_compared': len(group),
            'stake_over': sizing['stake_over'], 'stake_under': sizing['stake_under'],
            'loss_if_miss': sizing['loss_if_miss'], 'profit_if_middle_hits': sizing['profit_if_middle_hits'],
        })

    result = pd.DataFrame(opportunities)
    if not result.empty:
        result = result.sort_values('middle_width', ascending=False)
    return result


def main(input_path: str, min_width: float, top_n: int, total_stake: float,
        exclude_books: set = None, exclude_book_categories: set = None, with_ev: bool = False):
    df = pd.read_csv(input_path)
    print(f"Loaded {len(df)} book-level rows across "
          f"{df.groupby(['player_name', 'category']).ngroups} (player, category) pairs")

    opportunities = find_middles(df, min_width, total_stake, exclude_books, exclude_book_categories)

    if opportunities.empty:
        print(f"\nNo middling opportunities found with width >= {min_width}.")
        return

    print(f"\n{len(opportunities)} opportunities found with width >= {min_width}")

    base = input_path.replace('.csv.gz', '').replace('.csv', '')
    is_filtered = bool(exclude_books or exclude_book_categories)
    suffix = '_middling_opportunities_filtered.csv' if is_filtered else '_middling_opportunities.csv'
    out_path = base + suffix

    if with_ev:
        # Reuses estimate_middling_ev.py's actual computation directly
        # (not a reimplementation) — this needs the Flask app +
        # database, unlike everything above, which is why --with-ev
        # is opt-in rather than the default: the plain middling-width
        # calculation can run standalone with no database at all, and
        # turning that into a hard requirement for every run would
        # take that away.
        import estimate_middling_ev
        opportunities = estimate_middling_ev.compute_ev_for_opportunities(opportunities, df, exclude_books)
        estimable = opportunities.dropna(subset=['expected_value'])
        print(f"EV computed for {len(estimable)}/{len(opportunities)} opportunities "
              f"({len(opportunities) - len(estimable)} skipped — see 'note' column for why)")
        if not estimable.empty:
            opportunities = pd.concat([
                estimable.sort_values('expected_value', ascending=False),
                opportunities[opportunities['expected_value'].isna()]
            ])
        display_cols = ['player_name', 'category', 'low_book', 'low_line', 'high_book', 'high_line',
                        'middle_width', 'p_middle_hits', 'expected_value', 'note']
        print(f"\nTop {min(top_n, len(opportunities))} (sorted by expected value where available):\n")
        print(opportunities[display_cols].head(top_n).to_string(index=False))
    else:
        print(f"\nTop {min(top_n, len(opportunities))} by middle width "
              f"(sized for a ${total_stake:.0f} total stake per opportunity):\n")
        display_cols = ['player_name', 'category', 'low_book', 'low_line', 'high_book', 'high_line',
                        'middle_width', 'stake_over', 'stake_under', 'loss_if_miss', 'profit_if_middle_hits']
        print(opportunities[display_cols].head(top_n).to_string(index=False))

    opportunities.to_csv(out_path, index=False)
    print(f"\nFull results saved -> {out_path}")

    print(f"\nStake sizing balances the two legs so losing EITHER side alone costs the "
          f"same ('loss_if_miss') — bet more on whichever leg has worse odds."
          + (" EV estimates use the market's consensus line for the mean and the "
             "player's real historical variance — see estimate_middling_ev.py's "
             "own docstring for the real limitations (needs 6+ historical games, "
             "weak for low-count stats)." if with_ev else
             " This still ranks by middle WIDTH only, not a modeled probability of "
             "landing in it — pass --with-ev for a real EV estimate instead.")
          + " Remember middling is a variance play (usually the small 'loss_if_miss' "
          f"amount, occasionally the big 'profit_if_middle_hits' payout), not "
          f"guaranteed profit like pure arbitrage.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_market_comparison.csv.gz")
    parser.add_argument("--min-width", type=float, default=1.0,
                        help="Minimum gap between the lowest and highest line to count "
                             "as an opportunity (in the stat's own units, e.g. yards).")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--total-stake", type=float, default=100.0,
                        help="Total $ across both legs, used to compute balanced stake "
                             "sizing per opportunity (default $100).")
    parser.add_argument("--exclude-books", default=None,
                        help="Comma-separated book slugs to exclude entirely from "
                             "consideration, e.g. 'bet365,underdog,prizepicks' — for "
                             "books unavailable in your state. Omit for the "
                             "unrestricted view across every book.")
    parser.add_argument("--exclude-book-category", default=None,
                        help="Comma-separated book:category pairs to exclude — for a "
                             "book that's missing a specific market entirely (e.g. "
                             "Caesars not offering rushing-and-receiving-yards lines), "
                             "without removing that book from every other category or "
                             "that category from every other book. Format: "
                             "'caesars:rushing-and-receiving-yards,otherbook:othercat'.")
    parser.add_argument("--with-ev", action="store_true",
                        help="Also compute expected value for each opportunity, in the "
                             "same run — needs the Flask app/database (unlike everything "
                             "else here, which is pure CSV processing), so this is opt-in "
                             "rather than the default.")
    args = parser.parse_args()

    exclude_set = None
    if args.exclude_books:
        exclude_set = {b.strip().lower() for b in args.exclude_books.split(',')}

    exclude_book_category_set = None
    if args.exclude_book_category:
        exclude_book_category_set = set()
        for pair in args.exclude_book_category.split(','):
            book, _, category = pair.strip().partition(':')
            if not category:
                print(f"ERROR: '--exclude-book-category' entry {pair!r} isn't in "
                      f"'book:category' format — skipping it.")
                continue
            exclude_book_category_set.add(f"{book.strip().lower()}|{category.strip().lower()}")

    main(args.input, args.min_width, args.top, args.total_stake, exclude_set,
        exclude_book_category_set, args.with_ev)
