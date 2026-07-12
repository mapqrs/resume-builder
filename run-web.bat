@echo off
rem Launch the local Resume Builder web UI (Windows).
rem
rem First run sets everything up for you (creates a private Python environment
rem and installs dependencies - about 30 seconds, once). Every run after that
rem just launches and opens your browser.
rem
rem Usage:  run-web.bat          (double-click works too)
rem
rem To skip the auto-opened browser, run these two lines in a terminal:
rem         set "RESUME_BUILDER_NO_BROWSER=1"
rem         run-web.bat
setlocal

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "REQS=%ROOT%requirements.txt"
set "STAMP=%VENV%\.deps-installed"

rem --- 1. Create the virtualenv on first run --------------------------------
if exist "%VENV_PY%" goto :check_deps

echo ^> First-time setup - creating a private Python environment...

rem Prefer the py launcher (standard on Windows), fall back to python.
set "PYBIN="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>&1 && set "PYBIN=py -3"
if not defined PYBIN (
    python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>&1 && set "PYBIN=python"
)
if not defined PYBIN (
    echo ERROR: Need Python 3.9 or newer.
    echo   Install it from https://www.python.org/downloads/
    echo   IMPORTANT: tick "Add python.exe to PATH" in the installer,
    echo   then run this script again.
    pause
    exit /b 1
)

%PYBIN% -m venv "%VENV%"
if errorlevel 1 (
    echo ERROR: Could not create the Python environment.
    pause
    exit /b 1
)
goto :install_deps

rem --- 2. Existing venv: install only if something is missing or changed ----
:check_deps
"%VENV_PY%" -c "import flask, docx, yaml, pydantic, pypdf" >nul 2>&1
if errorlevel 1 goto :install_deps
if not exist "%STAMP%" goto :install_deps
fc /b "%REQS%" "%STAMP%" >nul 2>&1
if errorlevel 1 goto :install_deps
goto :launch

:install_deps
echo ^> Installing dependencies (one moment)...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r "%REQS%"
if errorlevel 1 (
    rem pip failed - fine as long as the core app can still run. Optional
    rem extras (e.g. the Anthropic SDK) are not required.
    "%VENV_PY%" -c "import flask, docx, yaml, pydantic, pypdf" >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Dependency install failed. Run it manually:
        echo   "%VENV_PY%" -m pip install -r "%REQS%"
        pause
        exit /b 1
    )
    echo WARNING: Some optional dependencies did not install - the app still
    echo works (copy-paste / claude CLI^). Re-run later to retry.
)
copy /y "%REQS%" "%STAMP%" >nul
echo ^> Dependencies ready.

rem --- 3. Launch -------------------------------------------------------------
:launch
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%src"
"%VENV_PY%" -m resume_builder.web %*
