@echo off
cd /d "%~dp0"
echo Starting server, please wait...
start "" cmd /c "timeout /t 2 >nul && start http://localhost:5000/"
py app.py
