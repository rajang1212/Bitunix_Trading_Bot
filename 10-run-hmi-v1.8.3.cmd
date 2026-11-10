@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Virtual environment not found. Run 00-install.cmd first.
  pause
  exit /b 1
)
start "Bitunix Swing + Copy Trader v1.8.3" ".venv\Scripts\pythonw.exe" hmi.py
