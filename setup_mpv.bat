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
if exist "venv\Scripts\python.exe" set "CAPCAP_PYTHON=venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" if "%CAPCAP_PYTHON%"=="python" set "CAPCAP_PYTHON=.venv\Scripts\python.exe"
%CAPCAP_PYTHON% -c "import mpv" >nul 2>&1
if errorlevel 1 (
    echo [CapCap] python-mpv is not installed; installing it now...
    %CAPCAP_PYTHON% -m pip install python-mpv
    if errorlevel 1 echo [WARN] Could not install python-mpv. The packaged app does not need this step.
)

rem Verify the real eSpeak data file. `import piper` alone can succeed with a
rem legacy wheel whose native module still points at its build machine.
%CAPCAP_PYTHON% -m pip uninstall -y piper-phonemize >nul 2>&1
%CAPCAP_PYTHON% -c "import pathlib,piper; p=pathlib.Path(piper.__file__).resolve().parent/'espeak-ng-data'/'phontab'; raise SystemExit(0 if p.is_file() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [CapCap] Repairing legacy Piper TTS runtime...
    %CAPCAP_PYTHON% -m pip uninstall -y piper-tts piper-phonemize >nul 2>&1
    %CAPCAP_PYTHON% -m pip install --no-cache-dir "piper-tts>=1.7.0"
    if errorlevel 1 (
        echo [ERROR] Could not install a working Piper TTS runtime.
        exit /b 1
    )
)

echo.
echo [OK] MPV setup completed. You can start CapCap now.
pause
exit /b 0
