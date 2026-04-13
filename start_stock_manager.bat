@echo off
echo Starting Stockinput Automation Manager...
echo Connect your phone to: http://192.168.224.14:8001
echo Opening Management Dashboard...
start "" "http://localhost:8001"
echo Logging errors to startup_log.txt...
python -m app.main_app > startup_log.txt 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo ❌ ERROR: The program failed to start.
    echo Check startup_log.txt for details.
    type startup_log.txt
)
pause
