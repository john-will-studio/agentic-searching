@echo off
rem Lucid Warrant launcher for Windows. Double-click to start.
rem First run: creates a private Python environment next to this file and installs Lucid Warrant into it.
setlocal
rem pushd, not cd: cmd can't use a network (UNC) folder as its current folder, so "cd /d" failed there and setup
rem ran in the wrong folder; pushd maps a drive letter for it. No popd: the app keeps running from that drive.
pushd "%~dp0" || goto :nofolder
if not exist ".venv\Scripts\pythonw.exe" (
  echo Setting up Lucid Warrant for the first time. This takes a minute...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" goto :nopython
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :failed
)
start "" ".venv\Scripts\pythonw.exe" -m lucid_warrant.gui
exit /b 0

:nofolder
echo.
echo Can't open the folder this file is in (%~dp0). Copy the Lucid Warrant folder to this computer and try again.
pause
exit /b 1

:nopython
echo.
echo Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then double-click this file again.
pause
exit /b 1

:failed
echo.
echo Setup failed (see the messages above). Check your internet connection and try again.
echo To start over, delete the .venv folder next to this file.
pause
exit /b 1
