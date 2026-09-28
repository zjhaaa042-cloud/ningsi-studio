@echo off
rem ============================================================
rem  Ningsi Studio launcher (double-click friendly)
rem
rem  Double-click   -> menu, and this window stays open
rem  From a console -> run.bat demo | test | check | doctor | smoke | serve
rem
rem  Everything is also appended to:
rem      %TEMP%\ningsi-studio-run.log
rem  (the console output is NOT buffered through the log, so the
rem   service URL and progress lines appear immediately)
rem ============================================================
setlocal EnableExtensions
set "HERE=%~dp0"
set "LOG=%TEMP%\ningsi-studio-run.log"
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=menu"

rem Find a PowerShell: prefer pwsh 7, fall back to Windows PowerShell
set "PS="
for %%C in (pwsh.exe powershell.exe) do (
    if not defined PS (
        for /f "delims=" %%P in ('where %%C 2^>nul') do (
            if not defined PS set "PS=%%P"
        )
    )
)
if not defined PS (
    echo.
    echo   [x] Windows PowerShell was not found on this machine.
    echo       Install PowerShell 7 from https://aka.ms/powershell and try again.
    echo.
    pause
    endlocal & exit /b 3
)

rem Strip the trailing backslash from the folder path and pass -File,
rem so no command-line quoting/escaping is involved.
set "SCRIPT=%HERE%run.ps1"
set "WORKDIR=%HERE:~0,-1%"

rem ------------------------------------------- double-click: keep window open
if /i "%ACTION%"=="menu" goto :menu

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -Action %ACTION% %2 %3 %4
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo   [x] launcher exited with code %CODE%.
    echo       Re-run with:  run.bat menu
    echo       Log file:     "%LOG%"
    echo.
    pause
)
endlocal & exit /b %CODE%

:menu
echo.
echo   Starting Ningsi Studio launcher ...  (this window will stay open)
echo.
"%PS%" -NoExit -NoProfile -ExecutionPolicy Bypass -Command "Start-Transcript -Path '%LOG%' -Append | Out-Null; Set-Location -LiteralPath '%WORKDIR%'; & '%SCRIPT%' -Action menu; Stop-Transcript | Out-Null"
set "CODE=%ERRORLEVEL%"
endlocal & exit /b %CODE%
