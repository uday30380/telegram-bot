@echo off
setlocal
cd /d "%~dp0"
title StudyMate Elite Nexus - Watchdog Mode

:loop
echo [%date% %time%] 🛡️ Monitoring StudyMate Nexus Engine...

echo [SYSTEM] Launching Engine...
py bot.py
set EXIT_CODE=%ERRORLEVEL%
echo [SYSTEM] Engine stopped with exit code %EXIT_CODE%

if %EXIT_CODE% equ 0 (
    echo [SYSTEM] Clean shutdown or instance already running. Exiting watchdog.
    exit /b 0
)

echo [%date% %time%] ⚠️ Engine stopped or crashed. Restarting in 5 seconds...
timeout /t 5 >nul
goto loop
