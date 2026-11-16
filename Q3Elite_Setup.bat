@echo off
setlocal EnableExtensions EnableDelayedExpansion

title Quake 3 Elite Setup

:: Use the folder containing this script as the install root by default.
:: Create the "Quake 3 Elite" wrapper only when launched directly from a
:: drive root (D:\) or the user's Desktop.
for %%I in ("%~dp0.") do set "CURRENT_DIR=%%~fI"
for %%I in ("%~dp0.") do set "DRIVE_ROOT=%%~dI\"
for /f "usebackq delims=" %%D in (`powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP_DIR=%%~fD"

set "TARGET_FOLDER=%CURRENT_DIR%"
if /I "%CURRENT_DIR%"=="%DRIVE_ROOT%" set "TARGET_FOLDER=%CURRENT_DIR%\Quake 3 Elite"
if defined DESKTOP_DIR if /I "%CURRENT_DIR%"=="%DESKTOP_DIR%" set "TARGET_FOLDER=%CURRENT_DIR%\Quake 3 Elite"

for %%I in ("%TARGET_FOLDER%") do set "TARGET_FOLDER=%%~fI"
for %%I in ("%~dp0.") do set "SCRIPT_DIR=%%~fI"

if /I not "%SCRIPT_DIR%"=="%TARGET_FOLDER%" (
    echo.
    echo Preparing setup in:
    echo "%TARGET_FOLDER%"
    if not exist "%TARGET_FOLDER%" mkdir "%TARGET_FOLDER%" >nul 2>&1
    if not exist "%TARGET_FOLDER%\" (
        echo ERROR: Could not create "%TARGET_FOLDER%".
        pause
        exit /b 1
    )
    set "TARGET_SCRIPT=%TARGET_FOLDER%\Q3Elite_Setup.bat"
    copy /Y "%~f0" "!TARGET_SCRIPT!" >nul
    if not exist "!TARGET_SCRIPT!" (
        echo ERROR: Could not copy setup script to the destination folder.
        pause
        exit /b 1
    )
    echo Setup copied. Restarting from the destination folder...
    start "Q3Elite Setup" "%ComSpec%" /d /k ""!TARGET_SCRIPT!""
    exit /b 0
)

set "ROOT=%TARGET_FOLDER%\"
set "INSTALL_DIR=%ROOT%Q3Elite"
set "LAUNCHER=%INSTALL_DIR%\Launcher"
set "REPO_ZIP=https://github.com/Mus1n/Quake-3-Elite-Launcher/archive/refs/heads/main.zip"
set "TEMP_DIR=%TEMP%\Q3Elite_Setup"
set "ZIP_FILE=%TEMP_DIR%\Q3Elite.zip"
set "EXTRACT_DIR=%TEMP_DIR%\Extracted"

echo.
echo ============================================================
echo                  QUAKE 3 ELITE SETUP
echo ============================================================
echo.

if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
mkdir "%TEMP_DIR%" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Could not create temporary directory.
    pause
    exit /b 1
)

if exist "%LAUNCHER%\launch.bat" if exist "%LAUNCHER%\modules\launch.pyw" if exist "%LAUNCHER%\modules\python\setup_python.bat" if exist "%LAUNCHER%\modules\python\requirements.txt" (
    echo Existing Q3Elite Launcher detected. Skipping launcher download.
    goto INSTALL
)

echo Downloading Q3Elite Launcher...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%ZIP_FILE%' -UseBasicParsing -ErrorAction Stop } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo ERROR: Failed to download Q3Elite Launcher.
    pause
    exit /b 1
)
if not exist "%ZIP_FILE%" (
    echo ERROR: Downloaded archive was not found.
    pause
    exit /b 1
)

mkdir "%EXTRACT_DIR%" >nul 2>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Expand-Archive -LiteralPath '%ZIP_FILE%' -DestinationPath '%EXTRACT_DIR%' -Force -ErrorAction Stop } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo ERROR: Failed to extract Q3Elite Launcher.
    pause
    exit /b 1
)

set "SOURCE=%EXTRACT_DIR%\Quake-3-Elite-Launcher-main\Q3Elite"
if not exist "%SOURCE%\Launcher\launch.bat" (
    echo ERROR: Invalid Q3Elite package. Expected:
    echo "%SOURCE%\Launcher\launch.bat"
    pause
    exit /b 1
)

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
robocopy "%SOURCE%" "%INSTALL_DIR%" /E /NFL /NDL /NJH /NJS /NP >nul
set "ROBO_RESULT=!ERRORLEVEL!"
if !ROBO_RESULT! GEQ 8 (
    echo ERROR: Failed to install Q3Elite files. Robocopy code: !ROBO_RESULT!
    pause
    exit /b !ROBO_RESULT!
)
rmdir /s /q "%TEMP_DIR%" >nul 2>&1

echo Launcher files installed successfully.

:INSTALL
if exist "%LAUNCHER%\mod_tree\branch.txt" (
    findstr /C:"windows" "%LAUNCHER%\mod_tree\branch.txt" >nul 2>&1
    if errorlevel 1 echo windows>>"%LAUNCHER%\mod_tree\branch.txt"
)

set "DESKTOP_SHORTCUT=%USERPROFILE%\Desktop\Q3Elite.lnk"
set "LOCAL_SHORTCUT=%ROOT%Q3Elite.lnk"
set "TARGET=%LAUNCHER%\launch.bat"
set "ICON=%LAUNCHER%\assets\icons\favicon.ico"
set "WORKDIR=%LAUNCHER%"

echo Creating shortcuts...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut('%DESKTOP_SHORTCUT%'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%WORKDIR%'; if(Test-Path '%ICON%'){$s.IconLocation='%ICON%'}; $s.Save()"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut('%LOCAL_SHORTCUT%'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%WORKDIR%'; if(Test-Path '%ICON%'){$s.IconLocation='%ICON%'}; $s.Save()"

if not exist "%LAUNCHER%\settings\backend.json" (
    echo Restoring Q3Elite Cloud configuration...
    if not exist "%LAUNCHER%\settings" mkdir "%LAUNCHER%\settings" >nul 2>&1
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ErrorActionPreference='Stop'; $u='https://raw.githubusercontent.com/Mus1n/Quake-3-Elite-Launcher/main/Q3Elite/Launcher/settings/backend.json'; try { Invoke-WebRequest -Uri $u -OutFile '%LAUNCHER%\settings\backend.json.tmp' -UseBasicParsing -TimeoutSec 15; $j=Get-Content -Raw '%LAUNCHER%\settings\backend.json.tmp' | ConvertFrom-Json; if(-not ($j.url -like 'https://*') -or -not ($j.publishable_key -or $j.anon_key)){throw 'Invalid backend config'}; Move-Item -Force '%LAUNCHER%\settings\backend.json.tmp' '%LAUNCHER%\settings\backend.json' } catch { Write-Host $_.Exception.Message; exit 1 }"
    if errorlevel 1 echo WARNING: Backend configuration could not be restored; account login may be unavailable.
)

if not exist "%LAUNCHER%\modules\python\setup_python.bat" (
    echo ERROR: Python bootstrap is missing.
    pause
    exit /b 1
)
if not exist "%LAUNCHER%\modules\launch.pyw" (
    echo ERROR: Launcher module is missing.
    pause
    exit /b 1
)

cd /d "%LAUNCHER%"
if errorlevel 1 (
    echo ERROR: Could not enter Launcher directory: "%LAUNCHER%"
    pause
    exit /b 1
)

call "%LAUNCHER%\modules\python\setup_python.bat" "%LAUNCHER%\modules\launch.pyw"
set "SETUP_RESULT=!ERRORLEVEL!"
if not "!SETUP_RESULT!"=="0" (
    echo ERROR: Q3Elite environment setup failed. Code: !SETUP_RESULT!
    pause
    exit /b !SETUP_RESULT!
)
echo.
echo Q3Elite setup completed.
pause
exit /b 0
