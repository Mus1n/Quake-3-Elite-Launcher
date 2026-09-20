@echo off
setlocal EnableExtensions

title Quake 3 Elite Setup

:: ============================================================
:: CONFIG
:: ============================================================

set "ROOT=%~dp0"
set "INSTALL_DIR=%ROOT%Q3Elite"
set "LAUNCHER=%INSTALL_DIR%\Launcher"

set "REPO_ZIP=https://github.com/Mus1n/Quake-3-Elite-Launcher/archive/refs/heads/main.zip"

set "TEMP_DIR=%TEMP%\Q3Elite_Setup"
set "ZIP_FILE=%TEMP_DIR%\Q3Elite.zip"
set "EXTRACT_DIR=%TEMP_DIR%\Extracted"

:: ============================================================
:: HEADER
:: ============================================================

echo.
echo ============================================================
echo                  QUAKE 3 ELITE SETUP
echo ============================================================
echo.

:: ============================================================
:: CLEAN TEMP DIRECTORY
:: ============================================================

if exist "%TEMP_DIR%" (
    rmdir /s /q "%TEMP_DIR%"
)

mkdir "%TEMP_DIR%" >nul 2>&1

if errorlevel 1 (
    echo ERROR: Could not create temporary directory.
    pause
    exit /b 1
)

:: ============================================================
:: CHECK EXISTING INSTALLATION
:: ============================================================

if exist "%LAUNCHER%\launch.bat" (
    echo Existing Q3Elite Launcher detected.
    echo Skipping launcher download.
    echo.
    goto INSTALL
)

:: ============================================================
:: DOWNLOAD GITHUB REPOSITORY
:: ============================================================

echo Downloading Q3Elite Launcher...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%ZIP_FILE%' -UseBasicParsing -ErrorAction Stop } catch { Write-Host $_.Exception.Message; exit 1 }"

if errorlevel 1 (
    echo.
    echo ERROR: Failed to download Q3Elite Launcher.
    echo.
    pause
    exit /b 1
)

if not exist "%ZIP_FILE%" (
    echo.
    echo ERROR: Downloaded archive was not found.
    pause
    exit /b 1
)

echo Download complete.
echo.

:: ============================================================
:: EXTRACT REPOSITORY
:: ============================================================

echo Extracting Q3Elite Launcher...
echo.

mkdir "%EXTRACT_DIR%" >nul 2>&1

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Expand-Archive -LiteralPath '%ZIP_FILE%' -DestinationPath '%EXTRACT_DIR%' -Force -ErrorAction Stop } catch { Write-Host $_.Exception.Message; exit 1 }"

if errorlevel 1 (
    echo.
    echo ERROR: Failed to extract Q3Elite Launcher.
    pause
    exit /b 1
)

:: GitHub ZIP structure:
::
:: Quake-3-Elite-Launcher-main\
::     Q3Elite\
::         Launcher\
::

set "SOURCE=%EXTRACT_DIR%\Quake-3-Elite-Launcher-main\Q3Elite"

if not exist "%SOURCE%\Launcher\launch.bat" (
    echo.
    echo ERROR: Invalid Q3Elite package.
    echo Expected:
    echo "%SOURCE%\Launcher\launch.bat"
    echo.
    pause
    exit /b 1
)

:: ============================================================
:: COPY Q3ELITE
:: ============================================================

echo Installing Q3Elite files...
echo.

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

robocopy "%SOURCE%" "%INSTALL_DIR%" /E /NFL /NDL /NJH /NJS /NP >nul

set "ROBO_RESULT=%ERRORLEVEL%"

if %ROBO_RESULT% GEQ 8 (
    echo.
    echo ERROR: Failed to install Q3Elite files.
    echo Robocopy error code: %ROBO_RESULT%
    pause
    exit /b %ROBO_RESULT%
)

:: ============================================================
:: CLEAN TEMP FILES
:: ============================================================

echo Cleaning temporary files...

rmdir /s /q "%TEMP_DIR%" >nul 2>&1

echo.
echo Launcher files installed successfully.
echo.

:: ============================================================
:: INSTALL
:: ============================================================

:INSTALL

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
set "ICON=%LAUNCHER%\assets\icons\favicon.ico"
set "WORKDIR=%LAUNCHER%"


:: ============================================================
:: CREATE DESKTOP SHORTCUT
:: ============================================================

echo Creating desktop shortcut...

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%DESKTOP_SHORTCUT%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%WORKDIR%'; if (Test-Path '%ICON%') { $s.IconLocation = '%ICON%' }; $s.Save()"

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
    "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%LOCAL_SHORTCUT%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%WORKDIR%'; if (Test-Path '%ICON%') { $s.IconLocation = '%ICON%' }; $s.Save()"

if exist "%LOCAL_SHORTCUT%" (
    echo OK: Local shortcut created.
) else (
    echo WARNING: Local shortcut was not created.
)

:: ============================================================
:: FIRST LAUNCH / PYTHON ENVIRONMENT
:: ============================================================

cd /d "%LAUNCHER%"

echo.
echo ============================================================
echo Starting Q3Elite Launcher installation...
echo ============================================================
echo.

call ".\python\setup_python.bat" ".\modules\launch.pyw"

set "SETUP_RESULT=%ERRORLEVEL%"

if not "%SETUP_RESULT%"=="0" (
    echo.
    echo ERROR: Q3Elite environment setup failed.
    echo Error code: %SETUP_RESULT%
    echo.
    pause
    exit /b %SETUP_RESULT%
)

exit /b 0