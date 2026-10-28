# Builds dist\Audiobooker-Setup-<version>.exe on a Windows machine.
# Requirements: Python 3.11+ on PATH, Inno Setup 6 (https://jrsoftware.org/isdl.php).
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Select-String -Path app\audiobooker\__init__.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$uvVersion = "0.11.32"
Write-Host "Building Audiobooker $version"

python -m pip install --upgrade pyinstaller pillow
python packaging\make_icon.py packaging
Copy-Item packaging\icon.png app\audiobooker\icon.png -Force

# 1. The launcher (small tkinter exe)
python -m PyInstaller --noconfirm --clean --windowed --name Audiobooker `
  --icon packaging\icon.ico --distpath dist\launcher --workpath build\launcher `
  packaging\launcher.py

# 2. uv (used by the launcher to set up Python + PyTorch on first run)
New-Item -ItemType Directory -Force -Path dist\uv | Out-Null
if (-not (Test-Path dist\uv\uv.exe)) {
  $zip = "dist\uv\uv.zip"
  Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
  Expand-Archive $zip -DestinationPath dist\uv -Force
  Remove-Item $zip
}

# 3. A private Python, bundled so first run never runs `uv python install`
#    (its folder junctions fail on some Windows 11 systems with os error 448).
if (-not (Test-Path dist\python\python.exe)) {
  & dist\uv\uv.exe python install 3.11 --install-dir dist\pyinst
  if ($LASTEXITCODE -ne 0) { throw "uv python install failed" }
  $src = Get-ChildItem dist\pyinst -Directory | Where-Object { $_.Name -match '^cpython-3\.11\.\d+-windows-x86_64-none$' } | Select-Object -First 1
  if (-not $src) { throw "Could not find the installed Python folder" }
  Copy-Item -Recurse $src.FullName dist\python
  if (-not (Test-Path dist\python\python.exe)) { throw "Bundled Python is missing python.exe" }
}

# 4. The installer
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
  Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php" }
& $iscc "/DAppVersion=$version" packaging\audiobooker.iss
Write-Host "Done: dist\Audiobooker-Setup-$version.exe"
