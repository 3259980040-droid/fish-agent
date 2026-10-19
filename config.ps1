# 统一路径与端口。整个目录移动后无需修改。
$Root = Split-Path -Parent $PSScriptRoot

$ResearchHome = Join-Path $Root "homes\research"
$QQHome       = Join-Path $Root "homes\qq"

$Knowledge    = Join-Path $Root "knowledge"
$QQWork       = Join-Path $Root "qq-work"
$Shared       = Join-Path $Root "shared"

$ResearchPort = 3080
$QQPort       = 3090
$DashboardPort = 3099
$OneBotUrl    = "ws://127.0.0.1:3001"
