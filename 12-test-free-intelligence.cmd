@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
echo Testing free market-intelligence sources...
echo.
python intelligence.py
pause
