param(
    [string]$ListenAddress = '127.0.0.1',
    [ValidateRange(1024,65535)][int]$Port = 8000,
    [switch]$Radmin,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
if ($Radmin) {
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like '*Radmin*' -and $_.IPAddress -ne '127.0.0.1' })
    if ($addresses.Count -ne 1) { throw '没有找到唯一的 Radmin IPv4 地址，请使用 -ListenAddress 显式指定。' }
    $ListenAddress = $addresses[0].IPAddress
}
if ($ListenAddress -ne '127.0.0.1') {
    $owned = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $ListenAddress })
    if ($owned.Count -eq 0) { throw '监听地址必须是本机已有的 IPv4 地址，不接受所有网卡地址。' }
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "端口 $Port 已被占用，请使用 -Port 指定空闲端口。" }
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行 scripts/setup.ps1 创建项目环境。' }
if (-not $SkipBuild) {
    & npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败，未启动服务。' }
}
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) { throw '缺少前端构建产物。' }
Write-Host "主机管理入口：http://127.0.0.1:$Port 。防火墙规则需单独配置。"
if ($ListenAddress -ne '127.0.0.1') { Write-Host "队友访问入口：http://${ListenAddress}:$Port" }
& $python -m backend.run --listen $ListenAddress --port $Port
if ($LASTEXITCODE -ne 0) { throw "服务已退出，退出码 $LASTEXITCODE。" }
