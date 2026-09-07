"""
scrapers/diagnose_scoresandodds_props_dupes.py
--------------------------------------------------
Finds exactly which rows in your real scraped
data/scoresandodds_props_all.csv.gz collide on (category,
player_name_normalized) — the crash reported was specifically a
"touchdowns" row normalizing to "650", which looks suspiciously like
the numeric portion of a moneyline price (+650) rather than an actual
player name. This dumps the raw rows involved so we can see whether
that's really what happened, rather than guessing.

Usage:
    python scrapers/diagnose_scoresandodds_props_dupes.py
"""

import os
import re
import sys
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
FILE_PATH = os.path.join(DATA_DIR, 'scoresandodds_props_all.csv.gz')


def normalize_name(name: str) -> str:
    """Matches app.py's normalize_name() exactly (lowercase, strip
    non-alphanumeric) — inlined here rather than imported, since it
    actually lives in app.py, not team_mapping.py, and importing the
    full Flask app just for this one function isn't worth the
    initialization overhead/side effects."""
    return re.sub(r"[^a-z0-9 ]", "", str(name).lower().strip())


def main():
    if not os.path.exists(FILE_PATH):
        print(f"{FILE_PATH} not found.")
        return

    df = pd.read_csv(FILE_PATH)
    print(f"Loaded {len(df)} rows, {df['category'].nunique()} categories\n")

    df['player_name_normalized'] = df['player_name'].astype(str).apply(normalize_name)

    dupe_mask = df.duplicated(subset=['category', 'player_name_normalized'], keep=False)
    dupes = df[dupe_mask].sort_values(['category', 'player_name_normalized'])

    if dupes.empty:
        print("No duplicate (category, player_name_normalized) pairs found — "
              "the file looks clean now. If the crash still happened, it may "
              "have been from an earlier scrape run before a fix.")
        return

    print(f"{len(dupes)} rows across {dupes.groupby(['category', 'player_name_normalized']).ngroups} "
          f"colliding (category, player_name_normalized) pairs:\n")

    for (category, name_norm), group in dupes.groupby(['category', 'player_name_normalized']):
        print(f"category={category!r}, player_name_normalized={name_norm!r} ({len(group)} rows):")
        print(group.to_string(index=False))
        print()

    # Specifically check whether any of the colliding normalized names
    # look numeric (like the reported "650") rather than a real name
    numeric_looking = dupes[dupes['player_name_normalized'].str.match(r'^\d+$', na=False)]
    if not numeric_looking.empty:
        print(f"*** {len(numeric_looking)} row(s) have a PURELY NUMERIC player_name_normalized "
              f"(not a real name) — these are the ones worth looking at closely: ***")
        print(numeric_looking[['category', 'player_name', 'player_name_normalized',
                               'moneyline_odds', 'over_line', 'under_line']].to_string(index=False))


if __name__ == "__main__":
    main()
