@echo off
REM ===================================================================
REM  Start Compass. Double-click this file, or type  run  in a terminal.
REM  Optionally pass a port:   run 8001
REM
REM  Why this file exists: there are two python.exe on this machine, and
REM  only the one inside .venv has Compass's dependencies. This calls that
REM  one by its full path so it can never pick the wrong interpreter.
REM ===================================================================

setlocal
cd /d "%~dp0"

set PORT=%1
if "%PORT%"=="" set PORT=8000

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   The virtual environment is missing.
    echo   Run setup.cmd first.
    echo.
    pause
    exit /b 1
)

REM Fail early and legibly if something already holds the port. Without this
REM check uvicorn dies with a Python traceback, and - worse - the browser still
REM shows a working Compass served by the *older* process, so you end up testing
REM stale code without realising it.
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
    REM /a forces arithmetic - plain %PORT%1 would concatenate to "80001".
    set /a NEXTPORT=%PORT%+1
    echo.
    echo   Port %PORT% is already in use - Compass is probably already running.
    echo.
    echo   Open http://localhost:%PORT% to use it, or find and stop the old one:
    echo.
    echo       netstat -ano ^| findstr :%PORT%
    echo       taskkill /PID ^<the-number-in-the-last-column^> /F
    echo.
    call echo   Or start this one on a different port:   run %%NEXTPORT%%
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting Compass...  open http://localhost:%PORT% in your browser
echo   Press Ctrl+C in this window to stop it.
echo.

REM No --reload: that is a development flag which restarts the server on every
REM file change, and each restart re-imports torch (~30 seconds).
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

echo.
echo   Compass has stopped.
pause
