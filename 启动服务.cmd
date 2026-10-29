@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Agent service launcher (MySQL + FastAPI :8000)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [x] venv interpreter not found:
    echo     %~dp0.venv\Scripts\python.exe
    echo     Do NOT use D:\Anaconda3\python.exe - it lacks langchain_openai.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "start_local.py"
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo [ok] Service is up. This window can be closed.
) else (
    echo [x] Startup failed with code %RC%. See run.err.log in this folder.
)
echo.
echo Tip: the service keeps running after you close this window.
pause
exit /b %RC%
