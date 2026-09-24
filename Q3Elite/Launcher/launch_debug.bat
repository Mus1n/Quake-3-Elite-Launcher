@echo off
setlocal

set "PYTHON=%APPDATA%\Python\venv\Scripts\python.exe"

rem Works whether this BAT is placed in Launcher\modules or Launcher itself.
if exist "%~dp0launch.pyw" (
    set "LAUNCH=%~dp0launch.pyw"
) else if exist "%~dp0modules\launch.pyw" (
    set "LAUNCH=%~dp0modules\launch.pyw"
) else (
    echo [ERROR] launch.pyw was not found.
    echo Checked:
    echo   "%~dp0launch.pyw"
    echo   "%~dp0modules\launch.pyw"
    echo.
    pause
    exit /b 2
)

if not exist "%PYTHON%" (
    echo [ERROR] Python environment was not found:
    echo   "%PYTHON%"
    echo.
    pause
    exit /b 3
)

"%PYTHON%" "%LAUNCH%"
set "EXITCODE=%ERRORLEVEL%"
echo.
echo Launcher exited with code %EXITCODE%.
pause
exit /b %EXITCODE%
