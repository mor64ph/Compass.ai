@echo off
REM ===================================================================
REM  One-time setup: create the virtual environment, install everything,
REM  and create .env. Safe to re-run - it skips work already done.
REM
REM  Double-click this file, or type  setup  in a terminal.
REM ===================================================================

cd /d "%~dp0"

echo.
echo ==========================================================
echo   Compass setup
echo ==========================================================

REM ---- 1. the virtual environment -------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo   [1/3] Virtual environment already exists - skipping.
) else (
    echo   [1/3] Creating the virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo   Could not create it. Is Python installed and on PATH?
        echo   Check with:  python --version
        echo.
        pause
        exit /b 1
    )
)

REM ---- 2. dependencies ------------------------------------------------
echo   [2/3] Installing dependencies. The first run downloads about 2 GB
echo         ^(PyTorch^), so this can take several minutes.
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo   Installation failed - see the output above.
    echo.
    pause
    exit /b 1
)

REM ---- 3. configuration ----------------------------------------------
if exist ".env" (
    echo   [3/3] .env already exists - leaving it alone.
) else (
    echo   [3/3] Creating .env with a generated secret key...
    copy /y .env.example .env >nul
    for /f "delims=" %%K in ('.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"') do set SECRET=%%K
    .venv\Scripts\python.exe -c "import pathlib; p=pathlib.Path('.env'); p.write_text(p.read_text(encoding='utf-8').replace('change-me-to-a-random-string', '%SECRET%'), encoding='utf-8')"
)

echo.
echo ==========================================================
echo   Setup complete.
echo ==========================================================
echo.
echo   Next:
echo     check.cmd        run all automated checks  ^(free^)
echo     run.cmd          start the app, then open http://localhost:8000
echo.
echo   To use the AI features, put your key in .env:
echo     ANTHROPIC_API_KEY=sk-ant-...
echo   then run verify-llm.cmd
echo.
pause
