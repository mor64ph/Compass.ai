@echo off
REM ===================================================================
REM  Run every automated check. Needs no API key and costs nothing.
REM  Double-click this file, or type  check  in a terminal.
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
echo ==========================================================
echo   1 of 2  Unit tests
echo ==========================================================
.venv\Scripts\python.exe -m pytest -q
if errorlevel 1 goto failed

echo.
echo ==========================================================
echo   2 of 2  Smoke test - boots the app and walks every page
echo ==========================================================
.venv\Scripts\python.exe scripts\smoke.py
if errorlevel 1 goto failed

echo.
echo   ALL CHECKS PASSED
echo.
pause
exit /b 0

:failed
echo.
echo   SOMETHING FAILED - see the output above.
echo.
pause
exit /b 1
