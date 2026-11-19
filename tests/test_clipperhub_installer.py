import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from clipperhub import youtube_bridge
from release_helpers import export


INSTALLER = Path(__file__).resolve().parents[1] / "Install-ClipperHub.ps1"
POWERSHELL = shutil.which("powershell.exe")


def _powershell(code):
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is required")
    script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CLIPPERHUB_INSTALLER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$functions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)
. ([scriptblock]::Create(($functions | ForEach-Object { $_.Extent.Text }) -join "`n"))
""" + code
    import os
    env = dict(os.environ, CLIPPERHUB_INSTALLER=str(INSTALLER))
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(("existing", "instagram", "expected"), [
    (False, "None", ["Python.Python.3.12", "Gyan.FFmpeg", "DenoLand.Deno", "Microsoft.VCRedist.2015+.x64"]),
    (False, "Direct", ["Python.Python.3.12", "Gyan.FFmpeg", "DenoLand.Deno",
                       "Microsoft.VCRedist.2015+.x64", "Cloudflare.cloudflared"]),
    (True, "None", []),
    (True, "Direct", ["Cloudflare.cloudflared"]),
])
def test_installer_installs_only_missing_selected_dependencies(existing, instagram, expected):
    result = _powershell(r"""
$script:ready = EXISTING
$script:pythonReady = EXISTING
$script:vcReady = EXISTING
$script:cloudReady = $false
$script:installed = @()
function Refresh-Path {}
function Has-Python { return $script:pythonReady }
function Has-VCRuntime { return $script:vcReady }
function Find-Cloudflared { if ($script:cloudReady) { return 'cloudflared.exe' } }
function Get-Command {
    param($Name, $ErrorAction)
    if ($script:ready) { return [pscustomobject]@{Source = $Name} }
}
function Install-Package {
    param($Id)
    $script:installed += $Id
    switch ($Id) {
        'Python.Python.3.12' { $script:pythonReady = $true }
        'DenoLand.Deno' { $script:ready = $true }
        'Microsoft.VCRedist.2015+.x64' { $script:vcReady = $true }
        'Cloudflare.cloudflared' { $script:cloudReady = $true }
    }
}
function ffmpeg.exe {
    $global:LASTEXITCODE = 0
    if ($args -contains '-filters') { return ' ... ass V->V Render ASS subtitles' }
    return @(' V libx264 ', ' A aac ')
}
Install-Runtime 'INSTAGRAM'
ConvertTo-Json -Compress -InputObject @($script:installed)
""".replace("EXISTING", "$true" if existing else "$false").replace("INSTAGRAM", instagram))
    assert result == expected


def test_installer_refuses_ffmpeg_without_subtitles():
    result = _powershell(r"""
function Refresh-Path {}
function Has-Python { return $true }
function Has-VCRuntime { return $true }
function Get-Command { param($Name, $ErrorAction) return [pscustomobject]@{Source=$Name} }
function Install-Package { throw 'Unexpected installation' }
function ffmpeg.exe { $global:LASTEXITCODE = 0; return 'scale' }
try {
    Install-Runtime 'None'
    throw 'Accepted FFmpeg without subtitle support'
} catch {
    if ($_.Exception.Message -notlike '*ass/libass*') { throw }
    ConvertTo-Json $_.Exception.Message
}
""")
    assert "ass/libass" in result


@pytest.mark.parametrize("missing", [None, "ssrf_guard.py", "security_utils.py", "clipperhub/director.py"])
def test_installer_rejects_incomplete_release_before_installing(tmp_path, missing):
    archive_path = tmp_path / "release.zip"
    export(archive_path)
    if missing:
        with ZipFile(archive_path) as archive:
            files = {name: archive.read(name) for name in archive.namelist() if name != missing}
        with ZipFile(archive_path, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
    path = str(archive_path).replace("'", "''")
    result = _powershell("""
try {
    Test-ReleaseArchive 'ARCHIVE'
    ConvertTo-Json 'complete'
} catch {
    ConvertTo-Json $_.Exception.Message
}
""".replace("ARCHIVE", path))
    if missing:
        assert "Incomplete release ZIP" in result and missing in result
    else:
        assert result == "complete"


def test_public_youtube_extraction_does_not_require_a_browser(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='{"formats": [{"url": "https://example.test/live.m3u8"}]}')

    monkeypatch.setattr(youtube_bridge.subprocess, "run", run)
    assert youtube_bridge.extract("https://www.youtube.com/watch?v=aaaaaaaaaaa")["formats"]
    assert "--cookies-from-browser" not in commands[0]
    assert "firefox" not in commands[0]


def test_youtube_bridge_combines_separate_hls_video_and_audio():
    video = {"url": "https://example.test/video.m3u8", "vcodec": "avc1", "acodec": "none",
             "protocol": "m3u8_native", "height": 720}
    audio = {"url": "https://example.test/audio.m3u8", "vcodec": "none", "acodec": None,
             "protocol": "m3u8_native", "tbr": 128}
    info = {"formats": [video, audio], "http_headers": {"User-Agent": "ClipperHub", "Referer": "https://youtube.com"}}
    selected = youtube_bridge.best_format(info, ["720p,best"])
    assert selected == video
    command = youtube_bridge.stream_command(info, selected)
    assert [command[i + 1] for i, value in enumerate(command) if value == "-i"] == [video["url"], audio["url"]]
    assert [command[i + 1] for i, value in enumerate(command) if value == "-map"] == ["0:v:0", "1:a:0"]
    assert command.count("-user_agent") == 2 and command.count("-headers") == 2
    assert command[-5:] == ["-c", "copy", "-f", "mpegts", "pipe:1"]
    assert youtube_bridge.best_format({"formats": [video]}, ["best"]) is None


def test_youtube_bridge_preserves_muxed_streams_and_quality_limit():
    def fmt(height, audio):
        return {"url": f"https://example.test/{height}-{audio}.m3u8", "vcodec": "avc1", "acodec": audio,
                "protocol": "m3u8_native", "height": height}

    video = fmt(1080, "none")
    muxed = fmt(720, "aac")
    lower = fmt(480, "aac")
    info = {"formats": [video, muxed, lower]}
    selected = youtube_bridge.best_format(info, ["480p,best"])
    assert selected == lower
    command = youtube_bridge.stream_command(info, selected)
    assert command.count("-i") == 1
    assert "0:a:0?" in command and "1:a:0" not in command
