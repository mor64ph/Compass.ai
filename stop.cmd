@echo off
REM ===================================================================
REM  Stop Compass and any tunnel.
REM
REM  Exists because a --reload worker or a minimised uvicorn window keeps
REM  port 8000 and the SQLite lock after its parent is gone, and the next
REM  run then fails with an unhelpful "address already in use".
REM ===================================================================

cd /d "%~dp0"

echo.
echo   Stopping cloudflared...
taskkill /f /im cloudflared.exe >nul 2>&1
if errorlevel 1 (echo     none running.) else (echo     stopped.)

echo   Stopping anything listening on port 8000...

REM Match on the listening port rather than on "python.exe": killing every
REM python on the machine would take out unrelated work.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"TCP.*:8000 .*LISTENING"') do (
    echo     pid %%P
    taskkill /f /pid %%P >nul 2>&1
)

echo.
echo   Done. Run run.cmd or share.cmd to start again.
echo.
pause
