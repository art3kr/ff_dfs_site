"""
scrapers/diagnose_name_mismatches.py
---------------------------------------
Pulls the EXACT name_normalized string PFR data uses for a set of known
mismatched players, and searches for any hist_player_stats row with a
SIMILAR (not identical) normalized name — so we can see precisely what
differs (an extra space, a missing period-stripped character, a suffix,
etc.) instead of guessing.

Usage:
    python scrapers/diagnose_name_mismatches.py
"""

import os
import sys

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from app import app, _connect, _cursor, _is_postgres, _ph

# Real (non-DST) unmatched names from the match-rate report
PROBLEM_NAMES = [
    "Slater, Matthew",
    "Hilton, T.Y.",
    "Green, A.J.",
    "Anderson, Robby",
    "Harris, Clark",
]


def main():
    with app.app_context():
        conn = _connect()
        cur  = _cursor(conn)
        ph   = _ph()

        for raw_name in PROBLEM_NAMES:
            print(f"\n{'='*70}")
            print(f"'{raw_name}'")
            print(f"{'='*70}")

            # Get the actual name_normalized this player has in salaries
            cur.execute(f"""
                SELECT DISTINCT name_normalized, source
                FROM hist_dfs_salaries
                WHERE name = {ph}
                LIMIT 5
            """, (raw_name,))
            salary_rows = cur.fetchall()
            for r in salary_rows:
                print(f"  SALARY  name_normalized = '{r['name_normalized']}'  (source: {r['source']})")

            if not salary_rows:
                print(f"  (no exact raw-name match in hist_dfs_salaries — trying LIKE search)")
                continue

            salary_norm = salary_rows[0]['name_normalized']

            # Exact match check in stats
            cur.execute(f"""
                SELECT DISTINCT name, name_normalized
                FROM hist_player_stats
                WHERE name_normalized = {ph}
                LIMIT 5
            """, (salary_norm,))
            exact = cur.fetchall()
            print(f"\n  Exact match in hist_player_stats for '{salary_norm}': {len(exact)} found")
            for r in exact:
                print(f"    -> name='{r['name']}'  name_normalized='{r['name_normalized']}'")

            # Fuzzy search: does a SIMILAR name exist in stats?
            # Use just the last name fragment for a broad search
            last_name_guess = salary_norm.split()[-1] if salary_norm else ''
            if last_name_guess:
                like_pattern = f"%{last_name_guess}%"
                cur.execute(f"""
                    SELECT DISTINCT name, name_normalized
                    FROM hist_player_stats
                    WHERE name_normalized LIKE {ph}
                    LIMIT 5
                """, (like_pattern,))
                similar = cur.fetchall()
                print(f"\n  Similar names in hist_player_stats matching '*{last_name_guess}*':")
                if not similar:
                    print(f"    (none found at all — this player may not be in hist_player_stats)")
                for r in similar:
                    print(f"    -> name='{r['name']}'  name_normalized='{r['name_normalized']}'")

        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
