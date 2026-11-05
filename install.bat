@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Install - DeepSeek WeChat Bot
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ================================================
echo   Install (run once)
echo ================================================
echo.

echo [1/3] Creating Python virtual environment...
if exist ".venv\Scripts\python.exe" goto venv_ok

py -3.12 -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto venv_ok
py -3 -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto venv_ok
python -m venv .venv

:venv_ok
if not exist ".venv\Scripts\python.exe" goto no_python
echo     done

echo.
echo [2/3] Installing dependencies (1-3 minutes)...
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 ".venv\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pip_failed

echo.
echo [3/3] Verify...
".venv\Scripts\python.exe" -c "import wechatauto, requests, numpy; print('deps OK: wechatauto', wechatauto.__version__)"

echo.
echo ================================================
echo   Done!
echo   1. Open config.ini, fill api_key
echo   2. Login WeChat
echo   3. Double click start.bat
echo ================================================
pause
exit /b

:no_python
echo.
echo ERROR: Python not found.
echo Install Python 3.12 from python.org (check "Add to PATH"), then rerun.
pause
exit /b

:pip_failed
echo.
echo pip install failed. Try a mirror:
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pause
exit /b
