@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_DIR=%APPDATA%\Quake 3 Elite\Python"
set "PYTHON=%PYTHON_DIR%\venv\Scripts\python.exe"
set "PYTHONW=%PYTHON_DIR%\venv\Scripts\pythonw.exe"
set "SETUP=%~dp0python\setup_python.bat"
set "LAUNCHER=%~dp0modules\launch.pyw"

if not exist "%LAUNCHER%" (
    echo ERROR: Q3Elite launcher module not found.
    echo %LAUNCHER%
    pause
    exit /b 1
)

if exist "%PYTHON%" if exist "%PYTHONW%" (
    "%PYTHON%" -c "import PyQt6; import PyQt6.QtWebEngineWidgets; import vulkan" >nul 2>&1
    if not errorlevel 1 (
        start "" "%PYTHONW%" "%LAUNCHER%"
        exit /b 0
    )
)

echo Q3Elite Python environment is missing or incomplete.
echo Preparing persistent environment in:
echo %PYTHON_DIR%
echo.

if not exist "%SETUP%" (
    echo ERROR: setup_python.bat not found.
    pause
    exit /b 1
)

call "%SETUP%" "%LAUNCHER%"
exit /b %ERRORLEVEL%
