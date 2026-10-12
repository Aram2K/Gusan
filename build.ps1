# build.ps1 - Build Gusan as a standalone Windows app with PyInstaller
# Run from the project root: powershell -ExecutionPolicy Bypass -File build.ps1
# Optional: -Tag v2.0.0 stops the build when the tag differs from APP_VERSION.
#
# The same steps as the Windows job in .github/workflows/build.yml. The output
# keeps its old file names on purpose (dist\TranscribeApp\TranscribeApp.exe):
# installed copies, pins and the updater depend on them. Windows shows "Gusan"
# from the exe's version info.

param([string]$Tag = "")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The venv's own python (never pip.exe or Activate.ps1: the venv may have been
# made at another path).
$py = ".\venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No venv found at $py - run setup.ps1 first." -ForegroundColor Red
    exit 1
}

# APP_VERSION is read from app_info.py as text (never import main).
$src = Get-Content app_info.py -Raw
if ($src -notmatch '(?m)^APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"') {
    Write-Host 'APP_VERSION = "X.Y.Z" not found in app_info.py' -ForegroundColor Red
    exit 1
}
$appVersion = $Matches[1]
if ($Tag) {
    $tagVersion = $Tag -replace '^v', ''
    if ($tagVersion -ne $appVersion) {
        Write-Host "Tag $Tag does not match APP_VERSION $appVersion in app_info.py - bump app_info.py or fix the tag." -ForegroundColor Red
        exit 1
    }
}
Write-Host "Building Gusan $appVersion" -ForegroundColor Cyan

Write-Host "Installing PyInstaller..." -ForegroundColor Cyan
& $py -m pip install pyinstaller==6.22.3 --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Generating the app icon..." -ForegroundColor Cyan
& $py installer/make_icon.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Windows shows these in Task Manager, Startup apps, notifications and the
# file's Properties: "Gusan" by AIBUBEN, while the file stays TranscribeApp.exe.
# Same script as build.yml (tests/test_release_2_0_0.py checks they match).
Write-Host "Writing the Windows version info..." -ForegroundColor Cyan
# version-info:begin
@'
import os, re, sys
src = open("app_info.py", encoding="utf-8").read()
name = re.search(r'^APP_NAME\s*=\s*"([^"]+)"', src, re.M).group(1)
ver = re.search(r'^APP_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src, re.M)
nums = tuple(int(x) for x in ver.groups()) + (0,)
dotted = ".".join(str(n) for n in nums)
strings = [
    ("CompanyName", "AIBUBEN"),
    ("FileDescription", name),
    ("FileVersion", dotted),
    ("InternalName", "TranscribeApp"),
    ("LegalCopyright", "Copyright (c) 2025 Aram Adamyan. All rights reserved."),
    ("OriginalFilename", "TranscribeApp.exe"),
    ("ProductName", name),
    ("ProductVersion", dotted),
]
text = (
    "VSVersionInfo(\n"
    "  ffi=FixedFileInfo(filevers=%r, prodvers=%r, mask=0x3f, flags=0x0,\n"
    "                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
    "  kids=[\n"
    "    StringFileInfo([StringTable('040904B0', [\n"
    "%s\n"
    "    ])]),\n"
    "    VarFileInfo([VarStruct('Translation', [1033, 1200])]),\n"
    "  ]\n"
    ")\n"
) % (nums, nums, ",\n".join("      StringStruct(%r, %r)" % kv for kv in strings))
out = sys.argv[1]
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    f.write(text)
print("Version info: %s %s -> %s" % (name, dotted, out))
'@ | & $py - build/version_info.txt
# version-info:end
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Building .exe..." -ForegroundColor Cyan
& $py -m PyInstaller `
    --noconfirm `
    --onedir `
    --windowed `
    --name "TranscribeApp" `
    --icon "assets/icon.ico" `
    --version-file "build/version_info.txt" `
    --add-data "config.example.json;." `
    --add-data "history.py;." `
    --add-data "assets;assets" `
    --hidden-import "faster_whisper" `
    --hidden-import "faster_whisper.utils" `
    --hidden-import "tqdm" `
    --hidden-import "tqdm.auto" `
    --hidden-import "ctranslate2" `
    --hidden-import "huggingface_hub" `
    --hidden-import "llama_cpp" `
    --hidden-import "tokenizers" `
    --hidden-import "pyaudiowpatch" `
    --hidden-import "sherpa_onnx" `
    --hidden-import "sherpa_onnx.lib._sherpa_onnx" `
    --collect-all "faster_whisper" `
    --collect-all "ctranslate2" `
    --collect-all "llama_cpp" `
    --collect-all "pystray" `
    --collect-all "pynput" `
    --collect-all "pyaudiowpatch" `
    --collect-all "sherpa_onnx" `
    --exclude-module "torch" `
    --exclude-module "openai" `
    --exclude-module "numba" `
    --exclude-module "llvmlite" `
    --exclude-module "matplotlib" `
    --exclude-module "IPython" `
    main.py

if ($LASTEXITCODE -eq 0) {
    $vi = (Get-Item dist\TranscribeApp\TranscribeApp.exe).VersionInfo
    Write-Host ""
    Write-Host "Build complete: $($vi.ProductName) $($vi.FileVersion) by $($vi.CompanyName)" -ForegroundColor Green
    Write-Host "  dist\TranscribeApp\TranscribeApp.exe" -ForegroundColor Green
    if ($vi.FileVersion -ne "$appVersion.0") {
        Write-Host "TranscribeApp.exe has no version info for $appVersion" -ForegroundColor Red
        exit 1
    }
    # multimedia-check:begin
    # Meetings play their recording in the app (Qt's Windows media plugin; the same check in build.yml and build.ps1).
    $mm = "dist\TranscribeApp\_internal\PySide6\plugins\multimedia\windowsmediaplugin.dll"
    if (-not (Test-Path $mm)) { throw "No Qt multimedia plugin in the build ($mm): meetings can't play their recording." }
    Write-Host "Multimedia plugin: $mm"
    # multimedia-check:end
    Write-Host ""
    Write-Host "The installer: ISCC.exe /DMyAppVersion=$appVersion installer\installer.iss" -ForegroundColor Yellow
} else {
    Write-Host "Build failed. Check errors above." -ForegroundColor Red
    exit 1
}
