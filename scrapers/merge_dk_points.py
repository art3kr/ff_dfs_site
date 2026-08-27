"""
scrapers/merge_dk_points.py
------------------------------
Adds PFR's own originally-reported dk_pts (from the old /fantasy/{year}/
scrape) into the new dataset as a separate reference column, rather than
relying solely on calculate_dk_points()'s formula — which has known gaps
(2-point conversions aren't captured; see calculate_dk_points()
docstring).

The NEW file stays the base (accurate full-game raw stats — pass_yds,
TDs, return stats, etc. — which the OLD file's redzone-limited fields
didn't have). We only borrow the OLD file's dk_pts value, matched on
(pfr_id, year, week), as an extra column called dk_pts_pfr_reported.

Rows that only exist in the new file (e.g. 2025+ games the old scrape
never covered) simply get a blank/null dk_pts_pfr_reported — our own
computed dk_pts stands alone for those.

Usage:
    python scrapers/merge_dk_points.py
    python scrapers/merge_dk_points.py --old data/pfr_player_stats_2014_2025_old.csv.gz \
                                        --new data/pfr_player_stats_2014_2025.csv.gz

By default this OVERWRITES the new file in place (after saving a .bak
backup), since that's the file flask load-history reads. Use --output
to write somewhere else instead.
"""

import os
import shutil
import argparse
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')


def main(old_path: str, new_path: str, output_path: str, no_backup: bool):
    if not os.path.exists(old_path):
        print(f"ERROR: old file not found: {old_path}")
        return
    if not os.path.exists(new_path):
        print(f"ERROR: new file not found: {new_path}")
        return

    print(f"Old (PFR-reported) file: {old_path}")
    print(f"New (computed) file:     {new_path}")
    print(f"Output:                  {output_path}\n")

    old = pd.read_csv(old_path)
    new = pd.read_csv(new_path)

    for df in (old, new):
        df['pfr_id'] = df['pfr_id'].astype(str)
        df['year']   = df['year'].astype(int)
        df['week']   = df['week'].astype(int)

    # Just the reference column we're borrowing, keyed for the merge
    old_ref = old[['pfr_id', 'year', 'week', 'dk_pts']].rename(
        columns={'dk_pts': 'dk_pts_pfr_reported'}
    )
    old_ref = old_ref.drop_duplicates(subset=['pfr_id', 'year', 'week'])

    merged = new.merge(old_ref, on=['pfr_id', 'year', 'week'], how='left')

    matched   = merged['dk_pts_pfr_reported'].notna().sum()
    unmatched = merged['dk_pts_pfr_reported'].isna().sum()
    print(f"New file rows: {len(new):,}")
    print(f"Matched to an old-file dk_pts_pfr_reported value: {matched:,}")
    print(f"No match found (old file didn't cover this row):  {unmatched:,}")

    diffs = (merged['dk_pts'] - merged['dk_pts_pfr_reported']).abs()
    diffs = diffs.dropna()
    if not diffs.empty:
        print(f"\nAmong matched rows:")
        print(f"  Rows matching exactly: {(diffs == 0).sum():,} ({(diffs == 0).mean()*100:.1f}%)")
        print(f"  Mean abs difference:   {diffs.mean():.3f} points")

    if not no_backup and output_path == new_path:
        backup_path = new_path + '.bak'
        shutil.copy2(new_path, backup_path)
        print(f"\nBackup of original new file saved to: {backup_path}")

    merged.to_csv(output_path, index=False, compression='gzip')
    print(f"\nSaved merged file ({len(merged):,} rows, "
          f"{len(merged.columns)} columns) → {output_path}")
    print(f"New column added: dk_pts_pfr_reported")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", default=os.path.join(DATA_DIR, "pfr_player_stats_2014_2025_old.csv.gz"))
    parser.add_argument("--new", default=os.path.join(DATA_DIR, "pfr_player_stats_2014_2025.csv.gz"))
    parser.add_argument("--output", default=None,
                        help="Where to save. Defaults to overwriting --new (with a .bak backup first).")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip creating a .bak file when overwriting --new in place.")
    args = parser.parse_args()

    output = args.output if args.output else args.new
    main(args.old, args.new, output, args.no_backup)
