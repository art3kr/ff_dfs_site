"""
scrapers/convert_scoresandodds_to_props_csv.py
---------------------------------------------------
Bridges scrape_scoresandodds_props.py's output into a CSV shaped for
`flask add-props` — but this is a CANDIDATE POOL, not the final
weekly slate. Every prop `add-props` accepts must map to exactly one
hist_player_stats column (that's what makes auto-scoring work), so
only categories that do that cleanly get converted here; anything
that doesn't (a composite stat, a longest-play prop, a yes/no
proposition) is reported and skipped rather than silently dropped.

Categories that DO convert (map 1:1 to a real stat_field):
    passing-yards, rushing-yards, receiving-yards, receptions,
    passing-tds, completions, interceptions, pass-attempts,
    rush-attempts

Categories that DON'T (and why):
    touchdowns                      — combines rush_td + rec_td, no
                                       single matching column
    passing-and-rushing-yards       — composite (pass_yds + rush_yds)
    rushing-and-receiving-yards     — composite (rush_yds + rec_yds)
    first-touchdown-scorer          — yes/no proposition, not a line
    last-touchdown-scorer           — yes/no proposition, not a line
    longest-reception                — we don't track single-play
    longest-rush                     — longest-X stats at all
    longest-completion
    kicking-points                   — we don't track kicker stats

The output keeps the site's projection, odds, and best-book info as
extra reference columns for your own curation decisions — add-props
only reads player_name/stat_field/line and ignores the rest, so
nothing needs stripping before you run it, but you'll still want to
open this in a spreadsheet and cut it down from "every convertible
prop we scraped" to your actual weekly ~20 (or whatever number of
CATEGORIES you want represented — this pool will have way more than
20 rows once combined across 9 categories).

Usage:
    python scrapers/convert_scoresandodds_to_props_csv.py \\
        --input data/scoresandodds_props_all.csv.gz \\
        --output data/props_candidates_week1.csv
"""

import os
import argparse
import pandas as pd

CATEGORY_TO_STAT_FIELD = {
    'passing-yards':   'pass_yds',
    'rushing-yards':   'rush_yds',
    'receiving-yards': 'rec_yds',
    'receptions':      'rec',
    'passing-tds':     'pass_td',
    'completions':     'pass_cmp',
    'interceptions':   'pass_int',
    'pass-attempts':   'pass_att',
    'rush-attempts':   'rush_att',
}

UNSUPPORTED_CATEGORIES = {
    'touchdowns':                   "combines rush_td + rec_td, no single matching column",
    'passing-and-rushing-yards':    "composite stat (pass_yds + rush_yds)",
    'rushing-and-receiving-yards':  "composite stat (rush_yds + rec_yds)",
    'first-touchdown-scorer':       "yes/no proposition, not a line",
    'last-touchdown-scorer':        "yes/no proposition, not a line",
    'longest-reception':            "we don't track single-play 'longest X' stats",
    'longest-rush':                 "we don't track single-play 'longest X' stats",
    'longest-completion':           "we don't track single-play 'longest X' stats",
    'kicking-points':               "we don't track kicker stats at all",
}


def main(input_path: str, output_path: str):
    if not os.path.exists(input_path):
        print(f"ERROR: {input_path} not found.")
        return

    df = pd.read_csv(input_path)
    print(f"Loaded {len(df)} scraped rows from {input_path}")

    if 'category' not in df.columns:
        print("ERROR: expected a 'category' column — is this the combined "
              "scoresandodds_props_all.csv.gz output from --all --combine?")
        return

    category_counts = df['category'].value_counts()
    print(f"\nCategories present: {dict(category_counts)}")

    convertible = df[df['category'].isin(CATEGORY_TO_STAT_FIELD.keys())].copy()
    skipped_categories = set(df['category'].unique()) - set(CATEGORY_TO_STAT_FIELD.keys())

    if skipped_categories:
        print(f"\nSkipping {len(df) - len(convertible)} rows across "
              f"{len(skipped_categories)} unsupported categories:")
        for cat in sorted(skipped_categories):
            reason = UNSUPPORTED_CATEGORIES.get(cat, "not in the known category list at all — "
                                                       "check for a typo or a new category")
            count = category_counts.get(cat, 0)
            print(f"  {cat} ({count} rows): {reason}")

    if convertible.empty:
        print("\nNothing convertible — no output written.")
        return

    convertible['stat_field'] = convertible['category'].map(CATEGORY_TO_STAT_FIELD)

    # Prefer the over_line as the canonical line (matches under_line in
    # the vast majority of real cases — a prop's line is one number,
    # just quoted with different odds on each side); fall back to
    # under_line for the rare row missing an over side.
    convertible['line'] = convertible['over_line'].fillna(convertible['under_line'])

    out = convertible[[
        'player_name', 'stat_field', 'line',
        'category', 'team', 'opponent', 'home_away',
        'over_odds', 'over_book', 'under_odds', 'under_book',
        'site_projection', 'projection_diff',
    ]].rename(columns={'category': 'category_original'})

    out.to_csv(output_path, index=False)
    print(f"\nSaved {len(out)} candidate props -> {output_path}")
    print(f"\nNext steps:")
    print(f"  1. Open {output_path} in a spreadsheet")
    print(f"  2. Trim down to your actual weekly slate (delete rows you don't want)")
    print(f"  3. flask add-props {output_path} --year YYYY --week N")
    print(f"     (extra columns like category_original/odds/site_projection are")
    print(f"     ignored by add-props — only player_name/stat_field/line matter)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/scoresandodds_props_all.csv.gz")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    main(args.input, args.output)
