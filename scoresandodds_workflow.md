# ScoresAndOdds Market Analysis Workflow

Personal betting research — separate from the weekly site operations
documented in `README.md`. Steps 1-2 are shared setup; after that,
four independent analysis scripts all read the same Step 2 output and
can be run in any combination depending on what you're looking for.

**Note:** Step 1 below is the same command as step 6 of the README's
"Beginning of week" checklist (which feeds the Prop Bet Challenge
instead). If you've already run the weekly prep, that file's already
fresh — skip straight to Step 2.

## 1. Scrape all prop categories

```cmd
python scrapers\scrape_scoresandodds_props.py --all --combine
```

Pulls every convertible category (passing yards, rushing yards, receptions, touchdowns, etc.) into one file: `data/scoresandodds_props_all.csv.gz`. Each row is one player's "best odds" line, plus an `event_id` used in the next step.

## 2. Scrape every book's own line/odds per prop

```cmd
python scrapers\scrape_scoresandodds_market_comparison.py
```

For every prop from Step 1, fetches the full multi-book comparison (not just the "best" one) into `data/scoresandodds_market_comparison.csv.gz` — one row per (player, category, book). This is the shared input every analysis script below reads.

- **Saves incrementally** — a crash or cancel partway through only loses the one in-flight row, not the whole run.
- **Default behavior does a full refresh** every time (lines change over time, so you generally want current data).
- If a run gets interrupted and you want to pick up where it left off instead of refreshing everything: add `--resume`.

This step takes a while (one request per player/category pair) — budget accordingly.

---

## 3. Analysis scripts

All four read `data/scoresandodds_market_comparison.csv.gz` by default and support `--exclude-books book1,book2,...` for books unavailable in your state.

### Middling opportunities

```cmd
python scrapers\find_middling_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-width 2 --total-stake 100
```

For each player/category, finds the book with the *lowest* line (bet Over there) and the book with the *highest* line (bet Under there) — the gap between them is your "middle." Only pays off big if the result lands inside the gap; otherwise a small loss. Ranks by gap width and computes balanced stake sizing.

Options:
- `--min-width N` — minimum line gap to bother showing (in the stat's own units)
- `--total-stake N` — dollar amount to split across both legs (default $100)
- `--exclude-books book1,book2,...` — saves to a separate `..._filtered.csv` file so you keep both the full and restricted views
- `--exclude-book-category book:category,...` — for a book missing a specific market entirely (e.g. `caesars:rushing-and-receiving-yards`) without dropping that book from every other category or that category from every other book
- `--with-ev` — also computes expected value in the same run (see below) instead of needing a separate `estimate_middling_ev.py` call. Needs the Flask app/database, unlike the plain width-based calculation, so this is opt-in rather than the default.

Output: `data/scoresandodds_market_comparison_middling_opportunities.csv` (or `_filtered.csv` if any exclusion flag was used).

### Expected value for middling opportunities

```cmd
python scrapers\estimate_middling_ev.py --opportunities data\scoresandodds_market_comparison_middling_opportunities.csv --market-data data\scoresandodds_market_comparison.csv.gz
```

Estimates P(middle hits) and real dollar EV per opportunity, using the market's consensus line (averaged across books) for the mean and the player's actual historical performance for the variance — cross-book line differences alone can't tell you variance, since every book sets its own line near a 50/50 point.

Standalone alternative to passing `--with-ev` above — same underlying calculation either way, this one just runs as its own separate step against an already-saved opportunities file.

**Real limitations, not glossed over:**
- Needs 6+ historical games for that player/stat, or it reports "insufficient data" rather than guessing
- Assumes a roughly normal distribution — solid for yardage stats, weak for low-count stats (touchdowns, interceptions), flagged automatically when it applies
- Uses recent historical variance as a stand-in for this week specifically — doesn't adjust for this week's particular matchup

Output: `..._with_ev.csv`, sorted by expected value.

### Arbitrage (guaranteed profit, same line only)

```cmd
python scrapers\find_arbitrage_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-profit-pct 1.0 --total-stake 100
```

Different from middling: this only checks the SAME line across books. If the best Over price at one book and the best Under price at another combine to less than 100% implied probability, betting both sides locks in guaranteed profit regardless of the actual result — no variance at all, unlike middling.

Genuinely rare — same-line, cross-book gaps big enough to clear the vig entirely don't come up often. Real risk is execution (books limiting bet size on this kind of action) and line movement between placing the two bets, not the outcome itself.

Output: `..._arbitrage_opportunities.csv`.

### Value bets vs. scoresandodds' own projection

```cmd
python scrapers\find_value_bets.py --input data\scoresandodds_market_comparison.csv.gz --min-diff-pct 8.0
```

Flags props where scoresandodds' own projection disagrees with the market line by more than the threshold — a signal their model sees something different, not a guaranteed edge. Doesn't tell you which number is actually right; worth checking the player's specific situation before trusting the disagreement alone.

Output: `..._value_bets.csv`.

### Outlier lines (one book vs. the rest of the market)

```cmd
python scrapers\find_outlier_lines.py --input data\scoresandodds_market_comparison.csv.gz --min-diff-pct 10.0 --min-books 3
```

Flags a book's line when it's a significant outlier against the consensus of every OTHER book on the same prop (that book is excluded from its own comparison baseline, so a real outlier can't understate its own deviation by pulling the average toward itself). Broader than middling or arbitrage — doesn't need a matched pair, just flags anything that stands out. Needs `--min-books` books quoting the same prop for "consensus" to mean anything.

Output: `..._outlier_lines.csv`.

---

## One-shot version (everything)

```cmd
python scrapers\scrape_scoresandodds_props.py --all --combine
python scrapers\scrape_scoresandodds_market_comparison.py
python scrapers\find_middling_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-width 2 --total-stake 100 --with-ev
python scrapers\find_arbitrage_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-profit-pct 1.0 --total-stake 100
python scrapers\find_value_bets.py --input data\scoresandodds_market_comparison.csv.gz --min-diff-pct 8.0
python scrapers\find_outlier_lines.py --input data\scoresandodds_market_comparison.csv.gz --min-diff-pct 10.0 --min-books 3
```
