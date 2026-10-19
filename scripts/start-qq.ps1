# 启动 QQ 实例
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\config.ps1")

if (-not (Get-Command dsh -ErrorAction SilentlyContinue)) {
    throw "找不到 dsh，请先把 DeepSeek Harness 的 CLI 加进 PATH。"
}

$env:DSH_HOME = $QQHome
Set-Location $QQWork

Write-Host "qq        ->  http://127.0.0.1:$QQPort"
Write-Host "DSH_HOME  =  $QQHome"
dsh web --port $QQPort --no-open
