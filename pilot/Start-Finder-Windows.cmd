@echo off
setlocal
title Finder pilot
py -3.12 -c "import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if not errorlevel 1 goto py312
py -3 -c "import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if not errorlevel 1 goto py3
python -c "import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if not errorlevel 1 goto python
echo Install Python 3.12 or later for Windows from https://www.python.org/downloads/windows/
echo Then open Start-Finder-Windows.cmd again. No administrator access is needed.
pause
exit /b 1
:py312
py -3.12 -I "%~dp0windows_start.py"
goto done
:py3
py -3 -I "%~dp0windows_start.py"
goto done
:python
python -I "%~dp0windows_start.py"
:done
set "finder_exit=%errorlevel%"
if not "%finder_exit%"=="0" pause
exit /b %finder_exit%
