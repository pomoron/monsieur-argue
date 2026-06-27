@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  setup.bat
::  One-time virtual environment setup for Monsieur Argue.
::  Run this once before using run.bat.
:: ============================================================

set "DIR=%~dp0"
set "VENV=%DIR%.venv"

echo.
echo  ============================================================
echo   Monsieur Argue ^| Environment Setup
echo  ============================================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found on PATH.
    echo          Download from https://www.python.org/downloads/
    echo          Make sure "Add Python to PATH" is checked during install.
    goto :fail
)
for /f "tokens=*" %%v in ('python --version') do echo  Found: %%v

:: Create venv if it doesn't exist
if exist "%VENV%\Scripts\activate.bat" (
    echo  Virtual environment already exists at .venv
    echo  To rebuild it, delete the .venv folder and re-run setup.bat
) else (
    echo.
    echo  Creating virtual environment at .venv ...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo  [ERROR] Failed to create virtual environment.
        goto :fail
    )
    echo  Done.
)

:: Activate and install
echo.
echo  Installing packages from requirements.txt ...
echo  (This may take a minute on first run.)
echo.
call "%VENV%\Scripts\activate.bat"
pip install --upgrade pip -q
pip install -r "%DIR%requirements.txt"
if errorlevel 1 (
    echo.
    echo  [ERROR] pip install failed. Check the output above.
    goto :fail
)

echo.
echo  ============================================================
echo   Setup complete. Run  run.bat  to start a session.
echo  ============================================================
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
