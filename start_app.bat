@echo off
title StoryDirector-VA Frontend (port 7766)
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

echo [START] StoryDirector-VA frontend at http://localhost:7766/app.html
echo [INFO]  LLM backend: http://llamaserver.com:10011  (qwen3.8-27b)
echo [INFO]  Press Ctrl+C to stop.

REM ===== Open browser =====
start "" http://localhost:7766/app.html

REM ===== Serve on port 7766 using the dedicated venv =====
"venv\Scripts\python.exe" -m http.server 7766 --bind 0.0.0.0
pause
