@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
REM Rolling-window robustness of your installed strategy: descriptive historical consistency, not independent validation.
REM Independent of Kronos/filter runs. Defaults: 2-month blocks, 90-day rolling windows.
python backtest_walkforward.py %*
pause
