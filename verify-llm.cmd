@echo off
REM ===================================================================
REM  Exercise every Claude-backed feature once and report what works.
REM  Double-click this file, or type  verify-llm  in a terminal.
REM
REM  THIS ONE COSTS MONEY - roughly $1 of Anthropic API usage per run.
REM  It needs ANTHROPIC_API_KEY set in .env first.
REM ===================================================================

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   The virtual environment is missing. Run setup.cmd first.
    echo.
    pause
    exit /b 1
)

set HF_HUB_DISABLE_SYMLINKS_WARNING=1

echo.
echo   This makes about a dozen calls to Claude - roughly $1 of API usage.
echo   Responses are cached, so running it again shortly after is free.
echo.
set /p CONFIRM="   Continue? (y/n) "
if /i not "%CONFIRM%"=="y" (
    echo   Cancelled.
    pause
    exit /b 0
)

echo.
.venv\Scripts\python.exe scripts\verify_llm.py --with-research

echo.
pause
