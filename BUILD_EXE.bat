@echo off
setlocal enabledelayedexpansion

:: ==========================================================================
::  Elysian Player 2.3.0.0 EXE Builder
::  Strictly requires Python 3.13.12
::  Works even when Python is NOT on PATH (uses the "py" launcher).
::
::  Put this .bat in the SAME folder as:
::     - run.py              (the entry point)
::     - elysian\            (the application package)
::     - icon.ico            (window/exe icon)
::     - version_info.txt    (exe version info)
::     - requirements.txt    (pinned dependencies)
::  Then just double-click it.
::
::  This build does NOT use a .spec file. PyInstaller generates one from the
::  command-line flags below, and this script deletes it again afterward, so
::  there is nothing extra to keep in the repo.
:: ==========================================================================

:: ---- EDIT THESE IF YOU RENAME FILES --------------------------------------
set "SCRIPT_NAME=run.py"
set "PACKAGE_DIR=elysian"
set "EXE_NAME=Elysian Player"
set "ICON=icon.ico"
set "VERSION_FILE=version_info.txt"
:: --------------------------------------------------------------------------

set "REQUIRED_PYTHON_VERSION=3.13.12"
set "PYTHON_DOWNLOAD_URL=https://www.python.org/downloads/release/python-31312/"

:: Always operate on the folder this script lives in, not the caller's cwd,
:: so double-clicking and "run from another directory" behave the same.
cd /d "%~dp0"

:: ==========================================================================
:: Pre-flight Check: Verify Python Version
:: ==========================================================================
echo [INFO] Checking Python version...

:: Prefer the "py" launcher pinned to 3.13. It lives in C:\Windows and is
:: reachable even when the 'python' command on PATH is a different version.
:: Fall back to 'python' on PATH only if it is also exactly the required one.
set "PY_CMD="
set "VER_A="
set "VER_B="

for /f "tokens=2" %%I in ('py -3.13 --version 2^>nul') do set "VER_A=%%I"
if "!VER_A!"=="%REQUIRED_PYTHON_VERSION%" set "PY_CMD=py -3.13"

if not defined PY_CMD (
    for /f "tokens=2" %%I in ('python --version 2^>nul') do set "VER_B=%%I"
    if "!VER_B!"=="%REQUIRED_PYTHON_VERSION%" set "PY_CMD=python"
)

if not defined PY_CMD (
    set "CURRENT_PYTHON_VERSION=!VER_B!"
    if not defined CURRENT_PYTHON_VERSION set "CURRENT_PYTHON_VERSION=!VER_A!"
    if not defined CURRENT_PYTHON_VERSION set "CURRENT_PYTHON_VERSION=None"
    if "!CURRENT_PYTHON_VERSION!"=="" set "CURRENT_PYTHON_VERSION=None"
    goto :WrongVersion
)

echo [INFO] Required Python version: %REQUIRED_PYTHON_VERSION%
echo [INFO] Python %REQUIRED_PYTHON_VERSION% detected via "!PY_CMD!".
echo =======================================================

:: ==========================================================================
:: Pre-flight Check: Verify every input the build needs
:: ==========================================================================
if not exist "%SCRIPT_NAME%" (
    echo [ERROR] "%SCRIPT_NAME%" not found next to this script.
    echo         Edit SCRIPT_NAME at the top of this file if you renamed it.
    goto :error
)
if not exist "%PACKAGE_DIR%\web\index.html" (
    echo [ERROR] %PACKAGE_DIR%\web\index.html was not found.
    echo         That folder is the entire user interface.
    goto :error
)
if not exist "%PACKAGE_DIR%\__init__.py" (
    echo [ERROR] The '%PACKAGE_DIR%' package folder was not found.
    echo         %SCRIPT_NAME% imports it, so the build needs it alongside.
    goto :error
)
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt not found in the project root.
    echo         The pinned dependency list is required for a reproducible build.
    goto :error
)
if not exist "%ICON%" (
    echo [ERROR] %ICON% not found in the project root.
    echo         The build embeds it as both the exe icon and the window icon.
    goto :error
)
if not exist "%VERSION_FILE%" (
    echo [ERROR] %VERSION_FILE% not found in the project root.
    echo         The build reads the Windows file-details version from it.
    goto :error
)
echo [INFO] Inputs present: %SCRIPT_NAME%, %PACKAGE_DIR%\, requirements.txt, %ICON%, %VERSION_FILE%
echo [INFO] Starting build process...

:: ==========================================================================
:: 1. Create a CLEAN Virtual Environment
:: ==========================================================================
echo [STEP 1/4] Creating a clean virtual environment in '.venv'...

:: Always start from a fresh venv so a stale dependency version cannot
:: silently persist across builds. What gets tested here has to be exactly
:: what everyone else builds, and that means nothing survives from last time.
if exist ".venv" (
    echo [INFO] Removing existing '.venv' for a clean, reproducible build...
    rmdir /s /q ".venv"
)

