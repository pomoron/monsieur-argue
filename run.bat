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

:: ── If args were passed, skip the menu and run directly ─────
if not "%~1"=="" (
    call "%VENV%\Scripts\activate.bat"
    python main.py %*
    goto :end
)

:: ── Interactive menu ────────────────────────────────────────
:menu
cls
echo.
echo  ============================================================
echo   Monsieur Argue ^| Negotiation Training
echo  ============================================================
echo.
echo   1.  Start session          (text mode)
echo   2.  Start session          (voice mode — mic + speech)
echo   3.  Start session + contract PDF
echo   4.  Start session + past learnings
echo   5.  Start session + contract + learnings
echo   6.  List audio devices     (diagnose mic issues)
echo   7.  Run evaluator          (score a past session)
echo   8.  Reinstall packages     (re-run pip install)
echo   9.  Open Python in venv    (interactive shell)
echo   0.  Exit
echo.
set /p "CHOICE=  Choice: "

if "%CHOICE%"=="1" goto :text_mode
if "%CHOICE%"=="2" goto :voice_mode
if "%CHOICE%"=="3" goto :contract_mode
if "%CHOICE%"=="4" goto :learnings_mode
if "%CHOICE%"=="5" goto :full_mode
if "%CHOICE%"=="6" goto :list_devices
if "%CHOICE%"=="7" goto :evaluator
if "%CHOICE%"=="8" goto :reinstall
if "%CHOICE%"=="9" goto :shell
if "%CHOICE%"=="0" goto :exit
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

:exit
exit /b 0

:end
echo.
pause
goto :menu
