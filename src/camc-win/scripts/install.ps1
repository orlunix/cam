#Requires -Version 5.1
<#
.SYNOPSIS
    Install wintmux as tmux.exe for CAM compatibility on Windows.
.DESCRIPTION
    Copies wintmux.exe as tmux.exe to a user-local bin directory
    and adds it to the user PATH so CAM can find it.
.PARAMETER InstallDir
    Target directory. Defaults to $env:LOCALAPPDATA\wintmux
#>
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\wintmux"
)

$ErrorActionPreference = 'Stop'

# Find wintmux.exe (in same directory as this script's parent, or current dir)
$scriptDir = if ($PSScriptRoot) { Split-Path $PSScriptRoot -Parent } else { Get-Location }
$candidates = @(
    Join-Path $scriptDir 'wintmux.exe'
    Join-Path (Get-Location) 'wintmux.exe'
)
$source = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $source) {
    Write-Error "wintmux.exe not found. Build it first: go build -o wintmux.exe ./cmd/wintmux/"
    exit 1
}

Write-Host "Source: $source"
Write-Host "Install to: $InstallDir"

# Create install directory
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

# Copy as both wintmux.exe and tmux.exe
Copy-Item $source (Join-Path $InstallDir 'wintmux.exe') -Force
Copy-Item $source (Join-Path $InstallDir 'tmux.exe') -Force
Write-Host "Copied wintmux.exe and tmux.exe"

# Add to user PATH if not already present
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ($userPath -notlike "*$InstallDir*") {
    $newPath = "$InstallDir;$userPath"
    [Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
    Write-Host "Added $InstallDir to user PATH"
    Write-Host "(Restart your terminal for PATH changes to take effect)"
} else {
    Write-Host "$InstallDir already in PATH"
}

# Also update current session PATH
if ($env:PATH -notlike "*$InstallDir*") {
    $env:PATH = "$InstallDir;$env:PATH"
}

# Verify
Write-Host ""
Write-Host "=== Verification ==="
$tmuxPath = Get-Command tmux -ErrorAction SilentlyContinue
if ($tmuxPath) {
    Write-Host "tmux found at: $($tmuxPath.Source)"
    & tmux -V
    Write-Host ""
    Write-Host "Installation complete. CAM can now use tmux on Windows."
} else {
    Write-Host "WARNING: tmux not found in PATH. Restart your terminal."
}
