@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
REM Runs the present strategy under real AND stricter (2x slippage) costs, and shows
REM exactly where it loses. Add --days N for a shorter window, e.g.  46-diagnose-strategy.cmd --days 60
python backtest_diagnose.py %*
pause
