@echo off
setlocal
cd /d "%~dp0"
set "SOLCAST_USE_TURSO=0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto setup_failed
)

if not exist ".venv\Scripts\streamlit.exe" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto setup_failed
)

".venv\Scripts\python.exe" -c "import turso_serverless" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto setup_failed
)

".venv\Scripts\python.exe" -m streamlit run app.py
exit /b %errorlevel%

:setup_failed
echo Could not set up the local Python environment. Install Python 3 and try again.
pause
exit /b 1
