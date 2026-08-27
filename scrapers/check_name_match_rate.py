"""
scrapers/check_name_match_rate.py
------------------------------------
Checks how well hist_dfs_salaries and hist_player_stats actually join
on name_normalized (the same join the /history page uses). Since this
is a text-based match rather than a shared player ID, it can silently
fail for nicknames, suffixes (Jr./Sr./II), or punctuation differences
between the salary sources (RotoGuru/RotoWire/DFF) and PFR's naming.

Reports:
  - Overall match rate (% of salaried player-weeks with a stats row)
  - Match rate broken down by year (to spot whether certain years/
    sources are worse than others)
  - The most frequent UNMATCHED names, so we can spot obvious fixable
    patterns at a glance

Usage:
    python scrapers/check_name_match_rate.py
"""

import os
import sys

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)   # so app.py's relative paths (e.g. sqlite file) resolve correctly

from app import app, _connect, _cursor, _is_postgres, _ph


def main():
    with app.app_context():
        conn = _connect()
        cur  = _cursor(conn)

        print(f"Database: {'Postgres' if _is_postgres() else 'SQLite'}\n")

        # Overall match rate
        cur.execute("""
            SELECT COUNT(*) AS total
            FROM hist_dfs_salaries
        """)
        total = cur.fetchone()['total']

        cur.execute("""
            SELECT COUNT(*) AS matched
            FROM hist_dfs_salaries s
            INNER JOIN hist_player_stats hp
                ON hp.year = s.year AND hp.week = s.week
                AND hp.name_normalized = s.name_normalized
        """)
        matched = cur.fetchone()['matched']

        unmatched = total - matched
        pct = (matched / total * 100) if total else 0

        print("=" * 60)
        print("OVERALL MATCH RATE")
        print("=" * 60)
        print(f"Total salaried player-weeks: {total:,}")
        print(f"Matched to a stats row:      {matched:,} ({pct:.1f}%)")
        print(f"Unmatched:                   {unmatched:,} ({100-pct:.1f}%)")

        # Breakdown by year
        print(f"\n{'='*60}")
        print("MATCH RATE BY YEAR")
        print("=" * 60)
        cur.execute("""
            SELECT s.year,
                   COUNT(*) AS total,
                   SUM(CASE WHEN hp.pfr_id IS NOT NULL THEN 1 ELSE 0 END) AS matched
            FROM hist_dfs_salaries s
            LEFT JOIN hist_player_stats hp
                ON hp.year = s.year AND hp.week = s.week
                AND hp.name_normalized = s.name_normalized
            GROUP BY s.year
            ORDER BY s.year
        """)
        for row in cur.fetchall():
            y, t, m = row['year'], row['total'], row['matched']
            pct_y = (m / t * 100) if t else 0
            bar = '#' * int(pct_y / 5)
            print(f"  {y}: {m:>6,}/{t:<6,} ({pct_y:5.1f}%) {bar}")

        # Most frequent unmatched names — sample for pattern-spotting
        print(f"\n{'='*60}")
        print("TOP 30 MOST FREQUENT UNMATCHED NAMES")
        print("=" * 60)
        cur.execute("""
            SELECT s.name, s.position, COUNT(*) AS n
            FROM hist_dfs_salaries s
            LEFT JOIN hist_player_stats hp
                ON hp.year = s.year AND hp.week = s.week
                AND hp.name_normalized = s.name_normalized
            WHERE hp.pfr_id IS NULL
            GROUP BY s.name, s.position
            ORDER BY n DESC
            LIMIT 30
        """)
        rows = cur.fetchall()
        if not rows:
            print("  (none — everything matched)")
        else:
            for row in rows:
                print(f"  {row['n']:>4}x  {row['name']:<28} ({row['position']})")

        print(f"\nIf you see real player names above (not DSTs or clearly-junk")
        print(f"rows), that points to a name-format mismatch worth fixing —")
        print(f"e.g. suffixes (Jr./Sr./II), punctuation, or nickname differences")
        print(f"between the salary source and PFR's naming convention.")

        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
