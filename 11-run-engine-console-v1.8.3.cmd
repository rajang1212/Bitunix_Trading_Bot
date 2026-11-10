@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python -c "from runtime_control import prepare_start; prepare_start()"
python multiagent.py run
pause
