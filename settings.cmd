@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1" -Settings -Notify
if errorlevel 1 (
    pause
    exit /b 1
)
