# Install UltraRoute with Python managed by uv. No Git required.
param([switch]$NoStart)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use install.sh on Linux.' }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing uv...'
    $UvInstaller = Invoke-RestMethod 'https://astral.sh/uv/install.ps1'
    Invoke-Expression $UvInstaller
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv installation failed. Restart your terminal and retry.' }
& uv tool install --upgrade --python 3.13 'https://github.com/wanbnn/ultraroute/archive/refs/heads/main.zip'
if ($LASTEXITCODE -ne 0) { throw 'UltraRoute installation failed.' }
$ToolBin = (& uv tool dir --bin | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not find the uv tool directory.' }
Write-Host ''
Write-Host 'UltraRoute installed. Dashboard: http://127.0.0.1:8989'
Write-Host 'Next time, run: ultraroute'
Write-Host 'If the command is not found, restart your terminal or run: uv tool update-shell'
if (-not $NoStart) {
    & (Join-Path $ToolBin 'ultraroute.exe')
    if ($LASTEXITCODE -ne 0) { throw 'UltraRoute exited with an error.' }
}
