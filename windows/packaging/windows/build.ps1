# Build AppHubSetup.exe on Windows 11.  Run in PowerShell from the project folder:
#     powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
# Needs: MSYS2 (https://www.msys2.org, default C:\msys64) and Inno Setup 6 (https://jrsoftware.org).
# Both can be installed with:  winget install MSYS2.MSYS2 JRSoftware.InnoSetup

$ErrorActionPreference = "Stop"
$root  = Resolve-Path "$PSScriptRoot\..\.."
$msys  = "C:\msys64"
$bash  = "$msys\usr\bin\bash.exe"
$iscc  = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" }   # per-user winget install

if (-not (Test-Path $bash)) { throw "MSYS2 not found at $msys. Install it: winget install MSYS2.MSYS2" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup" }

$version = (Select-String -Path "$root\src\app_hub\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value

# 1. GTK 4, libadwaita, PyGObject and PyInstaller inside MSYS2's UCRT64 environment
$env:MSYSTEM = "UCRT64"            # login shells then put /ucrt64/bin (python, gtk) first on PATH
$unix = ($root -replace '\\', '/') -replace '^([A-Za-z]):', '/$1'
& $bash -lc "pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-pip"
& $bash -lc "cd '$unix' && python -m pip install --break-system-packages --upgrade pyinstaller pyinstaller-hooks-contrib"
if ($LASTEXITCODE) { throw "pip install failed" }

# 2. Tests, icon, and the frozen app: dist\App Hub\AppHub.exe + app-hub.exe
& $bash -lc "cd '$unix' && python -m unittest discover -s tests"
if ($LASTEXITCODE) { throw "tests failed" }
& $bash -lc "cd '$unix' && python tools/make_ico.py"
if ($LASTEXITCODE) { throw "icon failed" }
& $bash -lc "cd '$unix' && python -m PyInstaller --noconfirm --distpath dist --workpath build/pyinstaller packaging/windows/app-hub.spec"
if ($LASTEXITCODE) { throw "PyInstaller failed" }
& "$root\dist\App Hub\app-hub.exe" --selftest
if ($LASTEXITCODE) { throw "selftest failed" }

# 3. Installer: dist\AppHubSetup-<version>.exe
& $iscc "/DAppVersion=$version" "$root\packaging\windows\installer.iss"
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Write-Host "`nDone: $root\dist\AppHubSetup-$version.exe"
