<#
Build QuantEcho as an MSIX package for the Microsoft Store (or local sideload testing).

  powershell -ExecutionPolicy Bypass -File packaging\build_msix.ps1
  powershell -ExecutionPolicy Bypass -File packaging\build_msix.ps1 -IdentityName "12345Publisher.QuantEcho" `
      -Publisher "CN=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX" -PublisherDisplayName "Your Name"

The Store signs the package itself, so no certificate is needed for submission. The three identity values
must match Partner Center > your app > Product management > Product identity. Defaults are for local tests.

Output: build\msix\QuantEcho_<version>_x64.msix   (+ build\msix\layout\ for loose-file dev install)
#>
param(
    [string]$IdentityName = "QuantEcho.Dev",
    [string]$Publisher = "CN=QuantEcho Dev",
    [string]$PublisherDisplayName = "QuantEcho (dev build)",
    [string]$Version = "",
    [switch]$SkipPyInstaller,
    [switch]$NoAI          # build without the bundled engine + model (small package; Ollama only)
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"

# --- version: from pyproject (x.y.z -> x.y.z.0; the Store requires the 4th part to be 0)
if (-not $Version) {
    $m = Select-String -Path (Join-Path $Root "pyproject.toml") -Pattern '^version = "(\d+)\.(\d+)\.(\d+)"'
    $Version = "{0}.{1}.{2}.0" -f $m.Matches[0].Groups[1].Value, $m.Matches[0].Groups[2].Value, $m.Matches[0].Groups[3].Value
}
Write-Host "QuantEcho MSIX $Version  identity=$IdentityName  publisher=$Publisher"

# --- Windows SDK tools
$sdk = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\makeappx.exe" -ErrorAction SilentlyContinue |
       Sort-Object FullName -Descending | Select-Object -First 1
if (-not $sdk) { throw "makeappx.exe not found - install the Windows 10/11 SDK" }
$makeappx = $sdk.FullName
$makepri = Join-Path $sdk.DirectoryName "makepri.exe"

# --- 1. icons + frozen app
& $Py (Join-Path $Root "packaging\make_icons.py")
if (-not $SkipPyInstaller) {
    & (Join-Path $Root ".venv\Scripts\pyinstaller.exe") (Join-Path $Root "packaging\quantecho.spec") --noconfirm `
        --distpath (Join-Path $Root "build\dist") --workpath (Join-Path $Root "build\work") --log-level WARN
    if ($LASTEXITCODE) { throw "PyInstaller failed" }
}
$dist = Join-Path $Root "build\dist\QuantEcho"

# --- 2. self-test the frozen build before packaging it
$report = Join-Path $env:TEMP "quantecho_selftest.txt"
$p = Start-Process -FilePath (Join-Path $dist "QuantEcho.exe") -ArgumentList "--selftest","--out",$report -Wait -PassThru
Get-Content $report | Write-Host
if ($p.ExitCode -ne 0) { throw "Frozen self-test failed (see above)" }

# --- 3. layout
$out = Join-Path $Root "build\msix"
$layout = Join-Path $out "layout"
if (Test-Path $layout) { Remove-Item $layout -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $layout "Assets") | Out-Null
Copy-Item $dist (Join-Path $layout "QuantEcho") -Recurse
$app = Join-Path $layout "QuantEcho"

# --- 3b. built-in AI: llama.cpp server (+ its libraries only) and the Qwen3 model, next to QuantEcho.exe
if (-not $NoAI) {
    & $Py (Join-Path $Root "packaging\fetch_ai.py")
    if ($LASTEXITCODE) { throw "fetch_ai.py failed" }
    $eng = Join-Path $app "engine"; New-Item -ItemType Directory -Force -Path $eng | Out-Null
    Copy-Item (Join-Path $Root "build\engine\llama-server.exe") $eng
    Get-ChildItem (Join-Path $Root "build\engine") -Filter "*.dll" |
        Where-Object { $_.Name -notmatch '-impl\.dll$' -or $_.Name -eq 'llama-server-impl.dll' } |
        Copy-Item -Destination $eng
    $mdl = Join-Path $app "models"; New-Item -ItemType Directory -Force -Path $mdl | Out-Null
    Copy-Item (Join-Path $Root "build\models\*.gguf") $mdl
    Write-Host ("built-in AI: engine {0:N0} MB + model {1:N0} MB" -f `
        ((Get-ChildItem $eng | Measure-Object Length -Sum).Sum / 1MB), ((Get-ChildItem $mdl | Measure-Object Length -Sum).Sum / 1MB))
}
$lic = Join-Path $app "licenses"; New-Item -ItemType Directory -Force -Path $lic | Out-Null
Copy-Item (Join-Path $Root "LICENSE") (Join-Path $lic "QuantEcho-LICENSE.txt")
Copy-Item (Join-Path $Root "THIRD_PARTY_NOTICES.md") $lic
if (Test-Path (Join-Path $Root "build\licenses")) { Copy-Item (Join-Path $Root "build\licenses\*") $lic }
Get-ChildItem (Join-Path $Root "packaging\assets") -Filter "*.png" |
    Where-Object { $_.Name -notmatch '^(icon-1024|StoreListing)' } |
    Copy-Item -Destination (Join-Path $layout "Assets")
(Get-Content (Join-Path $Root "packaging\AppxManifest.template.xml") -Raw) `
    -replace '\{\{IDENTITY_NAME\}\}', $IdentityName `
    -replace '\{\{PUBLISHER\}\}', [System.Security.SecurityElement]::Escape($Publisher) `
    -replace '\{\{PUBLISHER_DISPLAY_NAME\}\}', [System.Security.SecurityElement]::Escape($PublisherDisplayName) `
    -replace '\{\{VERSION\}\}', $Version |
    Set-Content -Path (Join-Path $layout "AppxManifest.xml") -Encoding UTF8

# --- 4. resource index (lets Windows pick scale-200 / targetsize icons)
$priCfg = Join-Path $out "priconfig.xml"
& $makepri createconfig /cf $priCfg /dq en-US /o | Out-Null
& $makepri new /pr $layout /cf $priCfg /of (Join-Path $layout "resources.pri") /o | Out-Null
if ($LASTEXITCODE) { throw "makepri failed" }

# --- 5. pack (makeappx validates the manifest schema)
$msix = Join-Path $out ("QuantEcho_{0}_x64.msix" -f $Version)
& $makeappx pack /d $layout /p $msix /o | Out-Null
if ($LASTEXITCODE) { throw "makeappx pack failed" }
$mb = [math]::Round((Get-Item $msix).Length / 1MB, 1)
Write-Host "OK: $msix ($mb MB)"
