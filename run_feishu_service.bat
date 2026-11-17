@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem EvoHarness Feishu agent launcher.
rem Called by Windows Task Scheduler. Restart duty belongs to the task itself.
rem IMPORTANT: keep this file ASCII-only. Non-ASCII comments break batch
rem parsing under some console code pages and produce cryptic errors.

set "REPO=D:\EvoHarness"
set "APPDIR=%REPO%\feishu-daily-agent"
set "PYEXE=%REPO%\.venv\Scripts\python.exe"
set "LOGDIR=%APPDIR%\logs"

if not exist "%PYEXE%" goto no_python
if not exist "%APPDIR%\main.py" goto no_main

rem Skip if an instance is already running. Two long connections for the same
rem app steal each other's events, which looks like connected-but-silent.
powershell -NoProfile -Command "$c=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' }).Count; if ($c -gt 0) { exit 3 }"
if errorlevel 3 goto already_running

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%APPDIR%"
"%PYEXE%" main.py >> "%LOGDIR%\service.out" 2>> "%LOGDIR%\service.err"
exit /b %errorlevel%

:already_running
echo [launcher] already running, skip
exit /b 0

:no_python
echo [launcher] python not found: %PYEXE%
exit /b 2

:no_main
echo [launcher] main.py not found: %APPDIR%\main.py
exit /b 4
