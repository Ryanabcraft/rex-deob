@echo off
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=.venv\bin\python.exe"
if not exist "%PY%" (
  echo venv missing - run install.ps1 first
  pause
  exit /b 1
)
"%PY%" bot.py
pause
