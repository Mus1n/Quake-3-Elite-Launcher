@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_DIR=%~dp0python"
set "PYTHON=%PYTHON_DIR%\pyenv\venv\Scripts\python.exe"
set "PYTHONW=%PYTHON_DIR%\pyenv\venv\Scripts\pythonw.exe"
set "SETUP=%PYTHON_DIR%\setup_python.bat"
set "LAUNCHER=%~dp0modules\launch.pyw"

if not exist "%LAUNCHER%" (
    echo ERROR: Q3Elite launcher module not found.
    echo.
    echo Expected:
    echo %LAUNCHER%
    pause
    exit /b 1
)

:: Fast normal launch only when the environment is complete.
if exist "%PYTHON%" if exist "%PYTHONW%" (
    "%PYTHON%" -c "import PyQt6; import vulkan" >nul 2>&1
    if not errorlevel 1 (
        start "" "%PYTHONW%" "%LAUNCHER%"
        exit /b 0
    )
)

:: Missing/broken environment: let setup_python repair it.
echo Q3Elite Python environment is missing or incomplete.
echo Repairing environment...
echo.

if not exist "%SETUP%" (
    echo ERROR: setup_python.bat not found.
    echo.
    echo Expected:
    echo %SETUP%
    pause
    exit /b 1
)

call "%SETUP%" "%LAUNCHER%"
exit /b %ERRORLEVEL%
