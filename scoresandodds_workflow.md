# ScoresAndOdds Market Analysis Workflow

Personal betting research — separate from the weekly site operations
documented in `README.md`. Steps 1-2 are shared setup; after that,
the analysis scripts below all read the same Step 2 output and
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

All of them read `data/scoresandodds_market_comparison.csv.gz` by default. The DraftKings/Caesars scripts take `--books` (the books you can bet); the middling/arbitrage/value/outlier scripts take `--exclude-books book1,book2,...` for books unavailable in your state.

### +EV bets at your books (DraftKings / Caesars)

```cmd
python scrapers\find_ev_bets.py --books draftkings,caesars --min-ev-pct 2
```

Single bets, no second leg: flags a price at a target book that beats the no-vig "fair" price built from every *other* real sportsbook on the same prop. The one to use when you can only bet at one or two books.

- **Fair price:** each other book's over/under is de-vigged, turned into a distribution (normal for yards/attempts/completions, Poisson for receptions, passing TDs, INTs), and the median taken. PrizePicks/Underdog/Sleeper are never used, since their odds are fixed payouts, not prices. Quotes the API marks `available: false` are dropped.
- **Different lines are compared properly:** a DraftKings 15.5 vs FanDuel 17.5 is converted to probability using how spread out that stat really is. Spreads were fitted from 2018–2025 PFR game logs (sd ≈ a·line^b, roughly a square root, so low lines are relatively much wider). Longest reception/rush/completion and kicking points have no calibration data, so they're compared at the same line only.
- **TD scorer props** (anytime/first/last) are skipped unless you pass `--one-way`. They have no "No" side, so that mode assumes a flat margin, and books' margin grows sharply toward longshots, so it floods with fake longshot "edges". Use the TD model below instead.
- Output columns: `fair_odds` (the no-vig price), `ev_pct`, `quarter_kelly_pct` (bankroll % at 1/4 Kelly), `ref_lines` (lines the other books hang), `best_other_same_line`.

**Real limitations:** "fair" means "the other books' consensus", and none of these books is a sharp market-maker (no Pinnacle/Circa). Props at 2–5% EV are the realistic range. Anything much higher is usually stale data, a line that moved, or injury news, so check the live price and the player's status first. Books limit accounts that win consistently.

Output: `..._ev_bets.csv`, sorted by EV.

### Anytime TD model (DraftKings / Caesars)

```cmd
python scrapers\td_model.py --fit
python scrapers\find_td_bets.py --books draftkings,caesars
```

Prices anytime TD props with our own probability instead of other books' prices: expected team TDs (from the Vegas implied team total) × the player's TD share (recent carries, targets and his own TD share from nflverse usage, weighted toward recent games). `td_model.py --fit` is only needed again when the model changes or a season of data is added; it writes `data/td_model_params.json`.

- **Tested before trusted:** fitted on 2015–2023, scored on 2024–2025. Log loss 0.365 vs 0.421 for a position base rate, and predicted TD rates land within a few points of actual in every probability bucket, by position, and for players who changed teams or lost snaps. The script prints all of that on every fit.
- **Measured quirks:** red zone / inside-10 shares add almost nothing once carry and target shares are in (0.93–0.96 correlated). QBs score ~1.7× what their carry share suggests (sneaks). Team changes, falling snaps and a new season each discount the probability.
- **Output is split in two.** *Clean*: played this season, 20%+ snaps last game, same team. *Check first*: the model's blind spot is offseason role change (Bam Knight: 46% snaps late 2025, 4% last game, still priced from the old role). Players with no game since before last season are skipped.
- **Read `gap_pp`:** model probability minus the other books' median implied probability (vig included). A few points is a normal disagreement; a huge gap is more often news the model can't see.

**Real limitations:** calibrated against outcomes, not against book prices, since there's no odds history yet. Until `bet_tracker.py` has graded a few weeks, treat the EV as a ranking, not a bankroll number. Rookies (no usage history) aren't priced.

Output: `..._td_bets.csv`.

### Very low lines (0.5 / 1.0 / 1.5)

```cmd
python scrapers\find_low_line_props.py --books draftkings,caesars --max-line 1.5
```

"One catch cashes it" props: receiving yards / receptions / longest reception / rushing yards at very low lines. Each price is compared against the player's own game logs (recent games weighted more, `weighted_rate`), shrunk toward the other books' no-vig price. Shows team pass attempts per game and players with a catch per game for the "teams that spread it around" angle. Books other than bet365 rarely hang these; ScoresAndOdds doesn't carry alt/milestone ladders ("1+ receptions") at all.

Output: `..._low_line_props.csv`.

### Weather flags

```cmd
python scrapers\weather_flags.py data\scoresandodds_market_comparison_ev_bets.csv
```

Adds each game's forecast to a finder output and marks `weather_lean` with/against. Measured on 2014–2025: Vegas totals already price weather into scoring, but not the pass/run split, since at 15+ mph wind or ≤32°F teams throw for ~20 fewer yards at the same implied total. So Overs on passing props (and Unders on rushing props) in those games lean *against*. Re-scrape weather close to kickoff; retractable roofs count as domes.

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

## 4. Track results (do this every week, or none of the above is proven)

```cmd
python scrapers\bet_tracker.py snapshot
python scrapers\bet_tracker.py log data\scoresandodds_market_comparison_ev_bets.csv data\scoresandodds_market_comparison_td_bets.csv data\scoresandodds_market_comparison_low_line_props.csv
python scrapers\bet_tracker.py grade --year 2026 --week 2
```

- **`snapshot`** after every market scrape copies the odds into `data\odds_archive\<year>_wk<NN>\`. The last snapshot before a game's kickoff is its closing line, so **scrape again Sunday morning** (and Thursday afternoon for TNF) or there's no close to compare against.
- **`log`** records every flagged bet in `data\bet_log\<year>_wk<NN>.csv`, keeping the earliest flag of each price.
- **`grade`** (after `weekly_after.bat` has loaded the week's stats) marks win/loss/push/void, units won, and **closing line value**: `clv_pct` (your price vs the same book's closing price) and `ev_at_close` (your price vs the no-vig consensus at the close). CLV settles in a few weeks; win/loss takes months. Longest-X and kicking points are ungradeable from box scores. Leave off `--week` for the season to date.

---

## One-shot version (everything)

```cmd
python scrapers\scrape_scoresandodds_props.py --all --combine
python scrapers\scrape_scoresandodds_market_comparison.py
python scrapers\bet_tracker.py snapshot
python scrapers\find_ev_bets.py --books draftkings,caesars --min-ev-pct 2
python scrapers\find_td_bets.py --books draftkings,caesars
python scrapers\find_low_line_props.py --books draftkings,caesars --max-line 1.5
python scrapers\weather_flags.py data\scoresandodds_market_comparison_ev_bets.csv data\scoresandodds_market_comparison_low_line_props.csv
python scrapers\bet_tracker.py log data\scoresandodds_market_comparison_ev_bets.csv data\scoresandodds_market_comparison_td_bets.csv data\scoresandodds_market_comparison_low_line_props.csv
python scrapers\find_middling_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-width 2 --total-stake 100 --with-ev
python scrapers\find_arbitrage_opportunities.py --input data\scoresandodds_market_comparison.csv.gz --min-profit-pct 1.0 --total-stake 100
python scrapers\find_value_bets.py --input data\scoresandodds_market_comparison.csv.gz --min-diff-pct 8.0
python scrapers\find_outlier_lines.py --input data\scoresandodds_market_comparison.csv.gz --min-diff-pct 10.0 --min-books 3
```
