@echo off
rem 中文名入口：实际逻辑在 start.bat（英文名，避免提权后路径乱码）
call "%~dp0start.bat" %*
