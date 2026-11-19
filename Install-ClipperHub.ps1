#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Ask', 'None', 'Text', 'TextAndVision')]
    [string]$Ollama = 'Ask',
    [ValidateSet('Ask', 'None', 'Direct')]
    [string]$Instagram = 'Ask',
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$app = Join-Path $env:LOCALAPPDATA 'ClipperHub'
if (Test-Path -LiteralPath $app) {
    throw "ClipperHub already exists at '$app'. This installer will not overwrite it."
}

function Refresh-Path {
    $env:Path = (@(
        $env:Path
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
        [Environment]::GetEnvironmentVariable('Path', 'User')
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama')
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher')
        (Join-Path $env:ProgramFiles 'WinGet\Links')
    ) | Where-Object { $_ }) -join ';'
}

function Install-Package([string]$Id) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Installing $Id requires WinGet. Install or update App Installer from the Microsoft Store, then retry."
    }
    & winget.exe install --id $Id --exact --source winget --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "WinGet could not install $Id (exit $LASTEXITCODE)." }
}

function Has-Python {
    if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) { return $false }
    $ErrorActionPreference = 'Continue'
    & py.exe -3.12 -V 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Find-Cloudflared {
    $command = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($directory in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if ($directory) {
            $candidate = Join-Path $directory 'cloudflared\cloudflared.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
}

function Has-VCRuntime {
    foreach ($dll in @('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $env:SystemRoot "System32\$dll") -PathType Leaf)) {
            return $false
        }
    }
    return $true
}

function Install-Runtime([string]$InstagramMode) {
    Refresh-Path
    if (-not (Has-Python)) { Install-Package 'Python.Python.3.12' }
    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) -or
        -not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)) { Install-Package 'Gyan.FFmpeg' }
    if (-not (Get-Command deno.exe -ErrorAction SilentlyContinue)) { Install-Package 'DenoLand.Deno' }
    if (-not (Has-VCRuntime)) { Install-Package 'Microsoft.VCRedist.2015+.x64' }
    if ($InstagramMode -eq 'Direct' -and -not (Find-Cloudflared)) { Install-Package 'Cloudflare.cloudflared' }
    Refresh-Path
    if (-not (Has-Python)) { throw 'Python 3.12 launcher not found. Open a new PowerShell and retry.' }
    if (-not (Has-VCRuntime)) { throw 'The Visual C++ x64 runtime required by Whisper is missing after installation.' }
    foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe', 'deno.exe')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool not found. Open a new PowerShell and retry." }
    }
    if ($InstagramMode -eq 'Direct' -and -not (Find-Cloudflared)) {
        throw 'cloudflared not found after installation. Open a new PowerShell and retry.'
    }
    $filters = & ffmpeg.exe -hide_banner -filters
    if ($LASTEXITCODE -ne 0 -or -not ($filters -match '\bass\b')) {
        throw 'FFmpeg needs the ass/libass subtitle filter. Install the full Gyan.FFmpeg build and retry.'
    }
    $encoders = & ffmpeg.exe -hide_banner -encoders
    if ($LASTEXITCODE -ne 0 -or -not ($encoders -match '\blibx264\b') -or -not ($encoders -match '\baac\b')) {
        throw 'FFmpeg needs libx264 and AAC encoders. Install the full Gyan.FFmpeg build and retry.'
    }
}

function Test-ReleaseArchive([string]$Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName })
        $required = @('clipperhub/__init__.py', 'clipperhub/__main__.py', 'clipperhub/webui.py',
            'clipperhub/director.py', 'clipperhub/web/index.html', 'requirements.txt',
            'ClipperHub.bat', 'clipperhub.example.toml', 'ssrf_guard.py', 'security_utils.py',
            'fonts/Montserrat-ExtraBold.ttf', 'fonts/Montserrat-OFL.txt')
        $missing = @($required | Where-Object { $_ -notin $names })
        if ($missing.Count) {
            throw "Incomplete release ZIP (missing: $($missing -join ', ')). No application was installed. Use a corrected ClipperHub release."
        }
    } finally {
        $archive.Dispose()
    }
}

function Test-Ollama {
    try {
        $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2
        return $true
    } catch {
        if ($_.Exception -is [System.Net.WebException] -or
            $_.Exception.GetType().FullName -in @('System.Net.Http.HttpRequestException', 'Microsoft.PowerShell.Commands.HttpResponseException')) {
            return $false
        }
        throw
    }
}

if ($Ollama -eq 'Ask') {
    do { $choice = Read-Host 'Local AI: [0] None (default), [1] Text, [2] Text + vision' }
    while ($choice -notin @('', '0', '1', '2'))
    $Ollama = switch ($choice) { '1' { 'Text' } '2' { 'TextAndVision' } default { 'None' } }
}

