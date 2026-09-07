@echo off
REM ============================================================
REM weekly_before.bat — everything to run BEFORE a week's games
REM start: salaries, weather forecast, depth charts, schedule,
REM season-long props, and the prop-challenge candidate pool.
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
echo [1/7] Updating schedule (kickoff times)...
python scrapers\scrape_schedules.py --years %YEAR%
if %errorlevel% neq 0 (
    echo WARNING: schedule scrape failed — lineup locking may use stale kickoff times.
)
flask load-schedule --year %YEAR%

echo.
echo [2/7] Scraping DraftKings salaries...
python scrapers\scrape_fp_dk_salaries.py --week %WEEK% --year %YEAR%
if %errorlevel% neq 0 (
    echo ERROR: salary scrape failed — the Slate page won't have this week's players. Stopping.
    exit /b 1
)
flask load-weekly-salary data\fp_dk_salaries_week%WEEK%_%YEAR%.csv.gz --year %YEAR%

echo.
echo [3/7] Scraping weather forecasts...
python scrapers\scrape_weekly_weather.py --year %YEAR% --weeks %WEEK%
if %errorlevel% neq 0 (
    echo WARNING: weather scrape failed — non-critical, continuing.
)

echo.
echo [4/7] Scraping depth charts...
python scrapers\scrape_ourlads_depth_charts.py
if %errorlevel% neq 0 (
    echo WARNING: depth chart scrape failed — Best Matchups' depth-chart filter and the
    echo Slate's String column will be stale until this succeeds.
)

echo.
echo [5/7] Scraping season-long fantasy points against (FirstDown Studio)...
python scrapers\scrape_firstdown_studio.py --position all
if %errorlevel% neq 0 (
    echo WARNING: FirstDown Studio scrape failed — non-critical, continuing.
)

echo.
echo [6/7] Scraping this week's prop bet candidates...
python scrapers\scrape_scoresandodds_props.py --all --combine
if %errorlevel% neq 0 (
    echo WARNING: props scrape failed — skipping the candidate-pool generation below.
    goto skip_props
)
python scrapers\convert_scoresandodds_to_props_csv.py --output data\props_candidates_week%WEEK%_%YEAR%.csv
python scrapers\select_top_props_by_category.py --input data\props_candidates_week%WEEK%_%YEAR%.csv --output data\props_week%WEEK%_%YEAR%.csv --top-n 6
:skip_props

echo.
echo [7/7] Persisting everything to the database...
flask load-history

echo.
echo ============================================================
echo DONE. Remaining MANUAL steps:
echo ============================================================
echo   1. Review data\props_week%WEEK%_%YEAR%.csv before publishing it
echo   2. flask add-props data\props_week%WEEK%_%YEAR%.csv --year %YEAR% --week %WEEK%
echo ============================================================
