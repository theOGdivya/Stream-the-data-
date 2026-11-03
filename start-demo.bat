@echo off
rem Double-click this file to try the AQ test platform on this computer.
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python is not installed.
  echo Install Python 3.12 or newer from https://www.python.org/downloads/
  echo During setup, tick "Add python.exe to PATH". Then double-click this file again.
  pause
  exit /b 1
)
%PY% demo.py %*
echo.
echo The demo has stopped.
pause
