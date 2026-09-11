@echo off
REM ============================================================
REM weekly_after.bat — everything to run AFTER a week's games
REM finish: backup, player stats, team points, DST scoring, game
REM info, final weather, fantasy points against, then persist and
REM let Standings recompute.
REM
REM Usage:
REM     weekly_after.bat 2026 3
REM     (year=2026, week=3)
REM ============================================================

if "%~1"=="" (
    echo ERROR: year required. Usage: weekly_after.bat YEAR WEEK
    exit /b 1
)
if "%~2"=="" (
    echo ERROR: week required. Usage: weekly_after.bat YEAR WEEK
    exit /b 1
)

set YEAR=%~1
set WEEK=%~2

echo ============================================================
echo AFTER-WEEK SCORING — Year %YEAR%, Week %WEEK%
echo ============================================================

echo.
echo [1/8] Backing up lineups, prop_bets, and prop_picks...
REM Runs first, before any other step writes to the database — and
REM this is the most valuable backup point of the week, since every
REM lineup and prop pick for the week is now in and final.
flask export-critical-data
if %errorlevel% neq 0 (
    echo WARNING: backup failed — continuing, but this week's submissions are not backed up.
)

echo.
echo [2/8] Scraping player stats and game info...
python scrapers\scrape_pfr.py --years %YEAR%
if %errorlevel% neq 0 (
    echo ERROR: player stats scrape failed — Standings, History, and Props scoring will
    echo all be stale for this week until this succeeds. Check for a PFR rate limit or
    echo expired cf_clearance cookie ^(see scrape_pfr.py's retry/backoff messages above^).
    exit /b 1
)

echo.
echo [3/8] Scraping team points scored/allowed...
python scrapers\scrape_team_points.py --years %YEAR%
if %errorlevel% neq 0 (
    echo ERROR: team points scrape failed — DST scoring needs this. Stopping before
    echo combine_dst_scoring.py, since it would compute wrong/incomplete DST points otherwise.
    exit /b 1
)

echo.
echo [4/8] Scraping DST fantasy stat categories...
python scrapers\scrape_dst_fantasy_stats.py --year %YEAR% --weeks %WEEK%
if %errorlevel% neq 0 (
    echo WARNING: DST stats scrape failed — combine_dst_scoring.py will fall back to the
    echo historical PFR-boxscore source if available, otherwise DST scoring will be stale.
)

echo.
echo [5/8] Combining DST scoring...
python scrapers\combine_dst_scoring.py --year %YEAR%
if %errorlevel% neq 0 (
    echo WARNING: DST scoring combine failed — DST picks won't score this week until fixed.
)

echo.
echo [6/8] Re-scraping weather for final/actual conditions...
python scrapers\scrape_weekly_weather.py --year %YEAR% --weeks %WEEK%
if %errorlevel% neq 0 (
    echo WARNING: weather scrape failed — non-critical, continuing.
)

echo.
echo [7/8] Scraping fantasy points against...
REM Season-to-date totals, so this only changes once a week's games
REM have actually been played — which is why it lives here rather
REM than in weekly_before.bat.
python scrapers\scrape_fantasy_points_against.py --year %YEAR% --position all
if %errorlevel% neq 0 (
    echo WARNING: fantasy points against scrape failed — that page will be stale.
)

echo.
echo [8/8] Persisting everything to the database...
flask load-history

echo.
echo ============================================================
echo DONE. Standings, History, and My Lineups will now reflect
echo Week %WEEK% results ^(assuming all steps above succeeded —
echo re-check any WARNING/ERROR lines before trusting the numbers^).
echo ============================================================
