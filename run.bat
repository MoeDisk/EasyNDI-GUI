@echo off
REM EasyNDI GUI launcher (ASCII-only for cmd.exe)
cd /d "%~dp0"
py -3.13 app.py
if errorlevel 1 pause
