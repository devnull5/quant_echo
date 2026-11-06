<#
Install / remove the locally built QuantEcho package for testing (no certificate needed).
Requires Windows Developer Mode: Settings > System > For developers > Developer Mode = On.

  powershell -ExecutionPolicy Bypass -File packaging\install_dev.ps1            # install (register layout)
  powershell -ExecutionPolicy Bypass -File packaging\install_dev.ps1 -Remove    # uninstall
#>
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$existing = Get-AppxPackage -Name "QuantEcho.Dev" -ErrorAction SilentlyContinue
if ($existing) { Remove-AppxPackage -Package $existing.PackageFullName; Write-Host "Removed $($existing.PackageFullName)" }
if ($Remove) { return }
$manifest = Join-Path $Root "build\msix\layout\AppxManifest.xml"
if (-not (Test-Path $manifest)) { throw "Build first: packaging\build_msix.ps1" }
Add-AppxPackage -Register $manifest
$pkg = Get-AppxPackage -Name "QuantEcho.Dev"
Write-Host "Installed $($pkg.PackageFullName). Start menu: QuantEcho"
Write-Host "Launch: explorer.exe shell:AppsFolder\$($pkg.PackageFamilyName)!QuantEcho"
