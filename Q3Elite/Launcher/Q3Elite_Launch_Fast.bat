@echo off
setlocal EnableExtensions

rem ============================================================
rem Q3Elite Launcher - FAST START
rem Normal startup deliberately avoids Python import checks.
rem Dependency validation/repair remains in setup_python.bat.
rem ============================================================

cd /d "%~dp0"

set "PYTHON_DIR=%APPDATA%\Python"
set "PYTHON=%PYTHON_DIR%\venv\Scripts\python.exe"
set "PYTHONW=%PYTHON_DIR%\venv\Scripts\pythonw.exe"
set "SETUP=%~dp0modules\python\setup_python.bat"
set "LAUNCHER=%~dp0modules\launch.pyw"

if not exist "%LAUNCHER%" (
    echo ERROR: Q3Elite launcher module not found:
    echo "%LAUNCHER%"
    pause
    exit /b 1
)

rem Fast path: if the persistent GUI interpreter exists, start immediately.
rem Do not import PyQt6/WebEngine/Vulkan on every normal launch.
if exist "%PYTHONW%" (
    start "" "%PYTHONW%" "%LAUNCHER%"
    exit /b 0
)

rem Missing environment: use the existing bootstrap/repair procedure.
if not exist "%SETUP%" (
    echo ERROR: Python setup script not found:
    echo "%SETUP%"
    pause
    exit /b 1
)

call "%SETUP%" "%LAUNCHER%"
exit /b %ERRORLEVEL%
