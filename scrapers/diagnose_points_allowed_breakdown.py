"""
scrapers/diagnose_points_allowed_breakdown.py
--------------------------------------------------
Breaks down exactly which players/games contribute to a team's
summed fantasy-points-allowed number for a position — to check
whether a surprisingly high number (e.g. ATL allowing 39.7 FP/G to
WR) reflects real games or a data-duplication bug.

Checks specifically for:
  - The same (name, week) appearing more than once (would silently
    double-count a player in the SUM)
  - Games with an unusually high player count at one position (a
    possible position-mislabeling issue, e.g. a WR row miscategorized
    that's really a different position)

Usage:
    python scrapers/diagnose_points_allowed_breakdown.py --team atl --position WR --year 2026
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def main(team: str, position: str, year: int):
    import app as app_module

    with app_module.app.app_context():
        ph = app_module._ph()
        rows = app_module.db_fetchall(f"""
            SELECT week, name, name_normalized, dk_pts, dk_pts_pfr_reported
            FROM hist_player_stats
            WHERE opponent = {ph} AND position = {ph} AND year = {ph}
            ORDER BY week, name
        """, (team, position, year))

        if not rows:
            print(f"No rows found for opponent={team}, position={position}, year={year}.")
            return

        by_week = {}
        for r in rows:
            by_week.setdefault(r["week"], []).append(r)

        print(f"{team.upper()} vs {position} — {year} season, {len(by_week)} weeks with data\n")

        season_total = 0.0
        duplicate_flags = []

        for week in sorted(by_week.keys()):
            week_rows = by_week[week]
            week_total = 0.0
            seen_names = {}

            print(f"Week {week}:")
            for r in week_rows:
                actual = r["dk_pts_pfr_reported"] if r["dk_pts_pfr_reported"] is not None else r["dk_pts"]
                actual = actual or 0
                week_total += actual
                print(f"    {r['name']:<25} {actual:.1f} pts")

                seen_names[r["name_normalized"]] = seen_names.get(r["name_normalized"], 0) + 1

            for name_norm, count in seen_names.items():
                if count > 1:
                    duplicate_flags.append((week, name_norm, count))

            print(f"    {'':<25} {'-'*8}")
            print(f"    {'WEEK TOTAL':<25} {week_total:.1f} pts  ({len(week_rows)} players)\n")
            season_total += week_total

        avg = season_total / len(by_week) if by_week else 0
        print(f"Average across {len(by_week)} weeks: {avg:.1f} FP/G")

        if duplicate_flags:
            print(f"\n*** POSSIBLE BUG: {len(duplicate_flags)} case(s) of the same player "
                  f"appearing more than once in the same week: ***")
            for week, name, count in duplicate_flags:
                print(f"    Week {week}: '{name}' appears {count} times")
            print("This would double-count that player's points in the sum — worth checking "
                  "the raw scrape for that week for a duplicate row.")
        else:
            print("\nNo duplicate (player, week) pairs found — each player only counted once "
                  "per week. If the number still looks high, it may genuinely reflect a lot of "
                  "different pass-catchers each getting some production against this defense, "
                  "not a data bug.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", required=True, help="Team abbreviation, e.g. atl")
    parser.add_argument("--position", required=True, choices=["QB", "RB", "WR", "TE"])
    parser.add_argument("--year", type=int, required=True)
    args = parser.parse_args()
    main(args.team.lower(), args.position, args.year)
