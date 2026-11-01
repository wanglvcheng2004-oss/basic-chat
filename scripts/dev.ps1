$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行 scripts/setup.ps1。' }
if (Get-NetTCPConnection -LocalPort 8000,5173 -State Listen -ErrorAction SilentlyContinue) { throw '8000 或 5173 端口已占用，请关闭现有开发服务。' }
$backend = Start-Process -WindowStyle Hidden -FilePath $python -WorkingDirectory $ProjectRoot -ArgumentList '-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','8000','--workers','1','--no-proxy-headers','--ws-max-size','16384' -PassThru
try {
    Write-Host '开发地址：http://127.0.0.1:5173。开发服务仅监听本机；多人联调用 start.ps1 -Radmin。'
    & npm --prefix frontend run dev -- --host 127.0.0.1 --strictPort
    if ($LASTEXITCODE -ne 0) { throw '前端开发服务异常退出。' }
} finally {
    if ($backend -and -not $backend.HasExited) { & taskkill /PID $backend.Id /T /F | Out-Null }
}
