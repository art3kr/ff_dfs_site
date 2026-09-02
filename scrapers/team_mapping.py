"""
scrapers/team_mapping.py
--------------------------
Shared team-name/abbreviation normalizer, built from
data/mapping_table/team_name_mapping_table.csv (ESPN <-> PFR abbreviation differences,
e.g. KC/KAN, NE/NWE, SF/SFO, WSH/WAS, LV/LVR, GB/GNB, NO/NOR, TB/TAM).

This exists because DST team names show up in wildly different formats
across our data sources:
  - hist_player_stats / hist_dfs_salaries use PFR-style lowercase
    abbreviations ('kan', 'nwe', 'sfo', ...)
  - FantasyPros shows ESPN-style abbreviations in parens ('KC', 'NE', 'SF', ...)
  - RotoWire-sourced salary rows store DST as "HOU DST" (already an
    abbreviation, but which convention varies)
  - DFF-sourced salary rows store DST as just the nickname ("Seahawks",
    no city, no abbreviation at all)

normalize_team(raw) tries, in order: exact PFR abbreviation match, exact
ESPN abbreviation match, full team name match (either convention), and
finally a nickname-only match (last word of the team name) — so any of
these formats resolves to the same canonical PFR abbreviation
('kan', 'sea', etc.) used throughout hist_player_stats / hist_dfs_salaries.

Usage:
    from team_mapping import normalize_team
    normalize_team("KC")              -> "kan"
    normalize_team("Kansas City Chiefs") -> "kan"
    normalize_team("Chiefs")          -> "kan"
    normalize_team("KAN")             -> "kan"
    normalize_team("HOU DST")         -> "hou"
"""

import os
import re
import csv

DATA_DIR     = os.path.join(os.path.dirname(__file__), '..', 'data')
MAPPING_FILE = os.path.join(DATA_DIR, 'mapping_table', 'team_name_mapping_table.csv')

# Known site-specific abbreviation quirks that don't follow either the
# ESPN or PFR convention in the mapping file — confirmed live:
# FantasyPros uses 'JAC' for Jacksonville, while both ESPN and PFR use
# 'JAX'. nflweather.com uses the bare city name 'washington' as its team
# slug, which our nickname-derivation logic (last word of the full team
# name) can never produce on its own, since Washington's team name ends
# in "Commanders" (or historically "Football Team"/"Redskins"), not
# "Washington". Add more entries here as other sources turn up similar
# quirks (a WARNING is printed by any scraper using this module whenever
# a slug fails to resolve, so new ones are easy to spot and add).
EXTRA_ALIASES = {
    'jac': 'jax',
    'washington': 'was',
}

_lookup = {}   # any known key (lowercased) -> canonical pfr_abbreviation (lowercase)


def _build_lookup():
    if _lookup:
        return   # already built
    if not os.path.exists(MAPPING_FILE):
        raise FileNotFoundError(
            f"Team mapping file not found: {MAPPING_FILE}\n"
            f"This is required for DST name matching."
        )

    with open(MAPPING_FILE, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            espn_team = row['espn_team'].strip()
            espn_abb  = row['espn_abbreviation'].strip()
            pfr_team  = row['pfr_team'].strip()
            pfr_abb   = row['pfr_abbreviation'].strip().lower()

            keys = [
                espn_abb, pfr_abb, espn_team, pfr_team,
                espn_team.split()[-1],   # nickname, e.g. "Chiefs"
                pfr_team.split()[-1],
            ]
            for k in keys:
                _lookup[k.strip().lower()] = pfr_abb

    # Site-specific quirks (e.g. FantasyPros' 'JAC' vs the standard 'JAX')
    # layered on top — these map directly to a canonical PFR abbreviation,
    # not to another key, so no further lookup chaining needed.
    _lookup.update(EXTRA_ALIASES)


def normalize_team(raw: str) -> str:
    """
    Resolve any team name/abbreviation format to the canonical PFR
    abbreviation (lowercase), matching hist_player_stats.team /
    hist_dfs_salaries.team convention. Returns '' if no match found.
    """
    _build_lookup()

    if not raw:
        return ''

    text = raw.strip()
    # Strip a trailing " DST" or " D/ST" suffix if present
    text = re.sub(r'\s*D/?ST\s*$', '', text, flags=re.IGNORECASE).strip()
    text = text.lower()

    if text in _lookup:
        return _lookup[text]

    # Last resort: try just the final word (handles "Seattle Seahawks",
    # "LA Rams", etc. that weren't an exact full-name match due to
    # abbreviated city names)
    last_word = text.split()[-1] if text else ''
    if last_word in _lookup:
        return _lookup[last_word]

    return ''
