@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python backtest_history.py --months 12
pause
