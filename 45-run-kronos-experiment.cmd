@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM ============================================================================
REM  EDIT THE NEXT LINE: full path to your Kronos-master folder -- the one that
REM  CONTAINS the "model" folder and forecast_bitunix.py. Do NOT add \model.
REM  Example: set KRONOS_DIR=C:\Meme\Day_TR\Kronos-master
REM ============================================================================
set KRONOS_DIR=C:\Meme\Day_TR\Kronos-master

if not exist "%KRONOS_DIR%\model\__init__.py" (
  echo.
  echo ERROR: Kronos not found at "%KRONOS_DIR%".
  echo Expected to find "%KRONOS_DIR%\model\__init__.py".
  echo Open 45-run-kronos-experiment.cmd in Notepad and set KRONOS_DIR to the
  echo Kronos-master folder ^(the one that CONTAINS the "model" folder^), with no
  echo trailing \model, then run this again.
  echo.
  pause & exit /b 1
)

echo Installing forecast dependencies into the app venv if needed (first time only)...
pip install --prefer-binary --only-binary=numpy,pandas,torch,safetensors numpy pandas torch safetensors einops huggingface_hub

echo.
echo Running the backtest: your live strategy vs. the same strategy with the Kronos filter.
echo First run is slow (it downloads the model once and forecasts at every proposed trade,
echo roughly 1-3 hours). It caches every forecast, so re-runs are fast.
echo.
python backtest_experiments.py
pause
