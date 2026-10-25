@echo off
setlocal

if exist "%~dp0SolcastDashboard.exe" (
    start "" "%~dp0SolcastDashboard.exe"
    exit /b 0
)

if exist "%~dp0dist\SolcastDashboard.exe" (
    start "" "%~dp0dist\SolcastDashboard.exe"
    exit /b 0
)

echo SolcastDashboard.exe was not found.
echo Build it with: python -m PyInstaller --clean solcast_dashboard.spec
pause
exit /b 1
