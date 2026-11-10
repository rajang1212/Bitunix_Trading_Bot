@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python learning_cli.py export
pause
