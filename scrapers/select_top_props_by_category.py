"""
scrapers/select_top_props_by_category.py
---------------------------------------------
Takes the first 6 rows per category from convert_scoresandodds_to_props_csv.py's
output, preserving the site's own original order within each category
(no re-sorting) — automates what was previously a manual "trim this
down to ~20-25" step.

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


def main(input_path: str, output_path: str, top_n: int):
    df = pd.read_csv(input_path)

    if 'category_original' not in df.columns:
        print("ERROR: expected a 'category_original' column — is this "
              "convert_scoresandodds_to_props_csv.py's output?")
        return

    print(f"Loaded {len(df)} candidate props across "
          f"{df['category_original'].nunique()} categories")

    # sort=False preserves each category's original position (first
    # appearance order), and .head(n) within a groupby preserves each
    # group's own original row order — no re-sorting happens anywhere.
    selected = df.groupby('category_original', sort=False).head(top_n)

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
