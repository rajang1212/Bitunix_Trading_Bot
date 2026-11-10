@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python probe_history.py
pause
