@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python scheduler_task.py remove
pause
