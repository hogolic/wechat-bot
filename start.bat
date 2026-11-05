@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DeepSeek x WeChat Bot
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist "logs" mkdir "logs"
echo [%date% %time%] launch start >> "logs\launch.log"

if not exist ".venv\Scripts\python.exe" goto no_env

if /i "%DSWECHAT_SKIP_ELEVATE%"=="1" goto run
net session >nul 2>&1
if errorlevel 1 goto need_admin
goto run

:need_admin
echo.
echo ============================================================
echo   Admin rights required (WeChat runs as administrator)
echo   A UAC prompt will appear - please click YES
echo ============================================================
echo.
echo [%date% %time%] not admin, trying elevate >> "logs\launch.log"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs" 2> "logs\elevate_error.txt"
if exist "logs\elevate_error.txt" goto elevate_failed
exit /b

:elevate_failed
echo.
echo [Elevation failed] Error:
type "logs\elevate_error.txt"
echo.
pause
exit /b

:no_env
echo.
echo Python environment not found. Please run install.bat first.
echo.
pause
exit /b

:run
echo [%date% %time%] admin ok, starting python >> "logs\launch.log"
echo Starting... first run needs 10-20 seconds to decrypt the database.
echo.
".venv\Scripts\python.exe" bot.py %*
echo.
echo Bot exited. If this was an error, send me the lines above.
echo [%date% %time%] python exited >> "logs\launch.log"
pause
