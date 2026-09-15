"""
scrapers/diagnose_nflverse_usage.py
-------------------------------------
Structure check for the nflverse files a usage scraper would read, run
BEFORE writing that scraper (project convention: confirm real structure
first, never guess at columns).

Prints, for each file: HTTP status, size, release asset update time,
columns, row count, season/week coverage, and a few sample rows. Then
checks the things the scraper depends on:
  - snap counts: does offense_pct agree with the snap_pct we already
    store from PFR game logs?
  - players.csv: how many of our 2026 PFR ids map to an nflverse gsis_id?
  - play-by-play: are the columns needed for red-zone / inside-10 /
    inside-5 / aDOT present, and do Week 1 red-zone target leaders look
    sane?

Makes no requests to pro-football-reference.com. Writes nothing.

Usage:
    python scrapers/diagnose_nflverse_usage.py [--year 2026]
"""

import argparse
import io
import os

import pandas as pd
import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
API = "https://api.github.com/repos/nflverse/nflverse-data/releases/tags"


def fetch_csv(url: str) -> pd.DataFrame | None:
    print(f"\n{'=' * 70}\nGET {url}")
    r = requests.get(url, timeout=120)
    print(f"  status {r.status_code}, {len(r.content):,} bytes")
    if r.status_code != 200:
        return None
    compression = 'gzip' if url.endswith('.gz') else None
    df = pd.read_csv(io.BytesIO(r.content), compression=compression, low_memory=False)
    print(f"  {len(df):,} rows, {len(df.columns)} columns")
    print(f"  columns: {list(df.columns)}")
    if 'season' in df.columns:
        print(f"  seasons: {sorted(df['season'].dropna().unique().tolist())}")
    if 'week' in df.columns:
        print(f"  weeks: {sorted(df['week'].dropna().unique().tolist())}")
    if 'season_type' in df.columns:
        print(f"  season_type: {df['season_type'].value_counts().to_dict()}")
    return df


def asset_times(tag: str):
    r = requests.get(f"{API}/{tag}", timeout=30)
    if r.status_code != 200:
        print(f"  release {tag}: status {r.status_code}")
        return
    for a in r.json().get('assets', []):
        if any(k in a['name'] for k in ('2026', 'players.csv', 'ngs_receiving')):
            print(f"  release {tag}: {a['name']} updated {a['updated_at']}")


def main(year: int):
    for tag in ('snap_counts', 'stats_player', 'pbp', 'players'):
        asset_times(tag)

    snaps = fetch_csv(f"{RELEASES}/snap_counts/snap_counts_{year}.csv")
    stats = fetch_csv(f"{RELEASES}/stats_player/stats_player_week_{year}.csv")
    pbp = fetch_csv(f"{RELEASES}/pbp/play_by_play_{year}.csv.gz")
    players = fetch_csv(f"{RELEASES}/players/players.csv")

    ours = pd.read_csv(os.path.join(DATA_DIR, 'pfr_player_stats_2014_2025.csv.gz'))
    ours = ours[ours['year'] == year]
    print(f"\n{'=' * 70}\nOur {year} PFR rows: {len(ours)}, weeks {sorted(ours['week'].unique().tolist())}")

    if snaps is not None:
        print("\n--- snap counts sample ---")
        print(snaps.head(5).to_string(index=False))
        if 'pfr_player_id' in snaps.columns and 'offense_pct' in snaps.columns:
            m = ours.merge(snaps, left_on=['pfr_id', 'week'], right_on=['pfr_player_id', 'week'], how='left')
            matched = m['offense_pct'].notna().sum()
            print(f"our rows matched to snap_counts on pfr_id+week: {matched}/{len(m)}")
            both = m.dropna(subset=['offense_pct', 'snap_pct'])
            # nflverse offense_pct may be 0-1 or 0-100; print both views
            print(both[['name', 'week', 'snap_pct', 'offense_snaps', 'offense_pct']].head(10).to_string(index=False))
            unmatched = m[m['offense_pct'].isna()][['name', 'week', 'snap_pct']]
            print(f"unmatched sample:\n{unmatched.head(10).to_string(index=False)}")

    if players is not None and {'gsis_id', 'pfr_id'} <= set(players.columns):
        ids = players.dropna(subset=['pfr_id'])
        ours_ids = set(ours['pfr_id'])
        hit = ours_ids & set(ids['pfr_id'])
        print(f"\nour {year} pfr_ids with a gsis_id in players.csv: {len(hit)}/{len(ours_ids)}")
        print(f"missing sample: {sorted(ours_ids - hit)[:15]}")

    if stats is not None:
        keep = [c for c in ['player_id', 'player_display_name', 'position', 'team', 'recent_team', 'week',
                            'season_type', 'targets', 'target_share', 'air_yards_share', 'wopr',
                            'receiving_air_yards', 'carries'] if c in stats.columns]
        print("\n--- player stats, top Week 1 target share ---")
        s = stats[stats['week'] == stats['week'].min()] if 'week' in stats.columns else stats
        if 'target_share' in s.columns:
            print(s.sort_values('target_share', ascending=False)[keep].head(10).to_string(index=False))

    if pbp is not None:
        need = ['season_type', 'week', 'posteam', 'play_type', 'pass_attempt', 'rush_attempt',
                'yardline_100', 'receiver_player_id', 'receiver_player_name', 'rusher_player_id',
                'rusher_player_name', 'air_yards', 'down', 'goal_to_go', 'two_point_attempt',
                'qb_kneel', 'qb_spike', 'sack', 'aborted_play', 'penalty']
        print(f"\npbp columns present: {[c for c in need if c in pbp.columns]}")
        print(f"pbp columns MISSING: {[c for c in need if c not in pbp.columns]}")
        if {'receiver_player_id', 'yardline_100'} <= set(pbp.columns):
            p = pbp[(pbp.get('season_type', 'REG') == 'REG') & pbp['receiver_player_id'].notna()]
            rz = p[p['yardline_100'] <= 20].groupby(['receiver_player_id', 'receiver_player_name']).size()
            print("\nWeek 1 red-zone target leaders (yardline_100 <= 20):")
            print(rz.sort_values(ascending=False).head(10).to_string())
            adot = p.groupby('receiver_player_name')['air_yards'].agg(['count', 'mean'])
            print("\naDOT sample (receivers with 6+ targets):")
            print(adot[adot['count'] >= 6].sort_values('mean', ascending=False).head(10).round(1).to_string())


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--year', type=int, default=2026)
    main(ap.parse_args().year)
