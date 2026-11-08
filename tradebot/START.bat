@echo off
title Trade Bot
cd /d "%~dp0"

REM ---- 1. Python check ----
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python is not installed.
  echo Opening the download page. Install it and TICK "Add python.exe to PATH",
  echo then double-click START.bat again.
  start https://www.python.org/downloads/
  pause
  exit /b
)

REM ---- 2. One-time install ----
if not exist ".venv" (
  echo Installing - first run only, takes 1-2 minutes...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements.txt pytest
) else (
  call .venv\Scripts\activate.bat
)

REM ---- 3. Keys ----
if not exist ".env" copy .env.example .env >nul
findstr /r "^ALPACA_API_KEY=..*" .env >nul
if errorlevel 1 (
  echo.
  echo Paste your Alpaca PAPER keys into the file that opens, save it, close it.
  echo Get keys at https://app.alpaca.markets  ^(Paper Trading ^> API Keys^)
  start https://app.alpaca.markets
  notepad .env
)
findstr /c:"example.com" config.yaml >nul
if not errorlevel 1 (
  echo.
  echo Now put YOUR name and email on the sec_user_agent line, save, close.
  notepad config.yaml
)

REM ---- 4. Menu ----
:menu
echo.
echo ===============================
echo   1  Dry run  (shows trades, places none)
echo   2  Run once (paper orders)
echo   3  Run every weekday at 10:00 ET (leave window open)
echo   4  Open dashboard
echo   5  Run tests
echo   6  Quit
echo ===============================
set /p c=Pick 1-6:
if "%c%"=="1" python -m bot.main --dry-run
if "%c%"=="2" python -m bot.main --once
if "%c%"=="3" python -m bot.main --loop
if "%c%"=="4" streamlit run dashboard.py
if "%c%"=="5" python -m pytest -q
if "%c%"=="6" exit /b
goto menu
