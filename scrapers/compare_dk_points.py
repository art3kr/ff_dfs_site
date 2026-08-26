"""
scrapers/compare_dk_points.py
------------------------------
Compares the DK points we now COMPUTE (calculate_dk_points(), from real
box-score stats on the /gamelog/ career page) against the DK points PFR
used to REPORT directly (draftkings_points field, from the old
/fantasy/{year}/ scrape) — to validate the scoring formula before
trusting it for the full 2014-2025 run.

Usage:
    python scrapers/compare_dk_points.py
    python scrapers/compare_dk_points.py --old data/pfr_player_stats_2014_2025_old.csv.gz \
                                          --new data/pfr_player_stats_2014_2025.csv.gz

Output:
    - Summary stats on how close the two match
    - The worst N discrepancies, with every underlying stat shown side
      by side, so a systematic formula bug (missed bonus, wrong
      multiplier, missing stat category) is easy to spot
    - Full comparison saved to data/dk_points_comparison.csv for further
      digging if needed
"""

import os
import argparse
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')


def main(old_path: str, new_path: str, top_n: int, threshold: float):
    if not os.path.exists(old_path):
        print(f"ERROR: old file not found: {old_path}")
        print("Pass its exact path with --old, e.g.:")
        print("  python scrapers/compare_dk_points.py --old data/pfr_player_stats_2014_2025_old.csv.gz")
        return
    if not os.path.exists(new_path):
        print(f"ERROR: new file not found: {new_path}")
        print("Run the scraper first, or pass --new with the right path.")
        return

    print(f"Old (PFR-reported) file: {old_path}")
    print(f"New (computed) file:     {new_path}\n")

    old = pd.read_csv(old_path)
    new = pd.read_csv(new_path)

    print(f"Old rows: {len(old):,}")
    print(f"New rows: {len(new):,}\n")

    # Standardize the join keys' types
    for df in (old, new):
        df['pfr_id'] = df['pfr_id'].astype(str)
        df['year']   = df['year'].astype(int)
        df['week']   = df['week'].astype(int)

    # Suffix all non-key columns so we can compare old vs new side by side
    old_cols = {c: f"{c}_old" for c in old.columns if c not in ('pfr_id', 'year', 'week')}
    new_cols = {c: f"{c}_new" for c in new.columns if c not in ('pfr_id', 'year', 'week')}
    old_r = old.rename(columns=old_cols)
    new_r = new.rename(columns=new_cols)

    merged = old_r.merge(new_r, on=['pfr_id', 'year', 'week'], how='inner')
    print(f"Matched rows (same pfr_id/year/week in both files): {len(merged):,}")

    only_old = old_r.merge(new_r, on=['pfr_id', 'year', 'week'], how='left', indicator=True)
    only_old = only_old[only_old['_merge'] == 'left_only']
    only_new = new_r.merge(old_r, on=['pfr_id', 'year', 'week'], how='left', indicator=True)
    only_new = only_new[only_new['_merge'] == 'left_only']
    print(f"Rows only in old file (not in new): {len(only_old):,}")
    print(f"Rows only in new file (not in old): {len(only_new):,}\n")

    if merged.empty:
        print("No overlapping rows to compare — check that both files cover the same years.")
        return

    merged['diff']     = merged['dk_pts_new'] - merged['dk_pts_old']
    merged['abs_diff']  = merged['diff'].abs()

    print("="*70)
    print("SUMMARY")
    print("="*70)
    print(f"Mean absolute difference:   {merged['abs_diff'].mean():.3f} points")
    print(f"Median absolute difference: {merged['abs_diff'].median():.3f} points")
    print(f"Max absolute difference:    {merged['abs_diff'].max():.3f} points")
    print(f"Rows matching exactly (diff=0):      {(merged['abs_diff'] == 0).sum():,} "
          f"({(merged['abs_diff'] == 0).mean()*100:.1f}%)")
    print(f"Rows within 0.5 pts:                  {(merged['abs_diff'] <= 0.5).sum():,} "
          f"({(merged['abs_diff'] <= 0.5).mean()*100:.1f}%)")
    print(f"Rows with diff > {threshold} pts:            {(merged['abs_diff'] > threshold).sum():,} "
          f"({(merged['abs_diff'] > threshold).mean()*100:.1f}%)")

    # Save full comparison for further digging
    out_path = os.path.join(DATA_DIR, 'dk_points_comparison.csv')
    merged.sort_values('abs_diff', ascending=False).to_csv(out_path, index=False)
    print(f"\nFull comparison saved to: {out_path}")

    # Show worst offenders with all underlying stats, to spot a
    # systematic formula bug (missing bonus, wrong multiplier, etc.)
    worst = merged.sort_values('abs_diff', ascending=False).head(top_n)

    if worst.empty or worst['abs_diff'].max() == 0:
        print("\nNo discrepancies found — computed dk_pts matches PFR's reported "
              "values exactly for every overlapping row. Formula is validated.")
        return

    print(f"\n{'='*70}")
    print(f"TOP {top_n} WORST DISCREPANCIES")
    print(f"{'='*70}")

    display_cols = [
        'pfr_id', 'name_old', 'year', 'week', 'position_old',
        'dk_pts_old', 'dk_pts_new', 'diff',
        'pass_yds_new', 'pass_td_new', 'pass_int_new',
        'rush_yds_new', 'rush_td_new',
        'rec_new', 'rec_yds_new', 'rec_td_new',
        'fumbles_lost_new',
    ]
    display_cols = [c for c in display_cols if c in worst.columns]

    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 220)
    print(worst[display_cols].to_string(index=False))

    print(f"\nLook for a PATTERN in the worst rows — e.g., all QBs (missing a")
    print(f"passing bonus?), all games with fumbles_lost>0 (fumble scoring")
    print(f"wrong?), or all high-yardage games (bonus threshold wrong?).")
    print(f"Paste this output back if the pattern isn't obvious and we'll")
    print(f"fix calculate_dk_points() accordingly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", default=os.path.join(DATA_DIR, "pfr_player_stats_2014_2025_old.csv.gz"))
    parser.add_argument("--new", default=os.path.join(DATA_DIR, "pfr_player_stats_2014_2025.csv.gz"))
    parser.add_argument("--top", type=int, default=20, help="How many worst discrepancies to show")
    parser.add_argument("--threshold", type=float, default=1.0, help="Point difference considered 'significant'")
    args = parser.parse_args()

    main(args.old, args.new, args.top, args.threshold)
