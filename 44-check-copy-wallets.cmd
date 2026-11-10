@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python copy_wallet_check.py
pause
