@echo off
cd /d "%~dp0"
title ClipperHub
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
"%PYTHON%" -B -m clipperhub ui --managed-updates %*
if "%ERRORLEVEL%"=="42" goto restart
if errorlevel 1 pause
exit /b

:restart
"%PYTHON%" -B -m clipperhub ui --managed-updates --no-browser %*
if "%ERRORLEVEL%"=="42" goto restart
if errorlevel 1 pause
