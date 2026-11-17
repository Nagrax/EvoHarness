@echo off
chcp 65001 >nul
title EvoHarness Loop Bridge (watchdog)
cd /d "%~dp0"

echo [run] 闭环自迭代 daemon（看门狗模式：崩溃 10 秒后自动重启）
echo [run] 每 4h 一轮 + 每天 08:35 闭环报告 + 每天 10:00 PM 催办
call .venv\Scripts\activate.bat

:loop
python integration\iterate.py --daemon
echo [watchdog] daemon 退出（exit=%errorlevel%），10 秒后重启...
timeout /t 10 /nobreak >nul
goto loop