%PY_CMD% -m venv ".venv"
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    goto :error
)

:: ==========================================================================
:: 2. Activate Virtual Environment
:: ==========================================================================
echo [STEP 2/4] Activating virtual environment...
call ".venv\Scripts\activate.bat"

if not defined VIRTUAL_ENV (
    echo [ERROR] Failed to activate the virtual environment.
    echo         Make sure '.venv\Scripts\activate.bat' exists.
    goto :error
)

:: ==========================================================================
:: 3. Install the pinned dependencies
:: ==========================================================================
:: pip itself is upgraded, but setuptools and wheel are deliberately NOT
:: upgraded here. They are pinned in requirements.txt, and upgrading them
:: first would just get them downgraded back a moment later.
echo [STEP 3/4] Upgrading pip and installing pinned dependencies...
python -m pip install --upgrade pip >nul
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    goto :error
)

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies from requirements.txt.
    goto :error
)

:: Print what actually landed in the venv. If someone else's build misbehaves,
:: this output can be diffed against requirements.txt line by line.
echo.
echo [INFO] Installed dependency versions:
python -m pip freeze
echo.

:: ==========================================================================
:: 4. Build with PyInstaller on the command line (no .spec file)
:: ==========================================================================
::    --onefile      single self-contained exe
::    --windowed     GUI app, no console window
::    --clean        clear the PyInstaller cache first
::    --noconfirm    overwrite a previous build without prompting
::    --noupx        no UPX compression (avoids antivirus false positives)
::    --icon         exe icon shown in Explorer
::    --add-data     icon.ico again, because resource_path() reads it at
::                   runtime for the window icon
::    --version-file Windows file-details metadata
::    --add-data     elysian\web holds index.html, style.css and app.js. That
::                   is the entire interface; without it the window opens blank.
::    --collect-all  just_playback and miniaudio ship the miniaudio DLL, which
::                   is the audio backend. If this is missing, the app runs but
::                   nothing plays.
::    --exclude-module  pywebview can drive Qt or GTK as well as WinForms, and
::                   PyInstaller bundles every backend it can find. Excluding
::                   the unused ones is what keeps this a ~40 MB exe. tkinter
::                   is excluded too, since it is no longer used for dialogs.
::
:: python -m PyInstaller is used rather than bare 'pyinstaller' so the build
:: cannot accidentally pick up a PyInstaller from outside this venv.
echo [STEP 4/4] Building the onefile executable with PyInstaller...
python -m PyInstaller --onefile --windowed --clean --noconfirm --noupx ^
 --name "%EXE_NAME%" ^
 --icon "%ICON%" ^
 --add-data "%ICON%;." ^
 --add-data "elysian\web;elysian/web" ^
 --version-file "%VERSION_FILE%" ^
 --collect-all just_playback ^
 --collect-all miniaudio ^
 --collect-submodules webview ^
 --collect-submodules mutagen ^
 --hidden-import PIL.Image ^
 --hidden-import clr_loader ^
 --exclude-module pygame ^
 --exclude-module dearpygui ^
 --exclude-module numpy ^
 --exclude-module matplotlib ^
 --exclude-module tkinter ^
 --exclude-module PySide6 ^
 --exclude-module PyQt5 ^
 --exclude-module PyQt6 ^
 --exclude-module gi ^
 "%SCRIPT_NAME%"

:: Capture the result first, then remove the .spec PyInstaller just generated.
:: It is rebuilt from the flags above on every run, so it must never be left
:: sitting in the folder or committed. A stale one invites someone to run
:: "pyinstaller <spec>" and silently ignore every flag set here.
set "BUILD_RC=!errorlevel!"
if exist "%EXE_NAME%.spec" del "%EXE_NAME%.spec" >nul 2>&1

if not "!BUILD_RC!"=="0" (
    echo =======================================================
    echo [ERROR] PyInstaller build failed. Scroll up for the error.
    goto :error
)

echo.
echo [SUCCESS] Build completed successfully.
echo The single-file executable is in the '.\dist' directory ("%EXE_NAME%.exe").
goto :end

:WrongVersion
echo =======================================================
echo [ERROR] Incorrect Python Version!
echo.
echo You currently have: Python !CURRENT_PYTHON_VERSION!
echo This script requires exactly: Python %REQUIRED_PYTHON_VERSION%
echo.
echo Please download and install Python %REQUIRED_PYTHON_VERSION% from here:
echo %PYTHON_DOWNLOAD_URL%
echo.
echo During installation, enable the "py launcher" option (and optionally
echo "Add Python to PATH").
echo =======================================================
start "" "%PYTHON_DOWNLOAD_URL%"
goto :end

:error
echo.
echo [FAILURE] The build process failed. Please check the errors above.
echo.
pause
exit /b 1

:end
echo.
pause
endlocal
