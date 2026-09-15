"""
scrapers/scrape_nflverse_usage.py
-----------------------------------
Per-player, per-week usage stats for the Usage tab, from nflverse's
free weekly releases on GitHub (CC-BY 4.0, credit "nflverse"). Makes no
requests to pro-football-reference.com.

Structure confirmed first with diagnose_nflverse_usage.py (2026-09-15,
real 2026 Week 1 files):
  - snap_counts_{year}.csv: pfr_player_id, week, offense_snaps,
    offense_pct (a 0-1 fraction). All 424 of our 2026 Week 1 PFR rows
    matched on pfr_id + week, and offense_pct agreed with the snap_pct
    we already store from PFR game logs.
  - stats_player_week_{year}.csv: player_id (gsis), week, season_type,
    targets, carries, receiving_air_yards, target_share,
    air_yards_share, wopr (shares are 0-1 fractions).
  - play_by_play_{year}.csv.gz: play_type, yardline_100, down,
    receiver_player_id, rusher_player_id, two_point_attempt, qb_kneel,
    season_type all present. Week 1 red-zone target leaders (Jeanty,
    McBride, Likely, St. Brown, 4 each) looked right.
  - players.csv: gsis_id -> pfr_id crosswalk, 423/424 of our 2026 PFR
    ids covered.

Red-zone style counts are derived here from play-by-play, so they may
differ slightly from other sites (penalty handling, laterals). Plays
wiped out by penalty (play_type 'no_play'), two-point tries and QB
kneels are excluded. Routes run aren't available: nflverse only
publishes route participation after the season ends.

Output: data/nflverse_usage_{year}.csv.gz, one row per player per
regular-season week (QB/RB/WR/TE/FB). Percent columns are 0-100.

Usage:
    python scrapers/scrape_nflverse_usage.py --year 2026
"""

import argparse
import io
import os
import re
import sys

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(__file__))
from team_mapping import normalize_team

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
POSITIONS = {'QB', 'RB', 'WR', 'TE', 'FB'}

OUT_COLUMNS = [
    'year', 'week', 'pfr_id', 'gsis_id', 'name', 'name_normalized', 'team', 'position',
    'offense_snaps', 'offense_pct',
    'targets', 'target_share', 'air_yards', 'air_yards_share', 'adot', 'wopr',
    'carries', 'receptions',
    'rz_targets', 'rz_carries', 'i10_targets', 'i10_carries', 'i5_targets', 'i5_carries',
    'third_down_targets',
]

_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv|v)$")


def normalize_name(name: str) -> str:
    """Same rule as app.normalize_name (the loader re-applies it anyway)."""
    key = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", str(name).lower().strip()))
    stripped = _SUFFIX_RE.sub("", key).strip()
    return stripped if " " in stripped else key


def fetch_csv(url: str) -> pd.DataFrame:
    print(f"  Fetching {url}")
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    compression = 'gzip' if url.endswith('.gz') else None
    return pd.read_csv(io.BytesIO(r.content), compression=compression, low_memory=False)


def pbp_counts(pbp: pd.DataFrame) -> pd.DataFrame:
    """Red-zone, inside-10/5 and third-down targets and carries per gsis id and week."""
    plays = pbp[(pbp['season_type'] == 'REG')
                & pbp['play_type'].isin(['pass', 'run'])
                & (pbp['two_point_attempt'].fillna(0) == 0)
                & (pbp['qb_kneel'].fillna(0) == 0)]

    targets = plays[plays['receiver_player_id'].notna()].rename(columns={'receiver_player_id': 'gsis_id'})
    carries = plays[(plays['play_type'] == 'run') & plays['rusher_player_id'].notna()] \
        .rename(columns={'rusher_player_id': 'gsis_id'})

    def tally(df, prefix):
        yl = df['yardline_100']
        return pd.DataFrame({
            'gsis_id': df['gsis_id'], 'week': df['week'],
            f'rz_{prefix}': (yl <= 20).astype(int),
            f'i10_{prefix}': (yl <= 10).astype(int),
            f'i5_{prefix}': (yl <= 5).astype(int),
        }).groupby(['gsis_id', 'week'], as_index=False).sum()

    t = tally(targets, 'targets')
    third = targets[targets['down'] == 3].groupby(['gsis_id', 'week']).size() \
        .rename('third_down_targets').reset_index()
    t = t.merge(third, on=['gsis_id', 'week'], how='left')
    c = tally(carries, 'carries')
    return t.merge(c, on=['gsis_id', 'week'], how='outer')


