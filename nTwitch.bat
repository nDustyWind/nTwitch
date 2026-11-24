@echo off
rem nTwitch launcher: double-click to start. Sets everything up on first run.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title nTwitch
set PYTHONUTF8=1

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" (
    echo.
    echo  nTwitch - setting up for the first time, this only happens once...
    set "PYCMD="

    rem 1) any Python 3.9+ already on this PC
    for %%C in ("py -3" "python" "python3") do (
        if not defined PYCMD (
            %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PYCMD=%%~C"
        )
    )

    rem 2) no Python: install it with winget, then look in its default folders
    if not defined PYCMD (
        where winget >nul 2>&1 && (
            echo  Python was not found. Installing Python 3.12...
            winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        )
        for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do (
            if not defined PYCMD if exist "%%~P" set PYCMD="%%~P"
        )
    )

    if not defined PYCMD (
        echo.
        echo  Could not find or install Python automatically.
        echo  Please install Python 3.9 or newer from https://www.python.org/downloads/
        echo  ^(tick "Add python.exe to PATH"^), then run nTwitch again.
        echo.
        pause
        exit /b 1
    )

    !PYCMD! -m venv "%VENV%"
    if errorlevel 1 (
        echo.
        echo  Could not create nTwitch's private Python environment.
        echo.
        pause
        exit /b 1
    )
)

"%VPY%" "%~dp0nTwitch.py" %*
set "CODE=%ERRORLEVEL%"

rem When launched by double-click, keep the window open if something went wrong.
if not "%CODE%"=="0" (
    echo %cmdcmdline% | find /i "/c" >nul && (
        echo.
        pause
    )
)
exit /b %CODE%
