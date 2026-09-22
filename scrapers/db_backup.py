"""
scrapers/db_backup.py
--------------------------------------------------
Dump every table of a Postgres database to gzipped CSV, restore a dump
into another database, and verify the two match. Written during the
2026-09-22 move off Render's expiring free Postgres onto Neon, and kept
because it's also the weekly off-database backup.

    python scrapers/db_backup.py dump
    python scrapers/db_backup.py verify --from backups/neon_full_20260922_170201
    python scrapers/db_backup.py restore --from <dir> --url-env NEW_DATABASE_URL

`--url-env` names the .env key holding the connection string (default
DATABASE_URL), so a URL with a password never has to be typed on a
command line or land in shell history.

Why not pg_dump: it isn't installed here, and a CSV-per-table dump is
readable, diffable and restorable by this script alone.

**The trap this script exists to avoid:** `COPY ... FROM STDIN WITH CSV
HEADER` ignores the header names and maps columns BY POSITION. A table
that grew a column through `ALTER TABLE ADD COLUMN` has that column last
on the old database, but a fresh `CREATE TABLE` from app.py puts it where
the CREATE statement lists it. Three tables differed that way on
2026-09-22: `game_schedule` (home_away/kickoff), `game_odds`
(kickoff/updated_at) and `hist_player_usage` (receptions). Only
game_schedule raised an error — the other two would have loaded 94k rows
of silently wrong values into same-typed columns. So restore always names
the columns from the CSV header: `COPY "t" (a, b, c) FROM STDIN WITH CSV`.

Two more things a naive restore gets wrong, both handled here:
  - SERIAL sequences stay at 1 after a COPY, so the next insert collides
    with a restored id. Every serial column is reset with setval().
  - prop_picks.prop_bet_id references prop_bets(id), so prop_bets must be
    restored first (RESTORE_LAST).

Restoring into a non-empty database needs --truncate, which empties every
table in the dump first. --schema restores into an alternative schema,
which is how the round-trip test runs without touching live tables.
"""

import argparse
import gzip
import json
import os
import sys
from datetime import datetime

import psycopg2
from dotenv import dotenv_values

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
DEFAULT_OUT = os.path.join(REPO_ROOT, 'backups')

# prop_picks.prop_bet_id -> prop_bets(id): restore the referenced table first.
RESTORE_LAST = ['prop_picks']


def _url(url_env: str) -> str:
    env = dotenv_values(os.path.join(REPO_ROOT, '.env'))
    if url_env not in env or not env[url_env]:
        sys.exit(f"{url_env} is not set in .env")
    return env[url_env]


def _tables(cur, schema: str = 'public') -> list:
    cur.execute("""select table_name from information_schema.tables
                   where table_schema = %s and table_type = 'BASE TABLE'
                   order by table_name""", (schema,))
    names = [r[0] for r in cur.fetchall()]
    return [t for t in names if t not in RESTORE_LAST] + [t for t in names if t in RESTORE_LAST]


def cmd_dump(args):
    conn = psycopg2.connect(_url(args.url_env))
    conn.set_session(readonly=True)
    cur = conn.cursor()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = args.out or os.path.join(DEFAULT_OUT, f'{args.label}_{stamp}')
    os.makedirs(out, exist_ok=True)

    counts, schema = {}, {}
    for t in _tables(cur):
        with gzip.open(os.path.join(out, f'{t}.csv.gz'), 'wt', encoding='utf-8', newline='') as f:
            cur.copy_expert(f'COPY "{t}" TO STDOUT WITH CSV HEADER', f)
        cur.execute(f'select count(*) from "{t}"')
        counts[t] = cur.fetchone()[0]
        cur.execute("""select column_name, data_type from information_schema.columns
                       where table_schema='public' and table_name=%s order by ordinal_position""", (t,))
        schema[t] = [{'column': c, 'type': d} for c, d in cur.fetchall()]
        print(f'  {t:34s} {counts[t]:>8,}')
    json.dump({'taken_at': stamp, 'url_env': args.url_env, 'row_counts': counts, 'schema': schema},
              open(os.path.join(out, 'manifest.json'), 'w'), indent=1, default=str)
    conn.close()
    print(f'\n{len(counts)} tables, {sum(counts.values()):,} rows -> {out}')
    print('Copy this folder somewhere off this machine: it holds the only copy of '
          'lineups, prop_bets and prop_picks, which no scraper can rebuild.')