def main(year: int):
    print(f"nflverse usage for {year}")
    stats = fetch_csv(f"{RELEASES}/stats_player/stats_player_week_{year}.csv")
    snaps = fetch_csv(f"{RELEASES}/snap_counts/snap_counts_{year}.csv")
    pbp = fetch_csv(f"{RELEASES}/pbp/play_by_play_{year}.csv.gz")
    players = fetch_csv(f"{RELEASES}/players/players.csv")

    xwalk = players.dropna(subset=['gsis_id'])[['gsis_id', 'pfr_id', 'display_name', 'position']]

    stats = stats[(stats['season_type'] == 'REG') & stats['position'].isin(POSITIONS)]
    base = stats.rename(columns={'player_id': 'gsis_id', 'player_display_name': 'name',
                                 'receiving_air_yards': 'air_yards'})[
        ['gsis_id', 'week', 'name', 'team', 'position', 'targets', 'carries', 'receptions',
         'target_share', 'air_yards', 'air_yards_share', 'wopr']]
    base = base.merge(xwalk[['gsis_id', 'pfr_id']], on='gsis_id', how='left')

    # Snap counts label some running backs "HB" (all three CIN RBs in 2026
    # Week 1), which the position filter would otherwise drop.
    snaps['position'] = snaps['position'].replace({'HB': 'RB'})
    snaps = snaps[(snaps['game_type'] == 'REG') & snaps['position'].isin(POSITIONS)]
    snaps = snaps.rename(columns={'pfr_player_id': 'pfr_id', 'player': 'snap_name',
                                  'team': 'snap_team', 'position': 'snap_position'})[
        ['pfr_id', 'week', 'snap_name', 'snap_team', 'snap_position', 'offense_snaps', 'offense_pct']]

    # Outer join so a player with snaps but no box-score line (e.g. a
    # blocking TE with no targets) still gets a row.
    df = base.merge(snaps, on=['pfr_id', 'week'], how='outer')
    df['name'] = df['name'].fillna(df['snap_name'])
    df['team'] = df['team'].fillna(df['snap_team'])
    df['position'] = df['position'].fillna(df['snap_position'])
    missing_gsis = df['gsis_id'].isna() & df['pfr_id'].notna()
    if missing_gsis.any():
        by_pfr = xwalk.dropna(subset=['pfr_id']).drop_duplicates('pfr_id').set_index('pfr_id')['gsis_id']
        df.loc[missing_gsis, 'gsis_id'] = df.loc[missing_gsis, 'pfr_id'].map(by_pfr)

    df = df.merge(pbp_counts(pbp), on=['gsis_id', 'week'], how='left')

    count_cols = ['targets', 'carries', 'receptions', 'rz_targets', 'rz_carries', 'i10_targets', 'i10_carries',
                  'i5_targets', 'i5_carries', 'third_down_targets', 'offense_snaps']
    df[count_cols] = df[count_cols].fillna(0).astype(int)
    for col in ['offense_pct', 'target_share', 'air_yards_share']:
        df[col] = (df[col] * 100).round(1)
    df['wopr'] = df['wopr'].round(3)
    df['adot'] = (df['air_yards'] / df['targets'].where(df['targets'] > 0)).round(1)

    df['year'] = year
    # nflverse calls the Rams "LA", which normalize_team() doesn't know
    # (and bare "LA" is ambiguous anywhere else, so it isn't a global alias).
    # Older seasons also use "SD" for the Chargers, which normalize_team()
    # doesn't know either ("OAK" and "STL" it already maps).
    df['team'] = df['team'].replace({'LA': 'LAR', 'SD': 'LAC'})
    df['team'] = df['team'].map(lambda t: normalize_team(t) or None if pd.notna(t) else None)
    df['name_normalized'] = df['name'].map(normalize_name)
    df = df[df['name'].notna()]

    # A player can arrive as two rows when the id crosswalk only covers one
    # side: snaps keyed by pfr_id with no gsis_id, stats keyed by gsis_id
    # with no pfr_id (2026 Week 1: Cody White, LV). Same name, team and week
    # is the same player, so fold them together, keeping each side's values.
    keys = ['name_normalized', 'week', 'team']
    if df.duplicated(keys).any():
        num_cols = [c for c in df.columns if c not in keys and pd.api.types.is_numeric_dtype(df[c])]
        agg = {c: 'max' for c in num_cols}
        agg.update({c: 'first' for c in df.columns if c not in keys and c not in num_cols})
        df = df.groupby(keys, as_index=False, dropna=False).agg(agg)

    unmapped = sorted(df.loc[df['team'].isna(), 'name'].unique())
    if unmapped:
        print(f"  WARNING: {len(unmapped)} players with no team after normalize_team: {unmapped[:10]}")
    no_pfr = df['pfr_id'].isna().sum()
    if no_pfr:
        print(f"  NOTE: {no_pfr} rows have no pfr_id (joined on name instead downstream)")

    out = df[OUT_COLUMNS].sort_values(['week', 'team', 'position', 'name'])
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f'nflverse_usage_{year}.csv.gz')
    out.to_csv(path, index=False, compression='gzip')
    print(f"  {len(out):,} rows, weeks {sorted(out['week'].unique().tolist())} -> {path}")


def parse_years(s: str) -> list[int]:
    """'2012-2025', '2024,2025' or '2026'."""
    if '-' in s:
        start, end = s.split('-')
        return list(range(int(start), int(end) + 1))
    return [int(y) for y in s.split(',')]


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--year', type=int, default=None, help='One season, e.g. 2026')
    ap.add_argument('--years', default=None,
                    help='Several seasons, e.g. 2012-2025 (snap counts start in 2012)')
    args = ap.parse_args()
    years = parse_years(args.years) if args.years else [args.year or 2026]
    failed = []
    for y in years:
        try:
            main(y)
        except Exception as e:   # one missing season shouldn't stop a backfill
            print(f"  ERROR for {y}: {e}")
            failed.append(y)
    if failed:
        print(f"Failed seasons: {failed}")
        sys.exit(1)
