@echo off
rem Movie Filter launcher for Windows. Double-click to run.
setlocal
cd /d "%~dp0"

where ffmpeg >nul 2>nul || (
  echo Installing ffmpeg...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo Close this window and double-click this file again so Windows can find ffmpeg.
  pause
  exit /b
)

rem Supported Python versions: the speech recognizer has no builds for 3.15 or later yet.
set "CHECK=import sys; sys.exit(not (3, 12) <= sys.version_info[:2] <= (3, 14))"
set "PY="
for %%V in (3.13 3.12 3.14) do (
  if not defined PY (
    py -%%V -c "%CHECK%" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "%CHECK%" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo Movie Filter needs Python 3.12, 3.13 or 3.14. Python 3.15 and later are not supported yet.
  echo Install Python 3.13 from https://www.python.org/downloads/ - it can sit alongside other versions.
  pause
  exit /b 1
)

rem Rebuild the setup if it was made with an unsupported Python.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "%CHECK%" >nul 2>nul || (
    echo Rebuilding setup...
    rmdir /s /q .venv
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup, this takes a few minutes...
  %PY% -m venv .venv || (echo Could not create the setup folder. & pause & exit /b 1)
)
rem Install or update the libraries whenever requirements.txt changes.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul || (
  ".venv\Scripts\python.exe" -m pip install --upgrade pip || (echo Setup failed. See Troubleshooting in README.txt. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade --only-binary=av,ctranslate2,onnxruntime,tokenizers -r requirements.txt || (echo Setup failed. See Troubleshooting in README.txt. & pause & exit /b 1)
  copy /y requirements.txt ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" movie_filter.py %*
if errorlevel 1 pause