def _manifest(src: str) -> dict:
    with open(os.path.join(src, 'manifest.json')) as f:
        return json.load(f)


def cmd_restore(args):
    counts = _manifest(args.src)['row_counts']
    conn = psycopg2.connect(_url(args.url_env))
    cur = conn.cursor()
    if args.schema != 'public':
        cur.execute(f'set search_path to "{args.schema}"')
    order = [t for t in counts if t not in RESTORE_LAST] + [t for t in counts if t in RESTORE_LAST]

    existing = set(_tables(cur, args.schema))
    missing = [t for t in order if t not in existing]
    if missing:
        sys.exit(f"destination is missing tables: {', '.join(missing)}\n"
                 f"Create the schema first (any `flask` command against it runs app.py's "
                 f"_auto_init), then re-run.")

    nonempty = []
    for t in order:
        cur.execute(f'select count(*) from "{t}"')
        if cur.fetchone()[0]:
            nonempty.append(t)
    if nonempty and not args.truncate:
        sys.exit(f"{len(nonempty)} destination tables already hold rows "
                 f"({', '.join(nonempty[:4])}...). Re-run with --truncate to replace them.")
    if nonempty:
        cur.execute('TRUNCATE ' + ', '.join(f'"{t}"' for t in order) + ' RESTART IDENTITY CASCADE')
        conn.commit()
        print(f'truncated {len(nonempty)} non-empty tables')

    for t in order:
        with gzip.open(os.path.join(args.src, f'{t}.csv.gz'), 'rt', encoding='utf-8', newline='') as f:
            header = f.readline().strip()
            cols = ', '.join(f'"{c}"' for c in header.split(','))
            # BY NAME, not by position — see the module docstring.
            cur.copy_expert(f'COPY "{t}" ({cols}) FROM STDIN WITH CSV', f)
        print(f'  {t:34s} restored')
    conn.commit()

    fixed = []
    for t in order:
        cur.execute("""select column_name from information_schema.columns
                       where table_schema=%s and table_name=%s
                         and column_default like 'nextval%%'""", (args.schema, t))
        for (col,) in cur.fetchall():
            cur.execute(f"""select setval(pg_get_serial_sequence('{args.schema}.{t}', '{col}'),
                                          coalesce((select max({col}) from "{t}"), 1))""")
            fixed.append(f'{t}.{col}')
    conn.commit()
    print(f'sequences reset: {len(fixed)}')
    _report(cur, order, counts)
    conn.close()


def _report(cur, order, counts) -> int:
    bad = 0
    for t in order:
        cur.execute(f'select count(*) from "{t}"')
        n = cur.fetchone()[0]
        if n != counts[t]:
            bad += 1
            print(f'  MISMATCH {t:30s} dump {counts[t]:>8,}   database {n:>8,}')
    print(f'{len(order)} tables, {sum(counts.values()):,} rows in the dump, mismatches: {bad}')
    return bad


def cmd_verify(args):
    counts = _manifest(args.src)['row_counts']
    conn = psycopg2.connect(_url(args.url_env))
    conn.set_session(readonly=True)
    cur = conn.cursor()
    if args.schema != 'public':
        cur.execute(f'set search_path to "{args.schema}"')
    sys.exit(1 if _report(cur, list(counts), counts) else 0)


def main():
    # On a parent parser so `db_backup.py restore --url-env X` works as well as
    # `db_backup.py --url-env X restore` — argparse otherwise only accepts the
    # second form, which is not how anyone types it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--url-env', default='DATABASE_URL',
                        help='.env key holding the connection string (default DATABASE_URL)')
    common.add_argument('--schema', default='public')

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[3], parents=[common])
    sub = parser.add_subparsers(dest='command', required=True)

    d = sub.add_parser('dump', parents=[common], help='Write every table to gzipped CSV.')
    d.add_argument('--out', default=None, help='Target folder (default backups/<label>_<timestamp>)')
    d.add_argument('--label', default='db', help='Folder name prefix (default "db")')

    r = sub.add_parser('restore', parents=[common], help='Load a dump into a database.')
    r.add_argument('--from', dest='src', required=True)
    r.add_argument('--truncate', action='store_true', help='Empty destination tables first.')

    v = sub.add_parser('verify', parents=[common], help='Compare a database against a dump manifest.')
    v.add_argument('--from', dest='src', required=True)

    args = parser.parse_args()
    {'dump': cmd_dump, 'restore': cmd_restore, 'verify': cmd_verify}[args.command](args)


if __name__ == '__main__':
    main()
