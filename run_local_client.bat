@echo off
setlocal
cd /d "%~dp0"

echo [CapCap] Starting local client...
python -m scripts.ollama_local --ensure
if errorlevel 1 (
    echo.
    echo [CapCap] Ollama local is not ready. Install Ollama, run "ollama signin", and pull the configured model.
    pause
    exit /b 1
)

python ui\gui.py

if errorlevel 1 (
    echo.
    echo [CapCap] Local client exited with an error.
    pause
)
