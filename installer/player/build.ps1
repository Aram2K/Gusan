# Builds installer\wizard\GusanSlides.exe, the program that plays the installer's intro video
# (installer\player\GusanSlides.cs), with the C# compiler and WPF that ship with Windows. Nothing to install.
# Run it before Inno Setup (build.yml does); installer.iss leaves the video out if the exe is missing.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$fw = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319"
$csc = Join-Path $fw "csc.exe"
if (-not (Test-Path $csc)) { throw "csc.exe not found: $csc" }
$out = Join-Path $here "..\wizard\GusanSlides.exe"
& $csc /nologo /target:winexe /optimize+ /debug- /warnaserror+ `
  "/out:$out" "/win32manifest:$(Join-Path $here 'app.manifest')" "/lib:$(Join-Path $fw 'WPF')" `
  /r:PresentationFramework.dll /r:PresentationCore.dll /r:WindowsBase.dll /r:System.Xaml.dll `
  (Join-Path $here "GusanSlides.cs")
if ($LASTEXITCODE -ne 0) { throw "csc failed ($LASTEXITCODE)" }
$f = Get-Item $out
Write-Host ("Built {0} ({1:N0} bytes)" -f $f.FullName, $f.Length)
