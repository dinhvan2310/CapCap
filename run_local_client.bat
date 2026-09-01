@echo off
setlocal
cd /d "%~dp0"

set "VENV_PYTHON=%~dp0venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo [CapCap] Python virtual environment not found:
    echo          %~dp0venv
    echo.
    echo Create it with: py -m venv venv
    echo Then install dependencies with: "%VENV_PYTHON%" -m pip install -r requirements-local.txt
    pause
    exit /b 1
)

rem Old piper-phonemize Windows wheels contain a build-machine path such as
rem D:/a/piper1-gpl/... and fail to locate phontab on another PC.  Verify the
rem actual bundled data file, not merely whether `import piper` succeeds.
"%VENV_PYTHON%" -c "import pathlib,piper; p=pathlib.Path(piper.__file__).resolve().parent/'espeak-ng-data'/'phontab'; raise SystemExit(0 if p.is_file() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [CapCap] Repairing legacy Piper TTS runtime...
    "%VENV_PYTHON%" -m pip uninstall -y piper-tts piper-phonemize >nul 2>&1
    "%VENV_PYTHON%" -m pip install --no-cache-dir "piper-tts>=1.7.0"
    if errorlevel 1 (
        echo.
        echo [CapCap] Piper TTS repair failed. Check the internet connection and try again.
        pause
        exit /b 1
    )
)

echo [CapCap] Starting local client with the project venv...
"%VENV_PYTHON%" -m scripts.ollama_local --ensure
if errorlevel 1 (
    echo.
    echo [CapCap] Ollama local is not ready. Install Ollama, run "ollama signin", and pull the configured model.
    pause
    exit /b 1
)

"%VENV_PYTHON%" ui\gui.py

if errorlevel 1 (
    echo.
    echo [CapCap] Local client exited with an error.
    pause
)
