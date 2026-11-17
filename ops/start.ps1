# EvoHarness 数字员工单活启动（Windows 包装脚本，DE-23）。
# 真正的检查/单活/看护在 ops/supervisor.py；本脚本只负责定位 Python 并拉起主管。
# 用法：powershell -ExecutionPolicy Bypass -File ops\start.ps1 [-Once]
param(
    [switch]$Once,   # 只做启动前检查 + 前台运行一次（调试用）
    [string]$Db = "",
    [string]$Cwd = ""
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Cwd) { $Cwd = Join-Path $repo "feishu-daily-agent" }

$python = Join-Path $repo "feishu-daily-agent\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# 部署者通过环境变量提供 DATABASE_URL / FEISHU_TOKEN_SECRET / FEISHU_* 配置；
# 本脚本不读也不写任何凭据（不碰 .env 的内容，只交给子进程环境）。
$supArgs = @("-m", "ops.supervisor", "start", "--cwd", $Cwd)
if ($Db) { $supArgs += @("--db", $Db) }
if ($Once) { $supArgs += @("--once") }

Push-Location $repo
try {
    & $python @supArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
