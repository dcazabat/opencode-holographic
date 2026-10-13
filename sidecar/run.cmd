@echo off
REM Launches the sidecar on Windows.
REM Interpreter: %OPENCODE_HOLOGRAPHIC_PYTHON%, the project .venv, or python on PATH.
setlocal enableextensions

set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "PROJECT=%%~fI"
if "%HERMES_HOME%"=="" set "HERMES_HOME=%USERPROFILE%\.hermes"

set "PY=%OPENCODE_HOLOGRAPHIC_PYTHON%"
if "%PY%"=="" if exist "%PROJECT%\.venv\Scripts\python.exe" set "PY=%PROJECT%\.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"

REM Shared mode resolves the provider from %HERMES_HOME%\plugins; standalone mode uses
REM vendor\holographic. server.py decides which one.
if "%PYTHONPATH%"=="" (
  set "PYTHONPATH=%HERMES_HOME%\plugins"
) else (
  set "PYTHONPATH=%HERMES_HOME%\plugins;%PYTHONPATH%"
)

"%PY%" "%HERE%server.py" %*
endlocal
