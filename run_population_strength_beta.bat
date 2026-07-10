@echo off
setlocal EnableExtensions DisableDelayedExpansion

title CoinPoker Population Strength Analyzer - BETA

echo.
echo ================================================
echo  CoinPoker Population Strength Analyzer - BETA
echo ================================================
echo.

set "SCRIPT=coinpoker_population_strength_analyzer.py"
if not exist "%SCRIPT%" (
    echo ERROR: %SCRIPT% was not found.
    echo Keep this BAT file in the same folder as the Python script.
    echo.
    pause
    exit /b 1
)

set "PYTHON_CMD="
python --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    py --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py"
)
if not defined PYTHON_CMD (
    echo ERROR: Python 3 was not found.
    echo Install Python 3, then run this file again.
    echo.
    pause
    exit /b 1
)

set "INPUT=cash_example.txt"
set /p "INPUT_USER=Hand-history file [default: cash_example.txt]: "
if not "%INPUT_USER%"=="" set "INPUT=%INPUT_USER%"
set "INPUT=%INPUT:"=%"

if not exist "%INPUT%" (
    echo.
    echo ERROR: Input file not found: %INPUT%
    echo Put the CoinPoker HH .txt file in this folder, or enter its full path.
    echo.
    pause
    exit /b 1
)

set "HERO=Hero"
set /p "HERO_USER=Hero screen name [default: Hero]: "
if not "%HERO_USER%"=="" set "HERO=%HERO_USER%"

set "OUTDIR=population_strength_report"
set /p "OUTDIR_USER=Output folder [default: population_strength_report]: "
if not "%OUTDIR_USER%"=="" set "OUTDIR=%OUTDIR_USER%"
set "OUTDIR=%OUTDIR:"=%"

echo.
echo Running default analysis:
echo   Game: PLO4 regular only
echo   Stakes: all
echo   Min stack: 40bb+
echo   Min known hands per line: 10
echo.

%PYTHON_CMD% "%SCRIPT%" --input "%INPUT%" --hero "%HERO%" --outdir "%OUTDIR%" --game-filter plo4 --min-stack-bb 40 --min-known 10

if errorlevel 1 (
    echo.
    echo ERROR: Analyzer failed.
    echo Check the input file and the error message above.
    echo.
    pause
    exit /b 1
)

echo.
echo Done.
echo Opening:
echo   %OUTDIR%\population_strength_presentation.html
echo.
start "" "%OUTDIR%\population_strength_presentation.html"
pause
endlocal
