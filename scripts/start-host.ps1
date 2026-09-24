param([switch]$Fake, [string]$DataDir)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$hostExe = Join-Path $repoRoot 'host\target\release\tts-host.exe'
if (-not (Test-Path -LiteralPath $hostExe)) { $hostExe = Join-Path $repoRoot 'dist\tts-host.exe' }
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $hostExe)) { throw 'Build Host first: cargo build --release --manifest-path host/Cargo.toml' }
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run scripts/setup-worker.ps1 first.' }
$hostArgs = @('--python', ('"' + $pythonExe + '"'))
if ($Fake) { $hostArgs += '--fake' }
if ($DataDir) { $hostArgs += @('--data-dir', ('"' + $DataDir + '"')) }
Start-Process -FilePath $hostExe -ArgumentList $hostArgs -WindowStyle Hidden

