# 启动研究实例（知识库 / B 站 / GitHub）
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\config.ps1")

if (-not (Get-Command dsh -ErrorAction SilentlyContinue)) {
    throw "找不到 dsh，请先把 DeepSeek Harness 的 CLI 加进 PATH。"
}

$env:DSH_HOME = $ResearchHome
Set-Location $Knowledge

Write-Host "research  ->  http://127.0.0.1:$ResearchPort"
Write-Host "DSH_HOME  =  $ResearchHome"
dsh web --port $ResearchPort --no-open
