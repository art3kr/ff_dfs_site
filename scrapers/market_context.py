"""
scrapers/market_context.py
--------------------------------------------------
Context the betting finders need but the odds themselves don't carry:
who's hurt, and which teammates actually matter.

Why this exists (Week 2, 2026-09-21): Puka Nacua was ruled out for MNF.
The other books moved his teammates within hours (Davante Adams anytime
TD +115 -> -120, receptions line 3.5 -> 5.5, receiving yards 44.5 ->
63.5), but find_ev_bets.py treated the books that hadn't caught up yet as
"fair" and flagged DraftKings' already-correct Adams Under as a 14% edge.
A finder can't tell stale consensus from real edge on price alone; a key
teammate on the injury report is the tell.

  injury_status()   (player key, team) -> status/position, Ourlads first,
                    Draftedge gap-fill (same priority as app.py's loaders).
                    Keyed WITH the team because names collide across the
                    league: on 2026-09-22 Draftedge listed a Cleveland
                    linebacker named Justin Jefferson as out, which by name
                    alone flagged the Vikings receiver (100% of snaps in
                    Week 2, props live at every book) as injured.
  key_players()     players whose absence moves teammates' lines: recent
                    target share >= KEY_TARGET_SHARE, carry share >=
                    KEY_CARRY_SHARE, or a starting QB.
  news_for()        human-readable notes for one prop: the player's own
                    status, and any key teammate who's out or doubtful.

The injury files are only as fresh as the last scrape. Nacua's injury
surfaced Friday; Thursday's files didn't have it. Re-run
scrape_ourlads_depth_charts.py and scrape_draftedge_injuries.py before a
Sunday or Monday finder run, or use find_stale_lines.py, which reads the
market's own movement and needs no injury data at all.
"""

import glob
import os
import re

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

OUT_STATUSES = {'out', 'doubtful', 'ir', 'inactive', 'suspended', 'pup'}
WATCH_STATUSES = OUT_STATUSES | {'questionable'}

KEY_TARGET_SHARE = 15.0   # % of team targets, recent games
KEY_CARRY_SHARE = 30.0    # % of team carries, recent games
RECENT_GAMES = 6

_NAME_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv|v)$")


def normalize_name(name: str) -> str:
    # Same rule as app.normalize_name(); not imported, since importing app.py
    # connects to whatever DATABASE_URL .env points at.
    key = re.sub(r"[^a-z0-9 ]", "", str(name).lower().strip())
    key = re.sub(r"\s+", " ", key)
    stripped = _NAME_SUFFIX_RE.sub("", key).strip()
    return stripped if " " in stripped else key


def injury_status(data_dir: str = DATA_DIR) -> dict:
    """(player key, team) -> {'status', 'position', 'name'}. Ourlads wins over
    Draftedge, matching replace_player_injuries()/fill_gap_player_injuries()."""
    out = {}
    for fname in ('draftedge_injuries.csv.gz', 'ourlads_injuries.csv.gz'):
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path)
        for r in df.itertuples():
            out[(normalize_name(r.player_name), str(r.team).lower())] = {
                'status': str(r.status).lower(), 'position': r.position,
                'name': r.player_name}
    return out


def key_players(data_dir: str = DATA_DIR) -> pd.DataFrame:
    """One row per player on his most recent team: recent target and carry
    shares, and whether he's a key player by the thresholds above."""
    files = sorted(glob.glob(os.path.join(data_dir, 'nflverse_usage_*.csv.gz')))[-2:]
    usage = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    usage = usage.sort_values(['year', 'week'])
    usage['team_carries'] = usage.groupby(['team', 'year', 'week'])['carries'].transform('sum')
    usage['carry_share'] = 100 * usage['carries'] / usage['team_carries'].where(usage['team_carries'] > 0)
    usage['key'] = usage['name_normalized'].map(normalize_name)
    last_team = usage.groupby('key')['team'].last()
    recent = usage.groupby('key').tail(RECENT_GAMES)
    recent = recent[recent['team'] == recent['key'].map(last_team)]
    agg = recent.groupby('key').agg(
        name=('name', 'last'), team=('team', 'last'), position=('position', 'last'),
        target_share=('target_share', 'mean'), carry_share=('carry_share', 'mean'),
        last_snap=('offense_pct', 'last')).reset_index()
    agg['is_key'] = ((agg['target_share'] >= KEY_TARGET_SHARE)
                     | (agg['carry_share'] >= KEY_CARRY_SHARE)
                     | ((agg['position'] == 'QB') & (agg['last_snap'] >= 80)))
    return agg


class NewsContext:
    """Built once per run; news_for() is called per prop."""

    def __init__(self, data_dir: str = DATA_DIR):
        self.injuries = injury_status(data_dir)
        players = key_players(data_dir)
        self.key_by_team = {}
        for r in players[players['is_key']].itertuples():
            status = (self.injuries.get((r.key, str(r.team).lower())) or {}).get('status')
            if status in OUT_STATUSES:
                self.key_by_team.setdefault(r.team, []).append((r.name, r.key, status))

    def news_for(self, player_name: str, team: str) -> list:
        key = normalize_name(player_name)
        notes = []
        # (name, team), never name alone: see injury_status()'s docstring for
        # the Justin Jefferson collision this avoids.
        own = self.injuries.get((key, str(team).lower()))
        if own and own['status'] in WATCH_STATUSES:
            notes.append(f"{own['status']} himself")
        for name, teammate_key, status in self.key_by_team.get(team, []):
            if teammate_key != key:
                notes.append(f"teammate {name} {status}")
        return notes
