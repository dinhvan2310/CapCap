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
