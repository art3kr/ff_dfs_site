"""
scrapers/estimate_middling_ev.py
-------------------------------------
Estimates P(middle hits) and expected value for each opportunity from
find_middling_opportunities.py — using a hybrid approach:
  - MEAN: the market consensus (average of quoted lines across books
    in the market comparison data) — this is well-constrained, since
    it's averaging multiple books' independent estimates of "where's
    the 50/50 point".
  - VARIANCE: the player's own real historical performance in that
    stat (from hist_player_stats), NOT derived from the market data.

Why not get variance from the market too? Tested directly: every book
sets ITS OWN line to be close to a 50/50 proposition (that's how lines
are built), so cross-book line differences carry almost no information
about the underlying distribution's SPREAD — only weak signal about
slightly different central estimates. Regressing quoted lines against
their implied z-scores to back out a standard deviation produced a
nonsensical result (std of 200+ yards for an NFL passing-yards prop)
precisely because of this — confirmed before building this script,
not assumed.

Assumes the stat is roughly normally distributed — reasonable for
yardage-type counting stats, much weaker for low-count stats like
touchdowns or interceptions (a normal distribution poorly approximates
a mostly-0-or-1 outcome). Flagged in the output, not silently ignored.

Requires enough of the player's own game logs in hist_player_stats to
estimate a variance from — reports "insufficient data" rather than
guessing with a made-up default when there aren't enough games.

Usage:
    python scrapers/estimate_middling_ev.py \\
        --opportunities data/scoresandodds_market_comparison_middling_opportunities.csv \\
        --market-data data/scoresandodds_market_comparison.csv.gz
"""

import os
import sys
import argparse
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

CATEGORY_TO_STAT_FIELD = {
    'passing-yards':   'pass_yds',
    'rushing-yards':   'rush_yds',
    'receiving-yards': 'rec_yds',
    'receptions':      'rec',
    'passing-tds':     'pass_td',
    'completions':     'pass_cmp',
    'interceptions':   'pass_int',
    'pass-attempts':   'pass_att',
    'rush-attempts':   'rush_att',
    'passing-and-rushing-yards':   'pass_rush_yds',
    'rushing-and-receiving-yards': 'rush_rec_yds',
}

# Stats where a normal-distribution assumption is weak (low counts,
# mostly 0/1/2 outcomes) — flagged in output, not excluded outright,
# since an approximate estimate can still be directionally useful.
LOW_COUNT_STATS = {'pass_td', 'pass_int'}

MIN_GAMES_FOR_ESTIMATE = 6   # below this, variance estimate is too noisy to trust


def normalize_name(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


def get_player_history(app_module, name_normalized: str, stat_field: str, composite_expr: str = None):
    """Real historical values for this player+stat, most recent games."""
    ph = app_module._ph()
    sql_expr = composite_expr or stat_field
    rows = app_module.db_fetchall(f"""
        SELECT {sql_expr} AS val
        FROM hist_player_stats
        WHERE name_normalized = {ph}
        ORDER BY year DESC, week DESC
    """, (name_normalized,))
    return [r["val"] for r in rows if r["val"] is not None]


def main(opportunities_path: str, market_data_path: str):
    import app as app_module

    opps = pd.read_csv(opportunities_path)
    market = pd.read_csv(market_data_path)

    print(f"Loaded {len(opps)} opportunities, {len(market)} market rows")

    # Composite fields need their SQL sum expression, same as
    # _score_props_for_week() in app.py
    composite_exprs = {
        'pass_rush_yds': '(COALESCE(pass_yds, 0) + COALESCE(rush_yds, 0))',
        'rush_rec_yds':  '(COALESCE(rush_yds, 0) + COALESCE(rec_yds, 0))',
    }

    results = []
    with app_module.app.app_context():
        for _, opp in opps.iterrows():
            category = opp['category']
            stat_field = CATEGORY_TO_STAT_FIELD.get(category)
            if not stat_field:
                results.append({**opp.to_dict(), 'p_middle_hits': None, 'expected_value': None,
                               'note': f"no stat_field mapping for category '{category}'"})
                continue

            name_norm = normalize_name(opp['player_name'])

            # Market consensus mean: average of this player+category's
            # quoted lines across all books
            player_market_rows = market[
                (market['player_name'] == opp['player_name']) & (market['category'] == category)
            ]
            if player_market_rows.empty:
                results.append({**opp.to_dict(), 'p_middle_hits': None, 'expected_value': None,
                               'note': "no market data rows found for this player/category"})
                continue
            market_mean = player_market_rows['line'].mean()

            composite_expr = composite_exprs.get(stat_field)
            history = get_player_history(app_module, name_norm, stat_field, composite_expr)

            if len(history) < MIN_GAMES_FOR_ESTIMATE:
                results.append({**opp.to_dict(), 'p_middle_hits': None, 'expected_value': None,
                               'note': f"insufficient history ({len(history)} games, need {MIN_GAMES_FOR_ESTIMATE}+)"})
                continue

            historical_std = pd.Series(history).std()
            if historical_std == 0 or pd.isna(historical_std):
                results.append({**opp.to_dict(), 'p_middle_hits': None, 'expected_value': None,
                               'note': "zero/undefined historical variance — can't estimate"})
                continue

            p_middle = stats.norm.cdf(opp['high_line'], market_mean, historical_std) - \
                       stats.norm.cdf(opp['low_line'], market_mean, historical_std)
            ev = p_middle * opp['profit_if_middle_hits'] + (1 - p_middle) * opp['loss_if_miss']

            note = ""
            if stat_field in LOW_COUNT_STATS:
                note = "CAUTION: low-count stat, normal-distribution assumption is weak here"

            results.append({
                **opp.to_dict(),
                'market_mean': round(market_mean, 2),
                'historical_std': round(historical_std, 2),
                'historical_games': len(history),
                'p_middle_hits': round(p_middle, 4),
                'expected_value': round(ev, 2),
                'note': note,
            })

    result_df = pd.DataFrame(results)
    estimable = result_df.dropna(subset=['expected_value'])
    print(f"\nEstimated EV for {len(estimable)}/{len(opps)} opportunities "
          f"({len(opps) - len(estimable)} skipped — see 'note' column for why)")

    if not estimable.empty:
        estimable = estimable.sort_values('expected_value', ascending=False)
        print(f"\nTop opportunities by expected value:\n")
        display_cols = ['player_name', 'category', 'low_line', 'high_line', 'market_mean',
                        'historical_std', 'historical_games', 'p_middle_hits', 'expected_value', 'note']
        print(estimable[display_cols].head(20).to_string(index=False))

    out_path = opportunities_path.replace('.csv', '') + '_with_ev.csv'
    result_df.to_csv(out_path, index=False)
    print(f"\nFull results (including skipped rows and why) saved -> {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--opportunities", required=True)
    parser.add_argument("--market-data", required=True)
    args = parser.parse_args()
    main(args.opportunities, args.market_data)
