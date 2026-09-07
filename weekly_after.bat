@echo off
REM ============================================================
REM weekly_after.bat — everything to run AFTER a week's games
REM finish: player stats, team points, DST scoring, game info,
REM final weather, then persist and let Standings recompute.
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
echo [1/6] Scraping player stats and game info...
python scrapers\scrape_pfr.py --years %YEAR%
if %errorlevel% neq 0 (
    echo ERROR: player stats scrape failed — Standings, History, and Props scoring will
    echo all be stale for this week until this succeeds. Check for a PFR rate limit or
    echo expired cf_clearance cookie ^(see scrape_pfr.py's retry/backoff messages above^).
    exit /b 1
)

echo.
echo [2/6] Scraping team points scored/allowed...
python scrapers\scrape_team_points.py --years %YEAR%
if %errorlevel% neq 0 (
    echo ERROR: team points scrape failed — DST scoring needs this. Stopping before
    echo combine_dst_scoring.py, since it would compute wrong/incomplete DST points otherwise.
    exit /b 1
)

echo.
echo [3/6] Scraping DST fantasy stat categories...
python scrapers\scrape_dst_fantasy_stats.py --year %YEAR% --weeks %WEEK%
if %errorlevel% neq 0 (
    echo WARNING: DST stats scrape failed — combine_dst_scoring.py will fall back to the
    echo historical PFR-boxscore source if available, otherwise DST scoring will be stale.
)

echo.
echo [4/6] Combining DST scoring...
python scrapers\combine_dst_scoring.py --year %YEAR%
if %errorlevel% neq 0 (
    echo WARNING: DST scoring combine failed — DST picks won't score this week until fixed.
)

echo.
echo [5/6] Re-scraping weather for final/actual conditions...
python scrapers\scrape_weekly_weather.py --year %YEAR% --weeks %WEEK%
if %errorlevel% neq 0 (
    echo WARNING: weather scrape failed — non-critical, continuing.
)

echo.
echo [6/6] Persisting everything to the database...
flask load-history

echo.
echo ============================================================
echo DONE. Standings, History, and My Lineups will now reflect
echo Week %WEEK% results ^(assuming all steps above succeeded —
echo re-check any WARNING/ERROR lines before trusting the numbers^).
echo ============================================================
