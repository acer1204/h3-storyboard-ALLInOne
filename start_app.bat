@echo off
title H3 Storyboard WebUI (port 9998)
cd /d "%~dp0"

REM ===== Create dedicated Python venv on first run =====
if not exist "venv\Scripts\python.exe" (
    echo [SETUP] Creating dedicated venv ...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Python not found. Please install Python 3 first.
        pause
        exit /b 1
    )
)

echo [START] H3 Storyboard WebUI at http://localhost:9998/
echo [INFO]  llama-server / ComfyUI addresses are in config.json (editable in the web UI)
echo [INFO]  Press Ctrl+C to stop.

start "" http://localhost:9998/

"venv\Scripts\python.exe" h3-server.py
pause
