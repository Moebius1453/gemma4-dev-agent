param(
    [string]$ProfileDir = $(if ($env:KAGGLE_EDGE_PROFILE) { $env:KAGGLE_EDGE_PROFILE } else { Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "_AI工作区\edge-kaggle-profile" }),
    [string]$NotebookUrl = $(if ($env:KAGGLE_NOTEBOOK_URL) { $env:KAGGLE_NOTEBOOK_URL } else { "https://www.kaggle.com/code/moebius1453/getting-started-gemma-4-developer-agent/edit" }),
    [int]$Port = 9333
)

$ErrorActionPreference = "Stop"

# 使用独立 user-data-dir 启动专属 Edge 进程，避开日常浏览器进程合并与每次 CDP 连接的确认弹窗；
# 默认端口选 9333 以避开本机可能占用 9222 的其他 Chromium 实例。
if (-not (Test-Path $ProfileDir)) {
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
}

$EdgeCandidates = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
)
$EdgeExe = $EdgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $EdgeExe) {
    throw "未找到 msedge.exe"
}

try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2 -ErrorAction Stop
    Write-Output "独立浏览器已在运行 (Port $Port): $($resp.Browser)"
    exit 0
} catch {
}

$ArgsList = @(
    "--user-data-dir=`"$ProfileDir`"",
    "--remote-debugging-port=$Port",
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    $NotebookUrl
)

Start-Process -FilePath $EdgeExe -ArgumentList $ArgsList
Start-Sleep -Seconds 3

$resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5 -ErrorAction Stop
Write-Output "已启动独立浏览器 (Port $Port): $($resp.Browser)"
Write-Output "WebSocket: $($resp.webSocketDebuggerUrl)"
