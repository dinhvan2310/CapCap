@echo off
setlocal
cd /d "%~dp0"

echo [CapCap] Starting local client...
set "PATH=%~dp0bin\ffmpeg;%PATH%"
"%~dp0.venv\Scripts\python.exe" "%~dp0ui\gui.py"

if errorlevel 1 (
    echo.
    echo [CapCap] Local client exited with an error.
    pause
)
