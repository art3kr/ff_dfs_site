"""
scrapers/pull_live_data.py
--------------------------------------------------
Rebuilds this machine's `data/` working set from what the cloud already
has, so you don't have to re-scrape after the GitHub Actions job
(.github/workflows/refresh-live-data.yml) has been refreshing things.

Three sources, in order of fidelity:

  1. **The workflow's artifact** (`gh run download`): the exact files the
     scrapers wrote on the runner. This is the one you want, and it's the
     default when the GitHub CLI is installed and logged in.
  2. **data/odds_archive/** (arrives by `git pull`): the per-book market
     comparison snapshots, which are never loaded into any table. The
     newest one for the current week is copied to
     data/scoresandodds_market_comparison.csv.gz.
  3. **The database** (`--from-db`): depth charts, injuries, game odds and
     the props market read back out of the live tables. Written with a
     `_from_db` suffix because the tables don't store every column the
     scrapers write (game_odds has no event_id, depth_charts renames a
     couple of fields), so these are for looking at, not for feeding the
     research scripts.

Not covered: PFR stats, salaries, usage, weather history. Those come from
the weekly local run.

Usage:
    python scrapers/pull_live_data.py              # artifact + snapshot
    python scrapers/pull_live_data.py --from-db    # also export the tables
    python scrapers/pull_live_data.py --skip-artifact
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from bet_tracker import ARCHIVE_DIR, current_week

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
WORKFLOW = 'refresh-live-data.yml'
ARTIFACT = 'scraped-data'

# Tables the scheduled job refreshes, for the --from-db fallback.
LIVE_TABLES = ['depth_charts', 'player_injuries', 'game_odds', 'scoresandodds_props']


def pull_artifact() -> bool:
    """Download the newest successful run's scraped files over data/.
    Returns False (with a reason printed) if gh isn't usable."""
    try:
        subprocess.run(['gh', 'auth', 'status'], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("  gh CLI not installed or not logged in; skipping the artifact")
        return False

    runs = subprocess.run(
        ['gh', 'run', 'list', '--workflow', WORKFLOW, '--status', 'success',
         '--limit', '1', '--json', 'databaseId,createdAt'],
        capture_output=True, text=True, cwd=REPO_ROOT)
    if runs.returncode != 0 or runs.stdout.strip() in ('', '[]'):
        print(f"  no successful {WORKFLOW} runs yet "
              f"({runs.stderr.strip().splitlines()[0] if runs.stderr.strip() else 'none found'})")
        return False

    import json
    run = json.loads(runs.stdout)[0]
    with tempfile.TemporaryDirectory() as tmp:
        got = subprocess.run(
            ['gh', 'run', 'download', str(run['databaseId']), '--name', ARTIFACT, '--dir', tmp],
            capture_output=True, text=True, cwd=REPO_ROOT)
        if got.returncode != 0:
            print(f"  artifact download failed: {got.stderr.strip().splitlines()[0]}")
            return False
        files = glob.glob(os.path.join(tmp, '**', '*.csv.gz'), recursive=True)
        for src in files:
            dest = os.path.join(DATA_DIR, os.path.basename(src))
            shutil.copy2(src, dest)
            print(f"  {os.path.basename(src):42s} {len(pd.read_csv(dest)):>6,} rows")
    print(f"  from run {run['databaseId']} ({run['createdAt']})")
    return True


def pull_market_snapshot() -> None:
    year, week = current_week()
    folder = os.path.join(ARCHIVE_DIR, f'{year}_wk{week:02d}')
    snaps = sorted(glob.glob(os.path.join(folder, 'market_comparison_*.csv.gz')))
    if not snaps:
        print(f"  no archived snapshot for {year} week {week} (run `git pull` first)")
        return
    newest = snaps[-1]
    dest = os.path.join(DATA_DIR, 'scoresandodds_market_comparison.csv.gz')
    shutil.copy2(newest, dest)
    print(f"  {os.path.basename(newest):42s} {len(pd.read_csv(dest)):>6,} rows -> "
          f"data/scoresandodds_market_comparison.csv.gz")


def pull_tables(url_env: str) -> None:
    import psycopg2
    from dotenv import dotenv_values
    url = dotenv_values(os.path.join(REPO_ROOT, '.env')).get(url_env)
    if not url:
        sys.exit(f"{url_env} is not set in .env")
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True)
    for table in LIVE_TABLES:
        try:
            df = pd.read_sql(f'select * from "{table}"', conn)
        except Exception as e:
            print(f"  {table:24s} skipped ({str(e).splitlines()[0]})")
            conn.rollback()
            continue
        df = df.drop(columns=[c for c in ('id',) if c in df.columns])
        path = os.path.join(DATA_DIR, f'{table}_from_db.csv.gz')
        df.to_csv(path, index=False, compression='gzip')
        print(f"  {table:24s} {len(df):>6,} rows -> data/{table}_from_db.csv.gz")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--skip-artifact', action='store_true')
    ap.add_argument('--skip-market', action='store_true')
    ap.add_argument('--from-db', action='store_true',
                    help='Also export the live tables (written with a _from_db suffix).')
    ap.add_argument('--url-env', default='DATABASE_URL')
    args = ap.parse_args()

    if not args.skip_artifact:
        print("From the latest workflow artifact:")
        pull_artifact()
    if not args.skip_market:
        print("From data/odds_archive (git):")
        pull_market_snapshot()
    if args.from_db:
        print("From the database:")
        pull_tables(args.url_env)
    print("\nDone.")


if __name__ == '__main__':
    main()
