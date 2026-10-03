@echo off
REM Job Scout - double-click to run. First run sets everything up (a minute or two).
setlocal
cd /d "%~dp0"

REM 1. Find Python 3.10+ (the "py" launcher first, then "python").
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo.
  echo  Python is not installed. Get it from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

REM 2. A private environment for this app, so nothing touches your system Python.
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Job Scout for the first time...
  %PY% -m venv .venv || (echo Could not create the environment. & pause & exit /b 1)
)

REM 3. Install or update dependencies when requirements.txt has changed.
fc /b requirements.txt ".venv\installed.txt" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (echo Install failed - check your internet connection. & pause & exit /b 1)
  copy /y requirements.txt ".venv\installed.txt" >nul
)

REM 4. Your own settings, made once from the starter file.
if not exist "config.yaml" copy "config.example.yaml" "config.yaml" >nul

REM 5. Open the browser once the server is up, then run it.
echo.
echo  Job Scout is starting at http://127.0.0.1:5000
echo  Keep this window open while you use it. Close it to stop the app.
echo.
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:5000"
".venv\Scripts\python.exe" app.py
pause
