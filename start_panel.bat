@echo off
rem EvoHarness panel launcher (ASCII only, see run_feishu_service.bat for why).
set "REPO=D:\EvoHarness"
set "PYEXE=%REPO%\.venv\Scripts\python.exe"
set "PANELDIR=%REPO%\panel"

if not exist "%PYEXE%" goto no_python
cd /d "%PANELDIR%"
"%PYEXE%" panel.py
exit /b %errorlevel%

:no_python
echo [panel] python not found: %PYEXE%
exit /b 2
