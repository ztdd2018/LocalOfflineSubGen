# fetch_ffmpeg.ps1
# Download Windows FFmpeg (gyan.dev release essentials build) and copy
# ffmpeg.exe / ffprobe.exe into bin\. Run ONCE on the build machine only;
# the final portable package already bundles these files, end users stay offline.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BinDir = Join-Path $Root "bin"
$TmpDir = Join-Path $env:TEMP "ffmpeg_fetch"
$ZipPath = Join-Path $TmpDir "ffmpeg.zip"
$Url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

New-Item -ItemType Directory -Force -Path $BinDir, $TmpDir | Out-Null

Write-Host "Downloading FFmpeg: $Url"
Invoke-WebRequest -Uri $Url -OutFile $ZipPath

Write-Host "Extracting..."
Expand-Archive -Path $ZipPath -DestinationPath $TmpDir -Force
$extracted = Get-ChildItem -Path $TmpDir -Directory | Where-Object { $_.Name -like "ffmpeg-*" } | Select-Object -First 1
if (-not $extracted) { throw "ffmpeg directory not found after extraction" }

Copy-Item (Join-Path $extracted.FullName "bin\ffmpeg.exe")  (Join-Path $BinDir "ffmpeg.exe")  -Force
Copy-Item (Join-Path $extracted.FullName "bin\ffprobe.exe") (Join-Path $BinDir "ffprobe.exe") -Force

Remove-Item -Recurse -Force $TmpDir
Write-Host "Done: ffmpeg.exe / ffprobe.exe placed in $BinDir"
