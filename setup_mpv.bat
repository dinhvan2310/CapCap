@echo off
setlocal EnableExtensions

rem CapCap MPV runtime setup. Run this from the repository root after clone.
cd /d "%~dp0"

echo [CapCap] Checking Git LFS...
where git >nul 2>&1 || (
    echo [ERROR] Git is not installed. Install Git for Windows first.
    exit /b 1
)
git lfs version >nul 2>&1 || (
    echo [ERROR] Git LFS is not installed.
    echo         Install it from https://git-lfs.com/ and run this file again.
    exit /b 1
)

if not exist ".git" (
    echo [ERROR] This folder is not a Git checkout.
    echo         Clone the repository with Git, rather than downloading a ZIP.
    exit /b 1
)

git lfs install >nul 2>&1
echo [CapCap] Downloading MPV runtime from Git LFS...
git lfs pull
if errorlevel 1 (
    echo [ERROR] Git LFS could not download the MPV runtime.
    exit /b 1
)

if not exist "bin\mpv\libmpv-2.dll" (
    echo [ERROR] bin\mpv\libmpv-2.dll is missing after Git LFS pull.
    echo         Run: git lfs pull
    exit /b 1
)

echo [CapCap] MPV runtime is ready.

rem python-mpv is required only when running CapCap from source.
set "CAPCAP_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "CAPCAP_PYTHON=.venv\Scripts\python.exe"
%CAPCAP_PYTHON% -c "import mpv" >nul 2>&1
if errorlevel 1 (
    echo [CapCap] python-mpv is not installed; installing it now...
    %CAPCAP_PYTHON% -m pip install python-mpv
    if errorlevel 1 echo [WARN] Could not install python-mpv. The packaged app does not need this step.
)

rem Piper 1.2 used an obsolete build-time espeak-ng path on Windows.
rem Piper 1.7+ bundles espeak-ng-data and works from any checkout path.
%CAPCAP_PYTHON% -c "import piper; print(getattr(piper, '__version__', 'installed'))" >nul 2>&1
if errorlevel 1 (
    echo [CapCap] Installing Piper TTS...
    %CAPCAP_PYTHON% -m pip install "piper-tts>=1.7.0"
) else (
    %CAPCAP_PYTHON% -m pip install --upgrade "piper-tts>=1.7.0"
)

echo.
echo [OK] MPV setup completed. You can start CapCap now.
pause
exit /b 0
