@echo off
title EBG WhatsApp Agent Server
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python -m pip install -q -r requirements.txt
    python server.py --open
) else (
    py -m pip install -q -r requirements.txt
    py server.py --open
)
echo.
echo The server has stopped. Close this window, or press any key to exit.
pause >nul
