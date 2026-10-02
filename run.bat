@echo off
title catcam
cd /d "%~dp0"
if not exist config.ini (
  copy config.example.ini config.ini >nul
  echo Created config.ini - edit it to set your camera url and passcode, then run this again.
  notepad config.ini
  exit /b 1
)
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg not found - installing it with winget...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*") do for /d %%E in ("%%D\ffmpeg-*") do set "PATH=%PATH%;%%E\bin"
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Could not find or install ffmpeg. Install it manually, then run this again.
  pause
  exit /b 1
)

if exist venv\Scripts\python.exe goto run
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY if exist "%USERPROFILE%\miniconda3\python.exe" set "PY="%USERPROFILE%\miniconda3\python.exe""
if not defined PY goto nopython
echo Setting up Python environment...
%PY% -m venv venv
venv\Scripts\python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

:run
venv\Scripts\python catcam.py
pause
exit /b

:nopython
echo Python not found. Install it from python.org, then run this again.
pause
exit /b 1
pause
