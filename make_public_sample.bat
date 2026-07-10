@echo off
setlocal EnableExtensions DisableDelayedExpansion

title Make public CoinPoker sample HH

echo.
echo ================================================
echo  Make public anonymized CoinPoker sample
echo ================================================
echo.

set "SCRIPT=anonymize_coinpoker_hh.py"
if not exist "%SCRIPT%" (
    echo ERROR: %SCRIPT% was not found.
    echo Keep this BAT file in the same folder as the anonymizer.
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

set "INPUT=cash.txt"
set /p "INPUT_USER=Input HH file [default: cash.txt]: "
if not "%INPUT_USER%"=="" set "INPUT=%INPUT_USER%"
set "INPUT=%INPUT:"=%"

if not exist "%INPUT%" (
    echo.
    echo ERROR: Input file not found: %INPUT%
    echo.
    pause
    exit /b 1
)

set "HERO=Hero"
set /p "HERO_USER=Your current Hero/screen name [default: Hero]: "
if not "%HERO_USER%"=="" set "HERO=%HERO_USER%"

set "MAXHANDS=1000"
set /p "MAXHANDS_USER=How many hands to export? [default: 1000, 0 = all]: "
if not "%MAXHANDS_USER%"=="" set "MAXHANDS=%MAXHANDS_USER%"

set "OUTPUT=examples\public_sample_hands.txt"
set /p "OUTPUT_USER=Output file [default: examples\public_sample_hands.txt]: "
if not "%OUTPUT_USER%"=="" set "OUTPUT=%OUTPUT_USER%"
set "OUTPUT=%OUTPUT:"=%"

echo.
echo Creating public sample:
echo   Input: %INPUT%
echo   Output: %OUTPUT%
echo   Hero replacement: Hero
echo   Max hands: %MAXHANDS%
echo   Hand IDs and table IDs: rewritten
echo   Dates and times: shifted
echo.

%PYTHON_CMD% "%SCRIPT%" --input "%INPUT%" --output "%OUTPUT%" --hero "%HERO%" --max-hands %MAXHANDS%

if errorlevel 1 (
    echo.
    echo ERROR: Anonymizer failed.
    echo.
    pause
    exit /b 1
)

echo.
echo Done.
echo Review this file manually before publishing:
echo   %OUTPUT%
echo.
pause
endlocal
