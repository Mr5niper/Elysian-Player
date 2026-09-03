@echo off
setlocal enabledelayedexpansion

:: ==========================================================================
::  Elysian Player 2.3.0.0 EXE Builder
::  Strictly requires Python 3.13.12
::  Works even when Python is NOT on PATH (uses the "py" launcher).
::
::  Now a genuine one-stop build: this script also locates a Visual Studio
::  C++ toolchain, ensures an LGPL SHARED FFmpeg build is present (fetching
::  one if third_party\ffmpeg is empty), builds elysian_video_real.dll,
::  THEN does everything it already did (venv, pinned deps, PyInstaller),
::  and finally copies the native DLL and its FFmpeg runtime DLLs into
::  dist\ alongside the exe. See NOTICE at the repo root before changing
::  anything about which FFmpeg build gets linked.
::
::  Put this .bat in the SAME folder as:
::     - run.py              (the entry point)
::     - elysian\            (the application package)
::     - native\elysian_video\  (the native video engine sources)
::     - icon.ico            (window/exe icon)
::     - version_info.txt    (exe version info)
::     - requirements.txt    (pinned dependencies)
::  Then just double-click it. Requires "Build Tools for Visual Studio"
::  with the "Desktop development with C++" workload, and a network
::  connection the first time (to fetch FFmpeg), unless you have already
::  placed an LGPL SHARED FFmpeg build at third_party\ffmpeg yourself.
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
if not exist "native\elysian_video\BUILD_DLL_REAL.bat" (
    echo [ERROR] native\elysian_video\BUILD_DLL_REAL.bat was not found.
    echo         That script builds the native video engine this build now
    echo         bundles; it must sit alongside its sources under native\.
    goto :error
)
echo [INFO] Inputs present: %SCRIPT_NAME%, %PACKAGE_DIR%\, requirements.txt, %ICON%, %VERSION_FILE%
echo [INFO] Starting build process...

:: ==========================================================================
:: 1. Locate a Visual Studio C++ toolchain
:: ==========================================================================
:: vswhere.exe ships with every Visual Studio Installer since VS2017 at this
:: fixed path, whether or not Visual Studio itself is installed, and is
:: Microsoft's own documented way to locate an install from a script.
echo [STEP 1/8] Locating a Visual Studio C++ toolchain...
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo [ERROR] vswhere.exe not found at "%VSWHERE%".
    echo         Install "Build Tools for Visual Studio" with the
    echo         "Desktop development with C++" workload, then re-run this.
    echo         https://visualstudio.microsoft.com/visual-cpp-build-tools/
    goto :error
)

set "VSINSTALL="
for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSINSTALL=%%I"
if not defined VSINSTALL (
    echo [ERROR] No Visual Studio install with the C++ build tools component
    echo         was found. Install "Build Tools for Visual Studio" with the
    echo         "Desktop development with C++" workload, then re-run this.
    echo         https://visualstudio.microsoft.com/visual-cpp-build-tools/
    goto :error
)

set "VCVARS=%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat"
if not exist "%VCVARS%" (
    echo [ERROR] vcvars64.bat not found under "%VSINSTALL%".
    echo         The C++ build tools component may be only partially installed.
    goto :error
)
call "%VCVARS%" >nul
if errorlevel 1 (
    echo [ERROR] Failed to set up the MSVC build environment from
    echo         "%VCVARS%".
    goto :error
)
where cl.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] cl.exe still not on PATH after vcvars64.bat ran. Something is
    echo         wrong with the Visual Studio install at "%VSINSTALL%".
    goto :error
)
echo [INFO] MSVC toolchain ready from "%VSINSTALL%".
echo =======================================================

:: ==========================================================================
:: 2. Ensure an LGPL SHARED FFmpeg build is present
:: ==========================================================================
:: A pinned, moving "latest" tag from a well-known, actively-maintained
:: Windows FFmpeg build project (BtbN/FFmpeg-Builds). It floats to whatever
:: the most recent successful build is, which is convenient for a one-click
:: build but not byte-for-byte reproducible; pin an autobuild-YYYY-MM-DD-HH-MM
:: release tag instead if you need that. This MUST stay the "lgpl-shared"
:: variant. See NOTICE at the repo root for why: switching this to a
:: "gpl-shared" or "nonfree-shared" build would change the license
:: obligations of the whole shipped exe.
echo [STEP 2/8] Ensuring an LGPL shared FFmpeg build is present...
set "FFMPEG_DIR=%CD%\third_party\ffmpeg"
set "FFMPEG_ZIP_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl-shared.zip"

