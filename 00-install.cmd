@echo off
setlocal
cd /d "%~dp0"
echo Bitunix Multi-Agent Trader v1.8.3 HMI installer
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)
if not exist .venv (
  %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat || goto :fail
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || goto :fail
python -m unittest discover -s tests -v || goto :fail
python -m unittest test_kronos_filter -v || goto :fail
python scheduler_task.py install

echo.
echo Installation complete.
echo DAY: 5 slots x 500 USDT margin at 5x.
echo SWING: 5 slots x up to 1000 USDT allocation at 5x with risk-sized notional.
echo HMI refresh: 2 seconds. No visual heartbeat.
echo Market Intelligence: free public sources, no X subscription or API key required.
echo Daily DAY + SWING learning tests are scheduled for 21:00 local computer time.
echo Next: double-click 10-run-hmi-v1.8.3.cmd and press START BOT.
pause
exit /b 0
:fail
echo.
echo INSTALL FAILED. Read the error above.
pause
exit /b 1
