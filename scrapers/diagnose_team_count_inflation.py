"""
scrapers/diagnose_team_count_inflation.py
------------------------------------------------
Best Matchups' opponent rank went above 32 for a position — there are
only 32 real NFL teams, so this checks for the most likely cause:
inconsistent team code normalization across hist_player_stats' opponent
column, built up over 2014-2025 using different versions of
normalize_team() as it was fixed multiple times throughout this
project's history (jac/jax, historical Redskins/Raiders/Chargers/Rams
naming, etc.) — it's plausible some rows still carry an old,
un-normalized code for a relocated/renamed franchise while others use
the current one, making the same real team count as two "different"
teams.

Usage:
    python scrapers/diagnose_team_count_inflation.py --position WR
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

CANONICAL_32 = {
    'ari', 'atl', 'bal', 'buf', 'car', 'chi', 'cin', 'cle', 'dal', 'den',
    'det', 'gnb', 'hou', 'ind', 'jax', 'kan', 'lac', 'lar', 'lvr', 'mia',
    'min', 'nwe', 'nor', 'nyg', 'nyj', 'phi', 'pit', 'sea', 'sfo', 'tam',
    'ten', 'was',
}


def main(position: str):
    import app as app_module

    with app_module.app.app_context():
        ph = app_module._ph()
        rows = app_module.db_fetchall(f"""
            SELECT opponent, COUNT(*) AS row_count,
                   MIN(year) AS earliest_year, MAX(year) AS latest_year
            FROM hist_player_stats
            WHERE position = {ph}
            GROUP BY opponent
            ORDER BY opponent
        """, (position,))

        distinct_values = [r["opponent"] for r in rows]
        print(f"Distinct 'opponent' values for position={position}: {len(distinct_values)}")
        print(f"(Should be exactly 32 if clean)\n")

        non_canonical = [r for r in rows if r["opponent"] not in CANONICAL_32]
        canonical_found = [r for r in rows if r["opponent"] in CANONICAL_32]

        print(f"Canonical (recognized) team codes found: {len(canonical_found)} / 32")
        missing_canonical = CANONICAL_32 - {r["opponent"] for r in rows}
        if missing_canonical:
            print(f"Canonical teams with ZERO rows at this position: {sorted(missing_canonical)}")

        if non_canonical:
            print(f"\n*** {len(non_canonical)} NON-CANONICAL value(s) found — these are "
                  f"likely the source of the inflated team count: ***\n")
            for r in non_canonical:
                print(f"  '{r['opponent']}': {r['row_count']} rows, years {r['earliest_year']}-{r['latest_year']}")
            print(f"\nThese don't match any of the 32 real team codes and are likely old/")
            print(f"un-normalized values from before a normalize_team() fix, or a genuine typo.")
        else:
            print(f"\nAll {len(distinct_values)} values are recognized canonical codes — "
                  f"if the count is still over 32, something else is going on (share this "
                  f"output and it can be looked into further).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--position", required=True, choices=["QB", "RB", "WR", "TE"])
    args = parser.parse_args()
    main(args.position)
