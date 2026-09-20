@echo off
setlocal enabledelayedexpansion

set "BASE_DIR=%~dp0"
set "APP_ROOT=%APPDATA%\Quake 3 Elite"
set "PY_ROOT=%APP_ROOT%\Python"
set "CACHE_ROOT=%APP_ROOT%\Cache\Python"
set "UV_EXE=%PY_ROOT%\uv.exe"
set "VENV_DIR=%PY_ROOT%\venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PYTHONW_EXE=%VENV_DIR%\Scripts\pythonw.exe"
set "REQUIREMENTS=%BASE_DIR%requirements.txt"
set "PYTHON_VERSION=3.12"
set "UV_ZIP_URL=https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"

set "UV_CACHE_DIR=%CACHE_ROOT%\uv"
set "UV_PYTHON_INSTALL_DIR=%PY_ROOT%\runtime"
set "UV_TOOL_DIR=%PY_ROOT%\tools"
set "UV_TOOL_BIN_DIR=%PY_ROOT%\bin"
set "UV_PYTHON_BIN_DIR=%PY_ROOT%\bin"
set "UV_NO_MODIFY_PATH=1"
set "UV_PYTHON_PREFERENCE=only-managed"

if not exist "%APP_ROOT%" mkdir "%APP_ROOT%"
if not exist "%PY_ROOT%" mkdir "%PY_ROOT%"
if not exist "%CACHE_ROOT%" mkdir "%CACHE_ROOT%"

:: Healthy persistent environment -> no Python/PyQt reinstall.
if exist "%PYTHON_EXE%" if exist "%PYTHONW_EXE%" (
    "%PYTHON_EXE%" -c "import PyQt6; import PyQt6.QtWebEngineWidgets; import vulkan" >nul 2>&1
    if not errorlevel 1 (
        echo Existing Q3Elite Python environment is ready.
        goto START_LAUNCHER
    )
    echo Existing Python environment is incomplete.
    echo Repairing dependencies...
    echo.
)

if not exist "%UV_EXE%" (
    echo Downloading uv...
    set "UV_ZIP=%CACHE_ROOT%\uv.zip"

    powershell -NoProfile -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%UV_ZIP_URL%' -OutFile '!UV_ZIP!'"

    if !ERRORLEVEL! neq 0 (
        echo ERROR: failed to download uv.
        pause
        exit /b 1
    )

    powershell -NoProfile -Command "Expand-Archive -Path '!UV_ZIP!' -DestinationPath '%PY_ROOT%' -Force"
    del "!UV_ZIP!" >nul 2>&1

    if not exist "%UV_EXE%" (
        echo ERROR: uv.exe not found after extraction.
        pause
        exit /b 1
    )
)

echo Installing/verifying Python %PYTHON_VERSION%...
"%UV_EXE%" python install %PYTHON_VERSION% --no-registry
if !ERRORLEVEL! neq 0 (
    echo ERROR: failed to install Python.
    pause
    exit /b 1
)

if not exist "%PYTHON_EXE%" (
    echo Creating persistent virtual environment...
    "%UV_EXE%" venv "%VENV_DIR%" --python %PYTHON_VERSION%
    if !ERRORLEVEL! neq 0 (
        echo ERROR: failed to create venv.
        pause
        exit /b 1
    )
)

if exist "%REQUIREMENTS%" (
    echo Installing/verifying dependencies...
    "%UV_EXE%" pip install --python "%PYTHON_EXE%" --link-mode copy -r "%REQUIREMENTS%"
    if !ERRORLEVEL! neq 0 (
        echo ERROR: failed to install dependencies.
        pause
        exit /b 1
    )
)

echo Verifying Python dependencies...
"%PYTHON_EXE%" -c "import PyQt6; import PyQt6.QtWebEngineWidgets; import vulkan"
if !ERRORLEVEL! neq 0 (
    echo.
    echo ERROR: Q3Elite Python dependencies are incomplete.
    echo.
    pause
    exit /b 1
)

:START_LAUNCHER
if "%~1"=="" (
    echo ERROR: no launcher script was supplied.
    pause
    exit /b 1
)

for %%I in ("%~1") do set "LAUNCHER_SCRIPT=%%~fI"
if not exist "%LAUNCHER_SCRIPT%" (
    echo ERROR: launcher script not found:
    echo "%LAUNCHER_SCRIPT%"
    pause
    exit /b 1
)

echo.
echo Starting Q3Elite Launcher...
start "" "%PYTHONW_EXE%" "%LAUNCHER_SCRIPT%"
exit /b 0
