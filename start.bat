@echo off
rem ============================================================
rem  Minimal launcher: starts the service with plain cmd + python.
rem  No PowerShell involved, so nothing can be blocked by the
rem  execution policy or a script parse error.
rem
rem  Usage:  start.bat [port]
rem  Then open:  http://127.0.0.1:<port>/    (default port 8765)
rem ============================================================
setlocal EnableExtensions
set "ACTION=%~1"
set "PORT=8765"
if not "%ACTION%"=="" set "PORT=%ACTION%"

set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
cd /d "%HERE%"

set "PYTHONPATH=%HERE%\src;%HERE%\..\ningsi\src;%HERE%\..\ningsi\ningsi\src"
set "PYTHONIOENCODING=utf-8"

set "PY=python"
where python >nul 2>&1
if errorlevel 1 set "PY=py"

echo.
echo   Ningsi Studio - starting service on port %PORT%
echo   project: %HERE%
echo   python : %PY%
echo.
echo   Open this in your browser once it says "service ready":
echo       http://127.0.0.1:%PORT%/
echo   Stop the service with Ctrl+C.
echo.

"%PY%" -m ningsi_studio serve --port %PORT%
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
    echo   [x] the service exited with code %CODE%.
    echo.
    echo   Common causes:
    echo     - the engine package is missing:  pip install -e ..\ningsi
    echo     - numpy is missing:               pip install numpy
    echo     - the port is already in use:     start.bat 8790
    echo.
)
pause
endlocal & exit /b %CODE%
