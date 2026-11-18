@echo off
rem ===========================================================================
rem  Parametric Aircraft for SolidWorks - launcher
rem
rem    run_bat.bat                 start the Aircraft Designer GUI
rem    run_bat.bat my_uav.json     start the GUI with another configuration
rem    run_bat.bat --install       only set up / update the Python environment
rem
rem  First run: creates the virtual environment in .venv and installs
rem  requirements.txt. Later runs reinstall only when requirements.txt changed.
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\requirements.installed"

rem ---- 1. virtual environment ------------------------------------------------
if not exist "%PY%" (
    echo Creating the Python environment in .venv ...
    where py >nul 2>nul && py -3 -m venv "%VENV%"
    if not exist "%PY%" python -m venv "%VENV%"
)
if not exist "%PY%" (
    echo.
    echo ERROR: Python was not found. Install Python 3.10 or newer from
    echo        https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo.
    echo ERROR: Python 3.10 or newer is required. Delete the .venv folder and
    echo        run this file again after installing a newer Python.
    pause
    exit /b 1
)

rem ---- 2. packages (only when requirements.txt changed) ----------------------
set "NEED_INSTALL=1"
if exist "%STAMP%" (
    fc /b "requirements.txt" "%STAMP%" >nul 2>nul && set "NEED_INSTALL=0"
)
if "%NEED_INSTALL%"=="1" (
    echo Installing the required packages ...
    "%PY%" -m pip install --disable-pip-version-check --upgrade pip >nul
    "%PY%" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: installing the packages failed - see the messages above.
        pause
        exit /b 1
    )
    copy /y "requirements.txt" "%STAMP%" >nul
)

if /i "%~1"=="--install" (
    echo Environment is ready.
    exit /b 0
)

rem ---- 3. start the GUI ------------------------------------------------------
echo Starting Aircraft Designer ...
"%PY%" aircraft_gui.py %*
if errorlevel 1 (
    echo.
    echo Aircraft Designer stopped with an error - see the messages above.
    pause
    exit /b 1
)
endlocal
