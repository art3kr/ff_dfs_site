"""
scrapers/select_top_props_by_category.py
---------------------------------------------
Takes the first 6 rows per category from convert_scoresandodds_to_props_csv.py's
output, preserving the site's own original order within each category
— automates what was previously a manual "trim this down to ~20-25" step.

Exception: anytime-touchdown props are ranked by the site's own
projection instead. ScoresAndOdds orders that category long-shots-first,
so taking its first 6 produced dead picks nobody would ever take the
Over on — the 2026 Week 2 slate led with Tanner Koziol (+2300) and Tyler
Badie (+3500), both projected at 0.000 TDs. Ranking by projection gives
the actual TD threats (Henry, McCaffrey, Gibbs). Every other category's
own order is already sensible, so only this one is re-ranked.

With 10 convertible categories x 6 each, this produces up to 60 props
total — more than the original manual ~20-25 target. That's
intentional per your own choice, not a bug: a bigger candidate pool
for participants to pick their 5 from, not a fixed weekly slate size.

Usage:
    python scrapers/select_top_props_by_category.py \\
        --input data/props_candidates_week1.csv \\
        --output data/props_week1.csv \\
        --top-n 6
"""

import argparse
import pandas as pd


# Categories whose own page order buries the props anyone would actually
# pick, so they get ranked by the site's projection instead. See the
# module docstring for the anytime-TD evidence.
RANK_BY_PROJECTION = {'touchdowns'}


def main(input_path: str, output_path: str, top_n: int):
    df = pd.read_csv(input_path)

    if 'category_original' not in df.columns:
        print("ERROR: expected a 'category_original' column — is this "
              "convert_scoresandodds_to_props_csv.py's output?")
        return

    print(f"Loaded {len(df)} candidate props across "
          f"{df['category_original'].nunique()} categories")

    # Category order and within-category row order both stay as the site
    # had them, EXCEPT for RANK_BY_PROJECTION categories (see above).
    df['_proj'] = pd.to_numeric(df.get('site_projection'), errors='coerce')
    frames = []
    for cat in df['category_original'].unique():
        group = df[df['category_original'] == cat]
        if cat in RANK_BY_PROJECTION:
            group = group.sort_values('_proj', ascending=False)
        frames.append(group.head(top_n))
    selected = pd.concat(frames).drop(columns='_proj')

    print(f"\nSelected {len(selected)} props (up to {top_n} per category):")
    for cat, count in selected['category_original'].value_counts().reindex(
            df['category_original'].unique(), fill_value=0).items():
        print(f"  {cat}: {count}")

    selected.to_csv(output_path, index=False)
    print(f"\nSaved -> {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-n", type=int, default=6)
    args = parser.parse_args()
    main(args.input, args.output, args.top_n)