if ($Instagram -eq 'Ask') {
    do { $choice = Read-Host 'Direct Instagram publishing: [0] No (default), [1] Yes (install cloudflared)' }
    while ($choice -notin @('', '0', '1'))
    $Instagram = if ($choice -eq '1') { 'Direct' } else { 'None' }
}

$repo = 'OctavGFX/ClipperHub'
$release = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -UserAgent 'ClipperHub Installer'
if ($release.tag_name -notmatch '^v\d+\.\d+\.\d+$' -or $release.draft -or $release.prerelease) {
    throw 'GitHub did not return a stable ClipperHub release.'
}
$zipName = "ClipperHub-Windows-$($release.tag_name).zip"
$asset = $release.assets | Where-Object { $_.name -eq $zipName } | Select-Object -First 1
if (-not $asset -or $asset.digest -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'The ClipperHub ZIP or its SHA-256 checksum is missing.'
}

$download = New-TemporaryFile
$zip = "$($download.FullName).zip"
Move-Item -LiteralPath $download.FullName -Destination $zip
try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$repo/releases/download/$($release.tag_name)/$zipName" -OutFile $zip
    if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $asset.digest.Substring(7)) {
        throw 'The downloaded ZIP does not match the SHA-256 checksum from GitHub.'
    }
    Test-ReleaseArchive $zip
    Install-Runtime $Instagram
    New-Item -Path $app -ItemType Directory | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $app
} finally {
    Remove-Item -LiteralPath $zip
}

& py.exe -3.12 -m venv (Join-Path $app '.venv')
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
& (Join-Path $app '.venv\Scripts\python.exe') -m pip install -r (Join-Path $app 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install the Python dependencies.' }
$env:Path = (Join-Path $app '.venv\Scripts') + ';' + $env:Path
Push-Location $app
try {
    & (Join-Path $app '.venv\Scripts\python.exe') -B -c "import requests, streamlink, yt_dlp, faster_whisper; import ssrf_guard, security_utils; from clipperhub import webui, director, procs; import subprocess; subprocess.run([*procs.guarded_ytdlp(), '--ignore-config', '--version'], check=True)"
    if ($LASTEXITCODE -ne 0) { throw "Runtime verification failed. ClipperHub files are at '$app'; see the error above." }
} finally {
    Pop-Location
}

$launcher = Join-Path $app 'Start-ClipperHub.cmd'
@'
@echo off
cd /d "%~dp0"
set "PATH=%~dp0.venv\Scripts;%LOCALAPPDATA%\Microsoft\WinGet\Links;%ProgramFiles%\WinGet\Links;%PATH%"
call "%~dp0ClipperHub.bat"
'@ | Set-Content -LiteralPath $launcher -Encoding Ascii

$config = @('[post]', 'delivery_mode = "manual"', '', '[ai]', 'provider = "off"')
if ($Ollama -ne 'None') {
    if (-not (Get-Command ollama.exe -ErrorAction SilentlyContinue)) { Install-Package 'Ollama.Ollama' }
    Refresh-Path
    $ollamaExe = (Get-Command ollama.exe).Source
    if (-not (Test-Ollama)) {
        Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
    }
    for ($i = 0; $i -lt 15; $i++) {
        if (Test-Ollama) { break }
        Start-Sleep -Seconds 1
    }
    if ($i -eq 15) { throw "Ollama did not start. ClipperHub was installed at '$app'; start Ollama and configure AI in Settings." }
    $model = 'qwen3:4b-instruct'
    $config = @('[post]', 'delivery_mode = "manual"', '', '[ai]', 'provider = "local"',
        'local_url = "http://127.0.0.1:11434/v1"', "local_model = `"$model`"")
    $models = @($model)
    if ($Ollama -eq 'TextAndVision') {
        $vision = 'qwen3-vl:4b-instruct-q4_K_M'
        $models += $vision
        $config += "local_vision_model = `"$vision`""
    }
    foreach ($id in $models) {
        & $ollamaExe pull $id
        if ($LASTEXITCODE -ne 0) { throw "Could not download $id. ClipperHub was installed at '$app'." }
    }
}
New-Item -Path (Join-Path $app 'livewatch.toml') -ItemType File -Value ($config -join "`n") | Out-Null

Write-Host "Installed ClipperHub at '$app'. Use Start-ClipperHub.cmd to open it again."
Write-Host 'Whisper downloads the selected speech model on first use. NVIDIA/Intel encoding uses an existing compatible driver; CPU encoding remains available.'
Write-Host 'Connect publishing accounts and API keys in Settings. No browser, TTS or GPU toolkit was installed.'
if (-not $NoLaunch) { Start-Process -FilePath $launcher }
