@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Self Check
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" goto no_env
if /i "%DSWECHAT_SKIP_ELEVATE%"=="1" goto run

net session >nul 2>&1
if errorlevel 1 goto need_admin
goto run

:need_admin
echo Admin rights required. A UAC prompt will appear - please click YES.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:no_env
echo Python environment not found. Please run install.bat first.
pause
exit /b

:run
".venv\Scripts\python.exe" bot.py --check
echo.
pause
