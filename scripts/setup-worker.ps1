param([switch]$Dev)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$venvPath = Join-Path $repoRoot '.venv'
py -3.12 -m venv $venvPath
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
$pythonExe = Join-Path $venvPath 'Scripts\python.exe'
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip setup failed.' }
& $pythonExe -m pip install -r (Join-Path $repoRoot 'worker\requirements-dev.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $pythonExe -m pip install -e (Join-Path $repoRoot 'worker')
if ($LASTEXITCODE -ne 0) { throw 'Worker installation failed.' }
& $pythonExe -m discord_speak_bot init
if ($LASTEXITCODE -ne 0) { throw 'Configuration initialization failed.' }
if ($Dev) {
    & $pythonExe -m pytest (Join-Path $repoRoot 'worker') -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
}
Write-Host 'Worker setup complete. Qwen/CUDA dependencies and model setup are separate; see docs/setup.md.'

