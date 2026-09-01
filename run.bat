@echo off
rem Launch the TimeLeapPlayer GUI from anywhere: double-click, shortcut or shell.
setlocal
title TimeLeapPlayer

rem %~dp0 is this script's folder, so the repo is found no matter the cwd.
pushd "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

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
    echo TimeLeapPlayer needs Python 3.10 or newer.
    %PY% --version
    goto :fail
)

%PY% -c "import numpy" >nul 2>&1
if errorlevel 1 (
    echo Missing dependencies. Run install.bat first.
    goto :fail
)

rem No arguments means "open the control panel". A bare `timeleap` only prints
rem help and exits 2, which would look like a crash to anyone double-clicking.
if "%~1"=="" (
    %PY% -m timeleap gui
) else (
    %PY% -m timeleap %*
)
if errorlevel 1 goto :fail

popd
endlocal
exit /b 0

:fail
echo.
echo TimeLeapPlayer exited with an error.
popd
pause
endlocal
exit /b 1
