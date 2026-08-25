"""
scrapers/combine_dk_salaries.py
--------------------------------
Combines DK salary data from two sources into one standardized file:

  SOURCE 1 — DFF (Daily Fantasy Fuel) cheatsheet exports
    Filename pattern: DFF_NFL_cheatsheet_YYYY-MM-DD.csv
    Year extracted from filename date. Week is a column in the file.
    DST format: first_name='Seahawks', last_name=NaN, position='DST'

  SOURCE 2 — RotoWire exports (the API dump used in FF_2025)
    Filename pattern: Week9_salaries_rotowire.csv  (no year in filename)
    Week is a column in the file. Year must come from a subfolder
    named by year (e.g. data/dk_salary_exports/2025/Week9_salaries_rotowire.csv)
    OR specified via --year flag.
    DST format: first_name='Houston', last_name='Texans', position='D'

Output: data/dk_salaries_2022_2025.csv.gz
  Columns:
    week, year, name, name_normalized, position,
    team, opponent, dk_salary, dk_pts_scored,
    projected_pts, ownership_pct, source

Usage:
    # Put files in data/dk_salary_exports/ (can use subfolders by year)
    python scrapers/combine_dk_salaries.py

    # Specify folder explicitly
    python scrapers/combine_dk_salaries.py --input-dir path/to/salary/files

    # If RotoWire files have no year in filename/folder, specify it
    python scrapers/combine_dk_salaries.py --fallback-year 2025

    # Check what's in the output file
    python scrapers/combine_dk_salaries.py --check
"""

import os
import re
import glob
import argparse
import pandas as pd
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
DEFAULT_IN  = os.path.join(DATA_DIR, 'dk_salary_exports')
OUTPUT_FILE = os.path.join(DATA_DIR, 'dk_salaries_2022_2025.csv.gz')

OUTPUT_COLUMNS = [
    'week', 'year', 'name', 'name_normalized', 'position',
    'team', 'opponent', 'dk_salary', 'dk_pts_scored',
    'projected_pts', 'ownership_pct', 'source',
]

# Canonical DST team name → abbreviation map (used to normalise RotoWire DST names)
DST_NAME_TO_ABB = {
    'arizona cardinals': 'ARI', 'atlanta falcons': 'ATL', 'baltimore ravens': 'BAL',
    'buffalo bills': 'BUF', 'carolina panthers': 'CAR', 'chicago bears': 'CHI',
    'cincinnati bengals': 'CIN', 'cleveland browns': 'CLE', 'dallas cowboys': 'DAL',
    'denver broncos': 'DEN', 'detroit lions': 'DET', 'green bay packers': 'GB',
    'houston texans': 'HOU', 'indianapolis colts': 'IND', 'jacksonville jaguars': 'JAX',
    'kansas city chiefs': 'KC', 'las vegas raiders': 'LV', 'los angeles chargers': 'LAC',
    'los angeles rams': 'LAR', 'miami dolphins': 'MIA', 'minnesota vikings': 'MIN',
    'new england patriots': 'NE', 'new orleans saints': 'NO', 'new york giants': 'NYG',
    'new york jets': 'NYJ', 'philadelphia eagles': 'PHI', 'pittsburgh steelers': 'PIT',
    'san francisco 49ers': 'SF', 'seattle seahawks': 'SEA', 'tampa bay buccaneers': 'TB',
    'tennessee titans': 'TEN', 'washington commanders': 'WAS',
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(name).lower().strip())

def _float(s) -> float | None:
    try:
        v = float(str(s).replace('$','').replace(',','').strip())
        return None if pd.isna(v) else v
    except:
        return None

def _int(s) -> int:
    try:
        return int(float(str(s).replace('$','').replace(',','').strip()))
    except:
        return 0

def detect_format(df: pd.DataFrame) -> str:
    """Return 'dff', 'rotowire', or 'unknown'."""
    cols = set(df.columns)
    if 'ppg_projection' in cols and 'game_date' in cols:
        return 'dff'
    if 'proj_points' in cols and 'proj_rotowire' in cols:
        return 'rotowire'
    return 'unknown'

def year_from_path(path: str, fallback: int | None) -> int | None:
    """
    Try to extract a year from the file path.
    Checks: parent folder name, filename date (YYYY-MM-DD), filename year (YYYY).
    Falls back to `fallback` if nothing found.
    """
    # Parent folder named as a year e.g. .../2025/Week9_salaries_rotowire.csv
    parent = os.path.basename(os.path.dirname(path))
    if re.match(r'^20\d{2}$', parent):
        return int(parent)

    # Date in filename e.g. DFF_NFL_cheatsheet_2025-12-11.csv
    m = re.search(r'(20\d{2})-\d{2}-\d{2}', os.path.basename(path))
    if m:
        return int(m.group(1))

    # Bare year in filename e.g. salaries_2025_wk1.csv
    m = re.search(r'(20\d{2})', os.path.basename(path))
    if m:
        return int(m.group(1))

    return fallback

