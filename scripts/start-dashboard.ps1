# 启动本地监控面板
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\config.ps1")

Set-Location $Root
Write-Host "dashboard -> http://127.0.0.1:$DashboardPort"
python dashboard\server.py --port $DashboardPort
