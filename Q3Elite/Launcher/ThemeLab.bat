@echo off
setlocal
set "PY=%APPDATA%\Python\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Q3Elite Python environment not found: %PY%
  pause
  exit /b 2
)
set "Q3ELITE_UI_DEV=1"
set "Q3ELITE_THEME=q3elite-v17-obsidian"
"%PY%" "%~dp0modules\ThemeLab.pyw"
