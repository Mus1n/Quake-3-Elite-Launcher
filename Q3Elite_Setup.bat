@echo off
setlocal

:: ============================================================
:: Q3ELITE PATHS
:: ============================================================

set "ROOT=%~dp0"
set "LAUNCHER=%ROOT%Q3Elite\Launcher"

:: ============================================================
:: CHECK LAUNCHER
:: ============================================================

if not exist "%LAUNCHER%\launch.bat" (
    echo ERROR: Q3Elite Launcher not found.
    echo.
    echo Expected:
    echo "%LAUNCHER%\launch.bat"
    pause
    exit /b 1
)

:: ============================================================
:: PLATFORM CONFIG
:: ============================================================

if exist "%LAUNCHER%\mod_tree\branch.txt" (
    findstr /C:"windows" "%LAUNCHER%\mod_tree\branch.txt" >nul 2>&1

    if errorlevel 1 (
        echo windows>>"%LAUNCHER%\mod_tree\branch.txt"
    )
)

:: ============================================================
:: SHORTCUT PATHS
:: ============================================================

set "DESKTOP_SHORTCUT=%USERPROFILE%\Desktop\Q3Elite.lnk"
set "LOCAL_SHORTCUT=%ROOT%Q3Elite.lnk"

set "TARGET=%LAUNCHER%\launch.bat"
set "ICON=%LAUNCHER%\icons\b3.ico"
set "WORKDIR=%LAUNCHER%"

:: ============================================================
:: CREATE DESKTOP SHORTCUT
:: ============================================================

echo Creating desktop shortcut...

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
"$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut($env:DESKTOP_SHORTCUT); $s.TargetPath = $env:TARGET; $s.WorkingDirectory = $env:WORKDIR; if (Test-Path $env:ICON) { $s.IconLocation = $env:ICON }; $s.Save()"

if exist "%DESKTOP_SHORTCUT%" (
    echo OK: Desktop shortcut created.
) else (
    echo WARNING: Desktop shortcut was not created.
)

:: ============================================================
:: CREATE LOCAL SHORTCUT
:: ============================================================

echo Creating local shortcut...

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
"$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut($env:LOCAL_SHORTCUT); $s.TargetPath = $env:TARGET; $s.WorkingDirectory = $env:WORKDIR; if (Test-Path $env:ICON) { $s.IconLocation = $env:ICON }; $s.Save()"

if exist "%LOCAL_SHORTCUT%" (
    echo OK: Local shortcut created.
) else (
    echo WARNING: Local shortcut was not created.
)

:: ============================================================
:: FIRST LAUNCH / REPAIR PYTHON ENVIRONMENT
:: ============================================================

cd /d "%LAUNCHER%"

echo.
echo Starting Q3Elite setup...
echo.

call ".\python\setup_python.bat" ".\modules\flaunch.pyw"

set "SETUP_RESULT=%ERRORLEVEL%"

if not "%SETUP_RESULT%"=="0" (
    echo.
    echo ERROR: Q3Elite environment setup failed.
    echo Error code: %SETUP_RESULT%
    pause
    exit /b %SETUP_RESULT%
)

exit /b 0