import os
import re
import csv

DATA_DIR     = os.path.join(os.path.dirname(__file__), '..', 'data')
MAPPING_FILE = os.path.join(DATA_DIR, 'mapping_table', 'team_name_mapping_table.csv')

EXTRA_ALIASES = {
    'jac': 'jax',
    'washington': 'was',
    # Historical franchise name changes within the 2014-2025 window —
    # confirmed real gap: "Washington Redskins" (2015 fantasy-points-
    # against page) failed to normalize since our mapping CSV only has
    # the current franchise name. Same issue would hit any other year
    # touching these renamed/relocated teams.
    'washington redskins': 'was',
    'washington football team': 'was',
    'oakland raiders': 'lvr',
    'san diego chargers': 'lac',
    'st. louis rams': 'lar',
    'st louis rams': 'lar',
}
_lookup = {}

def _build_lookup():
    if _lookup:
        return
    with open(MAPPING_FILE, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            espn_team = row['espn_team'].strip()
            espn_abb  = row['espn_abbreviation'].strip()
            pfr_team  = row['pfr_team'].strip()
            pfr_abb   = row['pfr_abbreviation'].strip().lower()
            keys = [espn_abb, pfr_abb, espn_team, pfr_team,
                    espn_team.split()[-1], pfr_team.split()[-1]]
            for k in keys:
                _lookup[k.strip().lower()] = pfr_abb
    _lookup.update(EXTRA_ALIASES)

def normalize_team(raw: str) -> str:
    _build_lookup()
    if not raw:
        return ''
    text = raw.strip()
    text = re.sub(r'\s*D/?ST\s*$', '', text, flags=re.IGNORECASE).strip()
    text = text.lower()
    if text in _lookup:
        return _lookup[text]
    last_word = text.split()[-1] if text else ''
    if last_word in _lookup:
        return _lookup[last_word]
    return ''
