@echo off
setlocal

:: =========================
:: ADMIN CHECK + ELEVATION
:: =========================
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: =========================
:: DEFINE PATHS
:: =========================
set "IconPath=%~dp0Q3Elite\Icons\d1.ico"
set "Icon2Path=%~dp0Q3Elite\Icons\a2.ico"
set "StartInPath=%~dp0Q3Elite\Engines"

:: =========================
:: RESHADE INSTALL
:: =========================
set "SRC=%~dp0Q3Elite\ReShade\Program"
set "DST=%ProgramData%\ReShade"
set "GAME=%~dp0Q3Elite\Engines\XQ3E_Vulkan.x64.exe"

echo Installing ReShade...

if not exist "%DST%" mkdir "%DST%"
xcopy "%SRC%\*" "%DST%\" /E /I /Y >nul

reg add "HKLM\SOFTWARE\Khronos\Vulkan\ImplicitLayers" /v "%DST%\ReShade64.json" /t REG_DWORD /d 0 /f >nul

reg add "HKLM\SOFTWARE\WOW6432Node\Khronos\Vulkan\ImplicitLayers" /v "%DST%\ReShade32.json" /t REG_DWORD /d 0 /f >nul

:: =========================
:: UPDATE ReShadeApps.ini
:: =========================
set "INI=%DST%\ReShadeApps.ini"

if not exist "%INI%" (
(
echo [GENERAL]
echo Apps=%GAME%
)> "%INI%"
) else (
    findstr /I /C:"%GAME%" "%INI%" >nul
    if errorlevel 1 (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$ini='%INI%';$c=Get-Content $ini -Raw;if($c -match 'Apps=(.*)'){$apps=$matches[1].Trim();if($apps){$apps+=';'};$apps+='%GAME%';$c=[regex]::Replace($c,'Apps=.*','Apps='+$apps)}else{$c+='`r`nApps=%GAME%'};Set-Content $ini $c"
    )
)

:: =========================
:: SHORTCUT DEFINITIONS
:: =========================
set "Shortcut1Name=Q3Elite\Engines\Q3Elite (OpenGL) - Better Compatibility.lnk"
set "Shortcut1Target=%~dp0Q3Elite\Engines\Q3Elite (OpenGL) - Better Compatibility.bat"

set "Shortcut2Name=Q3Elite (Vulkan) - Cinematic.lnk"
set "Shortcut2Target=%~dp0Q3Elite\Engines\Q3Elite (Vulkan) - Cinematic.bat"

set "Shortcut3Name=Updater OSP2-BE.lnk"
set "Shortcut3Target=%~dp0Q3Elite\Engines\Updater OSP2-BE.bat"

:: =========================
:: CREATE SHORTCUTS
:: =========================
call :CreateShortcut "%Shortcut1Name%" "%Shortcut1Target%" "%IconPath%" "%StartInPath%"
call :CreateShortcut "%Shortcut2Name%" "%Shortcut2Target%" "%IconPath%" "%StartInPath%"
call :CreateShortcut "%Shortcut3Name%" "%Shortcut3Target%" "%Icon2Path%" "%StartInPath%"

echo.
echo Shortcuts created successfully.

:: =========================
:: HIDE BATCH FILES
:: =========================
attrib +h "%Shortcut1Target%"
attrib +h "%Shortcut2Target%"
attrib +h "%Shortcut3Target%"

:: =========================
:: DELETE FIRSTLAUNCH
:: =========================
del "%~f0"

pause
exit /b


:CreateShortcut
setlocal

set "ShortcutName=%~1"
set "TargetPath=%~2"
set "IconPath=%~3"
set "StartInPath=%~4"

echo Creating shortcut "%ShortcutName%"...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%~dp0%ShortcutName%');$s.TargetPath='%TargetPath%';$s.IconLocation='%IconPath%';$s.WorkingDirectory='%StartInPath%';$s.Save()"

endlocal
exit /b