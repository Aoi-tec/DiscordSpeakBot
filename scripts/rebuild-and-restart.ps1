# Stop Host + Worker, test the Worker, rebuild the Host, update dist\, then start again.
# Usage: double-click scripts\rebuild-and-restart.cmd  (or: powershell -File scripts\rebuild-and-restart.ps1 [-NoStart] [-SkipTests])
param([switch]$NoStart, [switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $repoRoot

Write-Host '== [1/4] Stopping Host and Worker' -ForegroundColor Cyan
cmd /c "taskkill /IM tts-host.exe /T /F >nul 2>&1"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'discord_speak_bot' } |
    ForEach-Object {
        Write-Host "  stopping Worker pid $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
# Give Windows time to release the exe file and the data-dir lock.
Start-Sleep -Seconds 2

$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run scripts\setup-worker.ps1 first.' }
if (-not $SkipTests) {
    Write-Host '== [2/4] Worker tests' -ForegroundColor Cyan
    & $pythonExe -m pytest (Join-Path $repoRoot 'worker') -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'Worker tests failed. Host was not rebuilt.' }
} else {
    Write-Host '== [2/4] Worker tests skipped' -ForegroundColor Yellow
}

Write-Host '== [3/4] Building Host (cargo build --release)' -ForegroundColor Cyan
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw 'cargo not found. Install Rust (https://rustup.rs) and Visual Studio C++ Build Tools.'
}
cargo build --release --locked --manifest-path (Join-Path $repoRoot 'host\Cargo.toml')
if ($LASTEXITCODE -ne 0) { throw 'Host build failed.' }
$built = Join-Path $repoRoot 'host\target\release\tts-host.exe'
$dist = Join-Path $repoRoot 'dist\tts-host.exe'
Copy-Item -LiteralPath $built -Destination $dist -Force
Write-Host "  updated $dist"

if ($NoStart) {
    Write-Host '== [4/4] Start skipped (-NoStart)' -ForegroundColor Yellow
} else {
    Write-Host '== [4/4] Starting Host' -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'start-host.ps1')
}
Write-Host 'Done.' -ForegroundColor Green
