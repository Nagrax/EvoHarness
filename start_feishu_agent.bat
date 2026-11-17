@echo off
chcp 65001 >nul
title EvoHarness Feishu Agent (watchdog)
cd /d "%~dp0feishu-daily-agent"

echo [run] 飞书长连接服务（看门狗模式：崩溃 10 秒后自动重启）
echo [run] 若未配置凭证则运行在 offline 模式（本地流水线，不连飞书）
call "%~dp0.venv\Scripts\activate.bat"

:loop
python main.py
echo [watchdog] 飞书服务退出（exit=%errorlevel%），10 秒后重启...
timeout /t 10 /nobreak >nul
goto loop
