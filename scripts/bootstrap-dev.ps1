# bootstrap-dev.ps1 -- build dev venvs from packaging/deps.lock.json
# ASCII only (project rule: .ps1 must be pure ASCII).
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$lockPath = Join-Path $root "packaging\deps.lock.json"
if (-not (Test-Path $lockPath)) { throw "missing $lockPath" }

$py = "python"
$ver = & $py -c "import sys;print('%d.%d' % sys.version_info[:2])"
if ($ver -ne "3.12") { throw "need Python 3.12 x64 (got $ver)" }
Write-Host "[ok] Python $ver"

$lock = Get-Content $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($name in @("terminal", "panel", "music")) {
    $site = Join-Path $root "runtime\venvs\$name"
    $vpy = Join-Path $site "Scripts\python.exe"
    if (-not (Test-Path $vpy)) {
        Write-Host "[$name] creating venv ..."
        & $py -m venv $site
    }
    $prop = $lock.components.PSObject.Properties[$name]
    if ($null -eq $prop) { throw "deps.lock.json has no components.$name" }
    $pkgs = @($prop.Value)
    Write-Host "[$name] installing $($pkgs.Count) pinned packages ..."
    & $vpy -m pip install --disable-pip-version-check --no-input @pkgs
    if ($LASTEXITCODE -ne 0) { throw "[$name] pip install failed" }
}
Write-Host ""
Write-Host "done. Next:"
Write-Host "  set COCRAFT_NODE_SRC=D:\agentlist\deepseekharness\node\node-v22.23.3-win-x64"
Write-Host "  set COCRAFT_CODEX_NM=D:\agentlist\codex\node_modules"
Write-Host "  python packaging\build.py --check-lock"
