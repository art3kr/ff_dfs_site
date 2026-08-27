"""
scrapers/clean_old_file_years.py
-----------------------------------
Deletes rows for specific years from the "_old" PFR file — useful before
re-running scrape_fantasy_dk_points.py to fix a bad prior run (e.g. the
January week-18 games that got mis-tagged as year 2026 instead of 2025
before that bug was fixed).

Defaults to removing years 2025 and 2026, since that's the immediate
case this was built for, but any year(s) can be specified.

Makes a .bak backup before touching the file, unless --no-backup is set.

Usage:
    python scrapers/clean_old_file_years.py
    python scrapers/clean_old_file_years.py --years 2026
    python scrapers/clean_old_file_years.py --years 2025,2026 --old-file data/pfr_player_stats_2014_2025_old.csv.gz
"""

import os
import shutil
import argparse
import pandas as pd

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
DEFAULT_OLD = os.path.join(DATA_DIR, 'pfr_player_stats_2014_2025_old.csv.gz')


def main(old_path: str, years_to_remove: list[int], no_backup: bool):
    if not os.path.exists(old_path):
        print(f"ERROR: file not found: {old_path}")
        return

    print(f"File:              {old_path}")
    print(f"Years to remove:   {years_to_remove}\n")

    df = pd.read_csv(old_path)
    df['year'] = df['year'].astype(int)

    before = len(df)
    matching = df[df['year'].isin(years_to_remove)]
    print(f"Total rows before: {before:,}")
    print(f"Rows matching years to remove: {len(matching):,}")

    if matching.empty:
        print("\nNothing to remove — file already clean for these years.")
        return

    # Show a quick breakdown by year so it's obvious what's being deleted
    print("\nBreakdown of rows being removed:")
    print(matching['year'].value_counts().sort_index().to_string())

    if not no_backup:
        backup_path = old_path + '.bak'
        shutil.copy2(old_path, backup_path)
        print(f"\nBackup saved to: {backup_path}")

    cleaned = df[~df['year'].isin(years_to_remove)]
    cleaned.to_csv(old_path, index=False, compression='gzip')

    print(f"\nRows after removal: {len(cleaned):,}")
    print(f"Removed:             {before - len(cleaned):,}")
    print(f"Saved cleaned file → {old_path}")


def parse_years(s: str) -> list[int]:
    if ',' in s:
        return [int(y.strip()) for y in s.split(',')]
    elif '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    else:
        return [int(s)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--years', default='2025,2026',
                        help='Comma or dash-separated year(s) to remove, e.g. "2025,2026" or "2025-2026"')
    parser.add_argument('--old-file', default=DEFAULT_OLD,
                        help=f'Path to the file to clean (default: {DEFAULT_OLD})')
    parser.add_argument('--no-backup', action='store_true',
                        help='Skip creating a .bak file before overwriting')
    args = parser.parse_args()

    years = parse_years(args.years)
    main(args.old_file, years, args.no_backup)
