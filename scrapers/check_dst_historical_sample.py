"""
scrapers/check_dst_historical_sample.py
-------------------------------------------
Quick spot-check of scrape_pfr_defense_historical.py's output — real
values, not just "did it crash". Confirms Baltimore's Week 1 2024 row
looks right against what we already know from the live diagnostic
(3 of their defenders alone summed to 1.5 sacks, 1 INT — the full
11-defender roster total should be at least that high).

Usage:
    python scrapers/check_dst_historical_sample.py
"""

import os
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
PATH = os.path.join(DATA_DIR, 'hist_dst_stats_historical.csv.gz')


def main():
    if not os.path.exists(PATH):
        print(f"File not found: {PATH}")
        return

    df = pd.read_csv(PATH)
    print(f"Total rows: {len(df)}\n")

    bal_wk1 = df[(df['year'] == 2024) & (df['week'] == 1) & (df['team'] == 'bal')]
    print("Baltimore Week 1 2024:")
    print(bal_wk1.to_string(index=False))
    print()

    print("Sack value distribution (should look like a normal NFL season —")
    print("most teams 1-4 sacks/game, occasional 0, rare 6+):")
    print(df['sack'].describe())
    print()

    suspicious = df[(df['sack'] == 0) & (df['interception'] == 0) &
                    (df['fumble_rec'] == 0) & (df['def_td'] == 0)]
    print(f"Rows with zero everything: {len(suspicious)}")
    print("(Some teams genuinely have quiet games — this isn't necessarily broken,")
    print("just worth a glance if the count seems unusually high.)")


if __name__ == "__main__":
    main()
