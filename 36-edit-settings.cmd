@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python daytrader.py status >nul
notepad "%LOCALAPPDATA%\BitunixDayTraderV1\settings.json"
