$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw '未找到 Python，请安装 Python 3.11+。' }
$pythonVersion = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pythonVersion -lt [version]'3.11') { throw "Python 版本过低：$pythonVersion" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw '未找到 npm，请安装 Node.js。' }

if (-not (Test-Path .venv\Scripts\python.exe)) { python -m venv --without-pip .venv }
$venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $venvPython -m ensurepip --upgrade 2>$null
if ($LASTEXITCODE -ne 0) { throw '虚拟环境缺少 pip，请修复 Python 安装后重试；未向系统 Python 安装依赖。' }
& $venvPython -m pip install -r backend/requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
npm --prefix frontend ci --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
Write-Host '依赖安装完成。'