if exist "%FFMPEG_DIR%\include\libavformat\avformat.h" (
    echo [INFO] Found existing FFmpeg at "%FFMPEG_DIR%"; not re-downloading.
) else (
    echo [INFO] Downloading LGPL shared FFmpeg build:
    echo        %FFMPEG_ZIP_URL%
    if not exist "third_party" mkdir "third_party"
    curl -L --fail -o "third_party\ffmpeg_download.zip" "%FFMPEG_ZIP_URL%"
    if errorlevel 1 (
        echo [ERROR] Download failed. Check your network connection, or
        echo         place an LGPL SHARED FFmpeg build yourself at
        echo         "%FFMPEG_DIR%" with include\, lib\*.lib and bin\*.dll
        echo         inside it, then re-run this.
        goto :error
    )
    echo [INFO] Extracting...
    if exist "third_party\ffmpeg_extract" rmdir /s /q "third_party\ffmpeg_extract"
    powershell -NoProfile -Command "Expand-Archive -Path 'third_party\ffmpeg_download.zip' -DestinationPath 'third_party\ffmpeg_extract' -Force"
    if errorlevel 1 (
        echo [ERROR] Extraction failed.
        goto :error
    )
    set "FFDIR="
    for /d %%D in ("third_party\ffmpeg_extract\ffmpeg-*") do set "FFDIR=%%D"
    if not defined FFDIR (
        echo [ERROR] Could not find the extracted FFmpeg folder inside the
        echo         downloaded archive; its layout may have changed.
        goto :error
    )
    if exist "%FFMPEG_DIR%" rmdir /s /q "%FFMPEG_DIR%"
    move "!FFDIR!" "%FFMPEG_DIR%" >nul
    if errorlevel 1 (
        echo [ERROR] Could not move the extracted FFmpeg into place.
        goto :error
    )
    del "third_party\ffmpeg_download.zip" >nul 2>&1
    rmdir /s /q "third_party\ffmpeg_extract" >nul 2>&1
    echo [INFO] FFmpeg ready at "%FFMPEG_DIR%".
)

if not exist "%FFMPEG_DIR%\include\libavformat\avformat.h" (
    echo [ERROR] FFmpeg still not found at "%FFMPEG_DIR%" after the ensure step.
    goto :error
)
echo =======================================================

:: ==========================================================================
:: 3. Build the native video engine (elysian_video_real.dll)
:: ==========================================================================
echo [STEP 3/8] Building the native video engine...
pushd "native\elysian_video"
call BUILD_DLL_REAL.bat
set "DLL_RC=%errorlevel%"
popd
if not "%DLL_RC%"=="0" (
    echo [ERROR] Native engine build failed. Scroll up for the compiler error.
    goto :error
)
if not exist "native\elysian_video\elysian_video_real.dll" (
    echo [ERROR] elysian_video_real.dll was not produced.
    goto :error
)
echo [INFO] Native engine built.
echo =======================================================

:: ==========================================================================
:: 4. Create a CLEAN Virtual Environment
:: ==========================================================================
echo [STEP 4/8] Creating a clean virtual environment in '.venv'...

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
:: 5. Activate Virtual Environment
:: ==========================================================================
echo [STEP 5/8] Activating virtual environment...
call ".venv\Scripts\activate.bat"

if not defined VIRTUAL_ENV (
    echo [ERROR] Failed to activate the virtual environment.
    echo         Make sure '.venv\Scripts\activate.bat' exists.
    goto :error
)

:: ==========================================================================
:: 6. Install the pinned dependencies
:: ==========================================================================
:: pip itself is upgraded, but setuptools and wheel are deliberately NOT
:: upgraded here. They are pinned in requirements.txt, and upgrading them
:: first would just get them downgraded back a moment later.
echo [STEP 6/8] Upgrading pip and installing pinned dependencies...
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
:: 7. Build with PyInstaller on the command line (no .spec file)
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
echo [STEP 7/8] Building the onefile executable with PyInstaller...
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
echo =======================================================

:: ==========================================================================
:: 8. Bundle the native engine and its FFmpeg runtime into dist\
:: ==========================================================================
:: bindings.py looks for elysian_video_real.dll next to sys.executable when
:: frozen, i.e. right here in dist\, not inside the onefile archive - so
:: these are copied as loose files, not embedded with --add-binary. The
:: FFmpeg runtime DLLs sit alongside them for the same reason: Windows
:: resolves elysian_video_real.dll's own dependencies by first checking the
:: directory it was loaded from.
echo [STEP 8/8] Bundling the native engine and its FFmpeg runtime into dist...
if not exist "dist" (
    echo [ERROR] "dist" was not created; the PyInstaller step must not have run.
    goto :error
)
copy /y "native\elysian_video\elysian_video_real.dll" "dist\" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy elysian_video_real.dll into dist\.
    goto :error
)
copy /y "%FFMPEG_DIR%\bin\avformat-*.dll" "dist\" >nul
copy /y "%FFMPEG_DIR%\bin\avcodec-*.dll" "dist\" >nul
copy /y "%FFMPEG_DIR%\bin\avutil-*.dll" "dist\" >nul
copy /y "%FFMPEG_DIR%\bin\swscale-*.dll" "dist\" >nul
copy /y "%FFMPEG_DIR%\bin\swresample-*.dll" "dist\" >nul
if exist "%FFMPEG_DIR%\LICENSE.txt" copy /y "%FFMPEG_DIR%\LICENSE.txt" "dist\FFMPEG_LICENSE.txt" >nul
if exist "NOTICE" copy /y "NOTICE" "dist\NOTICE" >nul
echo [INFO] Native engine and FFmpeg runtime bundled into dist\.

echo.
echo [SUCCESS] Build completed successfully.
echo The single-file executable, elysian_video_real.dll, its FFmpeg runtime
echo DLLs, and license notices are all in the '.\dist' directory.
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
