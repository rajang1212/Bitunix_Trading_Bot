@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python check_bar_history.py
pause
