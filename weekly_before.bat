@echo off
REM ============================================================
REM weekly_before.bat — everything to run BEFORE a week's games
REM start: backup, schedule, salaries, weather forecast, depth
REM charts, injuries, game odds, Vegas-derived rankings, and the
REM prop-challenge candidate pool.
REM
REM Usage:
REM     weekly_before.bat 2026 3
REM     (year=2026, week=3)
REM ============================================================

if "%~1"=="" (
    echo ERROR: year required. Usage: weekly_before.bat YEAR WEEK
    exit /b 1
)
if "%~2"=="" (
    echo ERROR: week required. Usage: weekly_before.bat YEAR WEEK
    exit /b 1
)

set YEAR=%~1
set WEEK=%~2

echo ============================================================
echo BEFORE-WEEK PREP — Year %YEAR%, Week %WEEK%
echo ============================================================

echo.
echo [1/11] Backing up lineups, prop_bets, and prop_picks...
REM Runs first, before any other step writes to the database.
REM These three tables are the only ones with no external source
REM to re-scrape from if they're ever lost.
flask export-critical-data
if %errorlevel% neq 0 (
    echo WARNING: backup failed — continuing, but this run has no fresh backup behind it.
)

echo.
echo [2/11] Updating schedule (kickoff times)...
python scrapers\scrape_schedules.py --years %YEAR%
if %errorlevel% neq 0 (
    echo WARNING: schedule scrape failed — lineup locking may use stale kickoff times.
)
flask load-schedule --year %YEAR%

echo.
echo [3/11] Scraping DraftKings salaries...
python scrapers\scrape_fp_dk_salaries.py --week %WEEK% --year %YEAR%
if %errorlevel% neq 0 (
    echo ERROR: salary scrape failed — the Slate page won't have this week's players. Stopping.
    exit /b 1
)
flask load-weekly-salary data\fp_dk_salaries_week%WEEK%_%YEAR%.csv.gz --year %YEAR%

echo.
echo [4/11] Scraping weather forecasts...
python scrapers\scrape_weekly_weather.py --year %YEAR% --weeks %WEEK%
if %errorlevel% neq 0 (
    echo WARNING: weather scrape failed — non-critical, continuing.
)

echo.
echo [5/11] Scraping depth charts (and Ourlads' own injury flags)...
python scrapers\scrape_ourlads_depth_charts.py
if %errorlevel% neq 0 (
    echo WARNING: depth chart scrape failed — Best Matchups' depth-chart filter and the
    echo Slate's String column will be stale until this succeeds.
)

echo.
echo [6/11] Scraping Draftedge injury statuses...
REM Gap-fill source only — load-history loads Ourlads first (step 5)
REM and this one never overwrites a player Ourlads already flagged.
python scrapers\scrape_draftedge_injuries.py
if %errorlevel% neq 0 (
    echo WARNING: Draftedge injury scrape failed — Depth Charts will only show the
    echo injuries Ourlads flagged, with no gap-fill for DST/special-teams players.
)

echo.
echo [7/11] Scraping game odds (spread / over-under / favorite)...
python scrapers\scrape_scoresandodds_game_odds.py
if %errorlevel% neq 0 (
    echo WARNING: game odds scrape failed — Implied Team Points, Game Overview, and Best
    echo Matchups' O/U, Spread, Fav and Team Implied columns will be stale.
)

echo.
echo [8/11] Scraping FirstDown Studio rankings (FDS Pts comparison column)...
python scrapers\scrape_firstdown_studio_rankings.py
if %errorlevel% neq 0 (
    echo WARNING: FirstDown Studio rankings scrape failed — the FDS Pts column on
    echo Implied Player Points will be stale.
)

echo.
echo [9/11] Scraping FirstDown Studio season-long projections...
REM Reference data for your own research only — load-history does NOT
REM read firstdown_studio_season_*.csv.gz. The site's FDS Pts column
REM comes from step 8's separate rankings scrape, not this one.
python scrapers\scrape_firstdown_studio.py --position all
if %errorlevel% neq 0 (
    echo WARNING: FirstDown Studio season scrape failed — non-critical, continuing.
)

echo.
echo [10/11] Scraping this week's prop bet candidates...
python scrapers\scrape_scoresandodds_props.py --all --combine
if %errorlevel% neq 0 (
    echo WARNING: props scrape failed — skipping the candidate-pool generation below.
    goto skip_props
)
python scrapers\convert_scoresandodds_to_props_csv.py --output data\props_candidates_week%WEEK%_%YEAR%.csv
python scrapers\select_top_props_by_category.py --input data\props_candidates_week%WEEK%_%YEAR%.csv --output data\props_week%WEEK%_%YEAR%.csv --top-n 6
:skip_props

echo.
echo [11/11] Persisting everything to the database...
flask load-history

echo.
echo ============================================================
echo DONE. Remaining MANUAL steps:
echo ============================================================
echo   1. Review data\props_week%WEEK%_%YEAR%.csv before publishing it
echo   2. flask add-props data\props_week%WEEK%_%YEAR%.csv --year %YEAR% --week %WEEK%
echo ============================================================
