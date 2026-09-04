# ScoresAndOdds Market Analysis Workflow

Four steps, run in order. Each one's output feeds the next.

## 1. Scrape all prop categories

```cmd
python scrapers\scrape_scoresandodds_props.py --all --combine
```

Pulls every convertible category (passing yards, rushing yards, receptions, touchdowns, etc.) into one file: `data/scoresandodds_props_all.csv.gz`. Each row is one player's "best odds" line, plus an `event_id` used in the next step.

## 2. Scrape every book's own line/odds per prop

```cmd
python scrapers\scrape_scoresandodds_market_comparison.py
```

For every prop from Step 1, fetches the full multi-book comparison (not just the "best" one) into `data/scoresandodds_market_comparison.csv.gz` — one row per (player, category, book).

- **Saves incrementally** — a crash or cancel partway through only loses the one in-flight row, not the whole run.
- **Default behavior does a full refresh** every time (lines change over time, so you generally want current data).
- If a run gets interrupted and you want to pick up where it left off instead of refreshing everything: add `--resume`.

This step takes a while (one request per player/category pair) — budget accordingly.

## 3. Find middling opportunities

```cmd
python scrapers\find_middling_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-width 2 --total-stake 100
```

For each player/category, finds the book with the *lowest* line (bet Over there) and the book with the *highest* line (bet Under there) — the gap between them is your "middle." Ranks by gap width and computes balanced stake sizing (so losing either single side costs the same amount).

Options:
- `--min-width N` — minimum line gap to bother showing (in the stat's own units)
- `--total-stake N` — dollar amount to split across both legs (default $100)
- `--exclude-books book1,book2,...` — exclude books unavailable in your state; saves to a separate `..._filtered.csv` file so you keep both the full and restricted views

Output: `data/scoresandodds_market_comparison_middling_opportunities.csv` (or `_filtered.csv`).

## 4. Estimate expected value

```cmd
python scrapers\estimate_middling_ev.py --opportunities data\scoresandodds_market_comparison_middling_opportunities.csv --market-data data\scoresandodds_market_comparison.csv.gz
```

Estimates P(middle hits) and real dollar EV per opportunity, using the market's consensus line (averaged across books) for the mean and the player's actual historical performance for the variance — cross-book line differences alone can't tell you variance, since every book sets its own line near a 50/50 point.

**Real limitations, not glossed over:**
- Needs 6+ historical games for that player/stat, or it reports "insufficient data" rather than guessing
- Assumes a roughly normal distribution — solid for yardage stats, weak for low-count stats (touchdowns, interceptions), flagged automatically when it applies
- Uses recent historical variance as a stand-in for this week specifically — doesn't adjust for this week's particular matchup

Output: `..._middling_opportunities_with_ev.csv`, sorted by expected value.

---

## One-shot version

```cmd
python scrapers\scrape_scoresandodds_props.py --all --combine
python scrapers\scrape_scoresandodds_market_comparison.py
python scrapers\find_middling_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-width 2 --total-stake 100
python scrapers\estimate_middling_ev.py --opportunities data\scoresandodds_market_comparison_middling_opportunities.csv --market-data data\scoresandodds_market_comparison.csv.gz
```
