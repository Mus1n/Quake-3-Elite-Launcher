@echo off
setlocal

cd /d "%~dp0"

set "PYTHONW=%~dp0python\pyenv\venv\Scripts\pythonw.exe"
set "LAUNCHER=%~dp0modules\launch.pyw"

if not exist "%PYTHONW%" (
    echo ERROR: Q3Elite Python environment not found.
    echo.
    echo Expected:
    echo %PYTHONW%
    echo.
    echo Run Q3Elite_Setup.bat first.
    pause
    exit /b 1
)

if not exist "%LAUNCHER%" (
    echo ERROR: Q3Elite launcher module not found.
    echo.
    echo Expected:
    echo %LAUNCHER%
    pause
    exit /b 1
)

start "" "%PYTHONW%" "%LAUNCHER%"

exit /b 0