# ---------------------------------------------------------------------------
# Format-specific parsers
# ---------------------------------------------------------------------------

def parse_dff(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """
    Parse a DFF (Daily Fantasy Fuel) cheatsheet CSV.

    Relevant columns:
      first_name, last_name, position, week, team, opp,
      salary, ppg_projection, ppg_actual
    DST rows: first_name = team nickname (e.g. 'Seahawks'), last_name = NaN
    """
    # Filter to Thu-Mon slate only (skip Sunday-only, TNF-only slates if present)
    if 'slate' in df.columns:
        df = df[df['slate'].str.contains('Thu', case=False, na=False)]

    rows = []
    for _, row in df.iterrows():
        pos = str(row.get('position', '') or '').strip().upper()

        # Build name
        fn = str(row.get('first_name', '') or '').strip()
        ln = str(row.get('last_name',  '') or '').strip()

        if pos == 'DST':
            # DFF stores DST as e.g. first_name='Seahawks', last_name=NaN
            # Use team abbreviation as name for consistency with RotoGuru format
            team_abb = str(row.get('team', '') or '').strip().upper()
            name = f"{fn} {ln}".strip() if ln and ln != 'nan' else fn
            if not name:
                name = team_abb
        else:
            if not fn and not ln:
                continue
            name = f"{fn} {ln}".strip()

        if not name:
            continue

        salary = _int(row.get('salary', 0))
        if salary == 0:
            continue

        team     = str(row.get('team', '') or '').strip().upper()
        opponent = str(row.get('opp',  '') or '').strip().upper()
        week     = _int(row.get('week', 0))
        if week == 0:
            continue

        proj = _float(row.get('ppg_projection'))
        actual = _float(row.get('ppg_actual'))

        rows.append({
            'week':            week,
            'year':            year,
            'name':            name,
            'name_normalized': normalize_name(name),
            'position':        pos,
            'team':            team.lower(),
            'opponent':        opponent.lower(),
            'dk_salary':       salary,
            'dk_pts_scored':   actual,       # populated after games, else None
            'projected_pts':   proj,
            'ownership_pct':   None,         # DFF doesn't include ownership
            'source':          'dff',
        })

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def parse_rotowire(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """
    Parse a RotoWire API export CSV.

    Relevant columns:
      first_name, last_name, position, team, opponent,
      salary, proj_points, ownership, week
    DST rows: first_name='Houston', last_name='Texans', position='D'
    """
    rows = []
    for _, row in df.iterrows():
        pos = str(row.get('position', '') or '').strip().upper()
        # RotoWire uses 'D' for DST
        if pos == 'D':
            pos = 'DST'

        fn = str(row.get('first_name', '') or '').strip()
        ln = str(row.get('last_name',  '') or '').strip()

        if pos == 'DST':
            # RotoWire stores DST as first_name='Houston', last_name='Texans'
            # Normalise to team abbreviation using the team column
            team_abb = str(row.get('team', '') or '').strip().upper()
            full_name = f"{fn} {ln}".strip().lower()
            name = DST_NAME_TO_ABB.get(full_name, team_abb) + ' DST'
        else:
            if not fn and not ln:
                # Try the 'player' column as fallback
                name = str(row.get('player', '') or '').strip()
            else:
                name = f"{fn} {ln}".strip()

        if not name:
            continue

        salary = _int(row.get('salary', 0))
        if salary == 0:
            continue

        team     = str(row.get('team',     '') or '').strip().upper()
        opponent = str(row.get('opponent', '') or '').strip().upper()
        week     = _int(row.get('week', 0))
        if week == 0:
            continue

        proj      = _float(row.get('proj_points'))
        ownership = _float(row.get('ownership'))

        rows.append({
            'week':            week,
            'year':            year,
            'name':            name,
            'name_normalized': normalize_name(name),
            'position':        pos,
            'team':            team.lower(),
            'opponent':        opponent.lower(),
            'dk_salary':       salary,
            'dk_pts_scored':   None,         # actual pts come from PFR
            'projected_pts':   proj,
            'ownership_pct':   ownership,
            'source':          'rotowire',
        })

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


# ---------------------------------------------------------------------------
# Process one file
# ---------------------------------------------------------------------------

def process_file(path: str, fallback_year: int | None) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(path, encoding='utf-8', on_bad_lines='skip')
    except UnicodeDecodeError:
        try:
            df = pd.read_csv(path, encoding='latin-1', on_bad_lines='skip')
        except Exception as e:
            print(f"    ERROR reading {path}: {e}")
            return None
    except Exception as e:
        print(f"    ERROR reading {path}: {e}")
        return None

    if df.empty:
        return None

    fmt = detect_format(df)
    if fmt == 'unknown':
        print(f"    SKIP {os.path.basename(path)}: unrecognised format")
        print(f"    Columns: {list(df.columns)[:8]}...")
        return None

    year = year_from_path(path, fallback_year)
    if year is None:
        print(f"    SKIP {os.path.basename(path)}: can't determine year")
        print(f"    Put file in a subfolder named by year, e.g. dk_salary_exports/2025/")
        print(f"    Or run with --fallback-year 2025")
        return None

    if fmt == 'dff':
        return parse_dff(df, year)
    elif fmt == 'rotowire':
        return parse_rotowire(df, year)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def _save(df: pd.DataFrame):
    df = df.drop_duplicates(subset=['year', 'week', 'name', 'position'])
    df = df.sort_values(['year', 'week', 'position', 'dk_salary'],
                        ascending=[True, True, True, False])
    df.to_csv(OUTPUT_FILE, index=False, compression='gzip')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(input_dir: str, fallback_year: int | None):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(input_dir, exist_ok=True)

    csv_files = sorted(
        glob.glob(os.path.join(input_dir, '*.csv')) +
        glob.glob(os.path.join(input_dir, '**', '*.csv'), recursive=True)
    )

    if not csv_files:
        print(f"No CSV files found in: {input_dir}")
        print()
        print("Expected folder structure:")
        print("  data/dk_salary_exports/")
        print("    2024/")
        print("      Week1_salaries_rotowire.csv")
        print("      DFF_NFL_cheatsheet_2024-09-12.csv")
        print("    2025/")
        print("      Week9_salaries_rotowire.csv")
        print("      DFF_NFL_cheatsheet_2025-12-11.csv")
        return

    print(f"Found {len(csv_files)} CSV files\n")

    all_dfs = []
    for path in csv_files:
        basename = os.path.basename(path)
        print(f"  {basename}", end='', flush=True)
        result = process_file(path, fallback_year)
        if result is not None and not result.empty:
            week = result['week'].iloc[0]
            year = result['year'].iloc[0]
            fmt  = result['source'].iloc[0]
            print(f" → {year} W{week:02d} | {len(result)} players | source={fmt}")
            all_dfs.append(result)
        else:
            print()  # newline after skip message

    if not all_dfs:
        print("\nNo data parsed. Check filenames and formats.")
        return

    combined = pd.concat(all_dfs, ignore_index=True)
    _save(combined)

    final = pd.read_csv(OUTPUT_FILE)
    print(f"\nSaved {len(final):,} rows → {OUTPUT_FILE}")
    print("\nCoverage:")
    for (year, week), grp in final.groupby(['year', 'week']):
        sources = grp['source'].unique()
        print(f"  {int(year)} W{int(week):02d}: {len(grp)} players  [{', '.join(sources)}]")


def check():
    if not os.path.exists(OUTPUT_FILE):
        print(f"Output file not found: {OUTPUT_FILE}")
        print("Run: python scrapers/combine_dk_salaries.py")
        return
    df = pd.read_csv(OUTPUT_FILE)
    print(f"File: {OUTPUT_FILE}")
    print(f"Total rows: {len(df):,}")
    print(f"Years: {sorted(df['year'].unique())}")
    print(f"Sources: {df['source'].value_counts().to_dict()}")
    print(f"projected_pts coverage: {df['projected_pts'].notna().sum():,} / {len(df):,}")
    print(f"ownership_pct coverage: {df['ownership_pct'].notna().sum():,} / {len(df):,}")
    print(f"dk_pts_scored coverage: {df['dk_pts_scored'].notna().sum():,} / {len(df):,}")
    print()
    print("Rows per year/week:")
    for (year, week), grp in df.groupby(['year', 'week']):
        print(f"  {int(year)} W{int(week):02d}: {len(grp)} players")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Combine DFF and RotoWire salary exports into one file'
    )
    parser.add_argument('--input-dir', default=DEFAULT_IN,
                        help=f'Folder containing salary CSVs (default: data/dk_salary_exports/)')
    parser.add_argument('--fallback-year', type=int, default=None,
                        help='Year to use when it cannot be determined from the filename or folder')
    parser.add_argument('--check', action='store_true',
                        help='Print a summary of the existing output file and exit')
    args = parser.parse_args()

    if args.check:
        check()
    else:
        main(args.input_dir, args.fallback_year)
