@echo off
rem Editable install plus the one dependency pip cannot provide: ffmpeg.
setlocal
title TimeLeapPlayer - install

pushd "%~dp0"

set "PY=python"
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3.10 or newer: https://www.python.org/downloads/windows/
    goto :fail
)

%PY% -c "import sys;sys.exit(0 if min(sys.version_info[:2],(3,10))==(3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo TimeLeapPlayer needs Python 3.10 or newer. Found:
    %PY% --version
    goto :fail
)

echo Installing TimeLeapPlayer and its dependencies...
%PY% -m pip install -e .
if errorlevel 1 (
    echo.
    echo pip install failed. Try: %PY% -m pip install --upgrade pip setuptools
    goto :fail
)

echo.
echo Checking for ffmpeg...
set "FFMISSING="
where ffmpeg >nul 2>&1
if errorlevel 1 set "FFMISSING=1"
where ffprobe >nul 2>&1
if errorlevel 1 set "FFMISSING=1"

if defined FFMISSING (
    echo.
    echo   ffmpeg / ffprobe were NOT found on PATH.
    echo   TimeLeapPlayer cannot decode a single frame without them.
    echo.
    echo   Download a full build:  https://www.gyan.dev/ffmpeg/builds/
    echo   Unzip it, then add the "bin" folder to your PATH and open a new
    echo   terminal. Or install with winget:
    echo.
    echo       winget install Gyan.FFmpeg
    echo.
    goto :fail
)

echo   ffmpeg  OK
echo   ffprobe OK
where ffplay >nul 2>&1
if errorlevel 1 (
    echo   ffplay  missing - optional, only the fallback audio backend uses it
) else (
    echo   ffplay  OK
)

echo.
echo Done. Start the player with run.bat, or the "timeleap" command.
popd
endlocal
pause
exit /b 0

:fail
echo.
echo Install did not complete.
popd
endlocal
pause
exit /b 1
