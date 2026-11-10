@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
REM Tests universe / pair-list filters (age filter, volume floor) vs your current strategy, both halves.
REM Independent of the Kronos experiment -- safe to run while launcher 45 is going.
REM Add --days N for a shorter window, e.g.  47-run-filter-experiments.cmd --days 180
python backtest_filters.py %*
pause
