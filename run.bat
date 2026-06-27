@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  run.bat
::  Launch the Monsieur Argue negotiation training tool.
::
::  Usage:
::    run.bat                   — interactive menu
::    run.bat --voice           — jump straight to voice mode
::    run.bat --list-audio-devices
::    run.bat [any main.py args passed straight through]
:: ============================================================

set "DIR=%~dp0"
set "VENV=%DIR%.venv"
set "PYTHON=%VENV%\Scripts\python.exe"

:: ── Check venv exists ───────────────────────────────────────
if not exist "%PYTHON%" (
    echo.
    echo  [ERROR] Virtual environment not found.
    echo          Run setup.bat first.
    echo.
    pause
    exit /b 1
)

cd /d "%DIR%"

:: ── Extract ANTHROPIC_API_KEY from config.json (used by Legora) ─────────────
for /f "delims=" %%k in ('python -c "import json,sys; c=json.load(open('config.json')); print(c.get('claude',{}).get('api_key',''))" 2^>nul') do set "ANTHROPIC_API_KEY=%%k"

:: ── If args were passed, skip the menu and run directly ─────
if not "%~1"=="" (
    call "%VENV%\Scripts\activate.bat"
    python main.py %*
    goto :end
)

:: ── Interactive menu ────────────────────────────────────────
:menu
cd /d "%DIR%"
cls
echo.
echo  ============================================================
echo   Monsieur Argue ^| Negotiation Training
echo  ============================================================
echo.
echo   --- Monsieur Argue (practice) ----------------------------
echo   1.  Start session          (text mode)
echo   2.  Start session          (voice mode - mic + speech)
echo   3.  Start session + contract PDF
echo   4.  Start session + past learnings
echo   5.  Start session + contract + learnings (voice)
echo   6.  List audio devices     (diagnose mic issues)
echo   7.  Run evaluator          (score a past session)
echo.
echo   --- Legora (adaptive training loop) ----------------------
echo   10. Play full loop         (text - negotiate + score + adapt difficulty)
echo   11. Play full loop         (voice - mic input + spoken AI responses)
echo   12. Play full loop         (mock / offline, no API key needed)
echo   13. Assess a transcript    (score an existing session JSON)
echo   14. My progress + stats    (difficulty, streak, weaknesses)
echo   15. Simulate difficulty    (demo the adaptive curve)
echo.
echo   --- Web / Frontend ---------------------------------------
echo   16. Start API server       (FastAPI on :8000, then run frontend separately)
echo   17. Start full web stack   (API server + Bun dev server on :3000)
echo.
echo   --- Utilities --------------------------------------------
echo   8.  Reinstall packages     (re-run pip install)
echo   9.  Open Python in venv    (interactive shell)
echo   0.  Exit
echo.
set /p "CHOICE=  Choice: "

if "%CHOICE%"=="1"  goto :text_mode
if "%CHOICE%"=="2"  goto :voice_mode
if "%CHOICE%"=="3"  goto :contract_mode
if "%CHOICE%"=="4"  goto :learnings_mode
if "%CHOICE%"=="5"  goto :full_mode
if "%CHOICE%"=="6"  goto :list_devices
if "%CHOICE%"=="7"  goto :evaluator
if "%CHOICE%"=="8"  goto :reinstall
if "%CHOICE%"=="9"  goto :shell
if "%CHOICE%"=="10" goto :legora_play
if "%CHOICE%"=="11" goto :legora_voice
if "%CHOICE%"=="12" goto :legora_mock
if "%CHOICE%"=="13" goto :legora_assess
if "%CHOICE%"=="14" goto :legora_status
if "%CHOICE%"=="15" goto :legora_simulate
if "%CHOICE%"=="16" goto :web_api
if "%CHOICE%"=="17" goto :web_full
if "%CHOICE%"=="0"  goto :exit
goto :menu

:: ────────────────────────────────────────────────────────────

:text_mode
call "%VENV%\Scripts\activate.bat"
echo.
python main.py
goto :end

:voice_mode
call "%VENV%\Scripts\activate.bat"
echo.
python main.py --voice
goto :end

:contract_mode
echo.
set /p "PDF=  Path to contract PDF: "
if "%PDF%"=="" goto :menu
call "%VENV%\Scripts\activate.bat"
echo.
python main.py --contract "%PDF%"
goto :end

:learnings_mode
call "%VENV%\Scripts\activate.bat"
echo.
python main.py --learnings inputs\past_learnings.json
goto :end

:full_mode
echo.
set /p "PDF=  Path to contract PDF: "
if "%PDF%"=="" goto :menu
call "%VENV%\Scripts\activate.bat"
echo.
python main.py --contract "%PDF%" --learnings inputs\past_learnings.json --voice
goto :end

:list_devices
call "%VENV%\Scripts\activate.bat"
echo.
python main.py --list-audio-devices
echo.
pause
goto :menu

