@echo off
title H3 Storyboard WebUI (port 9998)
cd /d "%~dp0"

REM ===== venv health check =====
REM 整個資料夾複製到別台機器時，venv\pyvenv.cfg 還記著原本那台的 Python 路徑：
REM 檔案都在、但跑不起來。只看 python.exe 存不存在是抓不到的，要真的執行一次。
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [SETUP] venv unusable on this machine - rebuilding ...
        rmdir /s /q venv
    )
)

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
echo [INFO]  Cutout service log: h3-matte.log
echo [INFO]  Turn OFF this window's QuickEdit mode - clicking inside a QuickEdit
echo [INFO]  console pauses output and swallows the log (title bar - Properties - Options).
echo [INFO]  Press Ctrl+C to stop.

start "" http://localhost:9998/

"venv\Scripts\python.exe" h3-server.py
pause
