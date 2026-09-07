@echo off
rem ---------------------------------------------------------------------------
rem  TimeLeapPlayer - double-click launcher
rem
rem  Made for a desktop shortcut: it checks the environment, hands off to
rem  pythonw.exe (the windowed interpreter) and exits, so no black console
rem  window sits behind the control panel for the whole session.
rem
rem  Run it with arguments from a terminal and it stays on the console
rem  instead, so `TimeLeapPlayer.bat info clip.mp4` still prints its output.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title TimeLeapPlayer

rem %~dp0 is this script's own folder, so the shortcut works from anywhere
rem and the repo does not have to be pip-installed.
pushd "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "LOGDIR=%LOCALAPPDATA%\TimeLeapPlayer"
set "LOG=%LOGDIR%\launch.log"

rem The py launcher is more reliable than PATH order when several Pythons
rem are installed; fall back to a bare python/pythonw when it is absent.
set "PY=python"
set "PYW=pythonw"
where py >nul 2>&1 && set "PY=py -3"
where pyw >nul 2>&1 && set "PYW=pyw -3"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3.10 or newer: https://www.python.org/downloads/windows/
    goto :fail
)

%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo TimeLeapPlayer needs Python 3.10 or newer. Found:
    %PY% --version
    goto :fail
)

rem Import the real entry point rather than probing package names one by one:
rem this catches a missing numpy, a Tk-less Python and any import-time error
rem in one go, while there is still a console to report it on.
%PY% -c "import timeleap.ui.app" 2>"%TEMP%\timeleap_import.txt"
if errorlevel 1 (
    echo TimeLeapPlayer could not start. Details:
    echo.
    type "%TEMP%\timeleap_import.txt"
    echo.
    echo If numpy or pillow are missing, run install.bat in this folder.
    goto :fail
)
del "%TEMP%\timeleap_import.txt" >nul 2>&1

rem Arguments mean someone is driving it from a shell: stay attached so they
rem can read the output, and hold the window open if it fails.
if not "%~1"=="" (
    %PY% -m timeleap %*
    if errorlevel 1 goto :fail
    popd
    endlocal
    exit /b 0
)

rem No arguments: open the control panel with no console attached. A bare
rem `timeleap` only prints help and exits 2, which looks like a crash to
rem anyone double-clicking, so ask for `gui` explicitly.
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
echo [%DATE% %TIME%] launching >>"%LOG%"
start "TimeLeapPlayer" %PYW% -m timeleap gui

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
