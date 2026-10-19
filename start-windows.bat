@echo off
rem Double-click to start the knowledge base on Windows.
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 start.py) else (python start.py)
if %errorlevel%==9009 echo [rego] Python not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