:evaluator
echo.
set /p "SUMMARY=  Path to negotiation_summary JSON: "
if "%SUMMARY%"=="" goto :menu
call "%VENV%\Scripts\activate.bat"
echo.
python evaluator.py --summary "%SUMMARY%" --config config.json --output .
echo.
pause
goto :menu

:reinstall
call "%VENV%\Scripts\activate.bat"
echo.
echo  Reinstalling packages ...
pip install -r requirements.txt
echo.
pause
goto :menu

:shell
call "%VENV%\Scripts\activate.bat"
echo.
echo  Type 'exit' to return.
echo.
python
goto :menu

::
:: --- Legora ------------------------------------------------------------------
::
:: Legora uses ANTHROPIC_API_KEY (set from config.json above).
:: Learnings are shared with the main tool via inputs\past_learnings.json.
::

:legora_play
call "%VENV%\Scripts\activate.bat"
echo.
echo  Starting Legora full loop  (text mode)
echo  Past weaknesses in inputs\past_learnings.json will sharpen the opponent.
echo.
python integrated\Legora\legora.py play --learnings inputs\past_learnings.json
goto :end

:legora_voice
call "%VENV%\Scripts\activate.bat"
echo.
echo  Starting Legora full loop  (voice mode - mic input + spoken AI)
echo  Past weaknesses in inputs\past_learnings.json will sharpen the opponent.
echo.
python integrated\Legora\legora.py play --voice --learnings inputs\past_learnings.json
goto :end

:legora_mock
call "%VENV%\Scripts\activate.bat"
echo.
echo  Starting Legora mock session  (canned opponent, offline scoring)
echo.
python integrated\Legora\legora.py play --mock --learnings inputs\past_learnings.json
goto :end

:legora_assess
echo.
set /p "SUMMARY=  Path to negotiation_summary JSON: "
if "%SUMMARY%"=="" goto :menu
call "%VENV%\Scripts\activate.bat"
echo.
python integrated\Legora\legora.py assess -t "%SUMMARY%" -s inputs\scenario.json -p integrated\Legora\playbook.json --progress progress.json
echo.
pause
goto :menu

:legora_status
call "%VENV%\Scripts\activate.bat"
echo.
python integrated\Legora\legora.py status --progress progress.json --learnings inputs\past_learnings.json
echo.
pause
goto :menu

:legora_simulate
call "%VENV%\Scripts\activate.bat"
echo.
python integrated\Legora\legora.py simulate
echo.
pause
goto :menu

::
:: --- Web / Frontend ----------------------------------------------------------
::
:: Option 16: API server only (port 8000).
:: Run the frontend separately: cd frontend && bun install && bun run dev
::
:: Option 17: Both servers in separate windows.
::

:web_api
call "%VENV%\Scripts\activate.bat"
echo.
echo  Starting Python API server on http://localhost:8000
echo  Press Ctrl+C to stop.
echo  (Run the frontend separately: cd frontend ^&^& bun install ^&^& bun run dev)
echo.
python -m uvicorn api_server:app --reload --port 8000
goto :end

:web_full
call "%VENV%\Scripts\activate.bat"
echo.

:: ── Check for a JS runtime (bun preferred, npm fallback) ─────────────────────
set "JS_RUNNER="
where bun >nul 2>&1 && set "JS_RUNNER=bun"
if "!JS_RUNNER!"=="" (
    where npm >nul 2>&1 && set "JS_RUNNER=npm"
)
if "!JS_RUNNER!"=="" (
    echo  [ERROR] No JavaScript runtime found.
    echo.
    echo  The frontend requires Bun (recommended) or Node.js / npm.
    echo.
    echo  Install Bun   (fast, recommended):
    echo    https://bun.sh   or:  winget install Oven-sh.Bun
    echo.
    echo  Install Node  (alternative):
    echo    https://nodejs.org
    echo.
    echo  After installing, re-run this option.
    echo.
    pause
    goto :menu
)

echo  JS runtime: !JS_RUNNER!
echo  Starting Python API server on http://localhost:8000 (new window) ...
start "Monsieur Argue API" cmd /k "cd /d "%DIR%" && call "%VENV%\Scripts\activate.bat" && python -m uvicorn api_server:app --reload --port 8000"
echo.
echo  Installing frontend dependencies ...
cd /d "%DIR%frontend"
if "!JS_RUNNER!"=="bun" (
    bun install
) else (
    npm install
)
echo.
echo  Starting frontend dev server on http://localhost:3000 ...
echo  Open http://localhost:3000 in your browser.
echo  Press Ctrl+C to stop.
echo.
if "!JS_RUNNER!"=="bun" (
    bun run dev
) else (
    npm run dev
)
goto :end

:exit
exit /b 0

:end
echo.
pause
goto :menu
