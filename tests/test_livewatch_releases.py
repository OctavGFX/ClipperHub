import io
import hashlib
import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from zipfile import ZipFile

import pytest

from clipperhub import releases
from release_helpers import export


def test_exported_release_runs_guarded_download_outside_repository(tmp_path, monkeypatch):
    pytest.importorskip("yt_dlp")
    archive_path = tmp_path / "ClipperHub.zip"
    export(archive_path)
    installation = tmp_path / "portable"
    with ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == releases.ARCHIVE_FILES
        archive.extractall(installation)
    blob = archive_path.read_bytes()
    monkeypatch.setattr(releases, "urlopen", lambda *args, **kwargs: io.BytesIO(blob))
    latest = {"version": releases.__version__, "patch": {"tag": f"v{releases.__version__}",
              "size": len(blob), "digest": hashlib.sha256(blob).hexdigest()}}
    files = releases.download_update(latest)
    releases.install_update(installation, files, ())
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "SSRF_ALLOW_HOSTS", "PROXY_URL", "STATIC_PROXY_URLS", "BGUTIL_BASE_URL"):
        environment.pop(name, None)
    check = subprocess.run([sys.executable, "-B", "-c", """
import socket
from pathlib import Path
import security_utils
import ssrf_guard
from clipperhub import director, procs, webui
import yt_dlp.version
for module in (security_utils, ssrf_guard, director, webui):
    assert Path(module.__file__).resolve().is_relative_to(Path.cwd()), module.__file__
with ssrf_guard.guarded():
    try:
        socket.getaddrinfo("127.0.0.1", 80)
    except ssrf_guard.BlockedAddressError:
        pass
    else:
        raise AssertionError("guard allowed loopback")
result = procs.run([*procs.guarded_ytdlp(), "--ignore-config", "--version"], text=True, timeout=30)
assert result.returncode == 0, result.stderr
assert result.stdout.strip() == yt_dlp.version.__version__, result.stdout
"""], cwd=installation, env=environment, capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stdout + check.stderr


@pytest.mark.parametrize(("tag", "prerelease", "status"), [
    ("v1.0.0", False, "current"),
    ("v1.0.10", False, "update"),
    ("v0.9.9", False, "current"),
    ("v2.0.0-rc.1", True, "error"),
    ("v2.0.0/extra", False, "error"),
])
def test_stable_release_check(monkeypatch, tag, prerelease, status):
    monkeypatch.setattr(releases, "__version__", "1.0.0")

    def open_release(request, *, timeout):
        assert request.full_url == f"https://api.github.com/repos/{releases.REPOSITORY}/releases/latest"
        assert not request.has_header("Authorization")
        return io.BytesIO(json.dumps({"tag_name": tag, "prerelease": prerelease,
                                      "html_url": "https://example.test/not-the-release"}).encode())

    monkeypatch.setattr(releases, "urlopen", open_release)
    result = releases.check_latest()
    assert result["status"] == status
    if status == "update":
        assert result["version"] == "1.0.10"
        assert result["url"] == f"https://github.com/{releases.REPOSITORY}/releases/tag/v1.0.10"
    else:
        assert "url" not in result


@pytest.mark.parametrize(("exc", "status"), [
    (HTTPError("", 404, "", None, None), "unavailable"),
    (HTTPError("", 403, "", None, None), "error"),
    (URLError("offline"), "error"),
])
def test_github_failure_does_not_offer_update(monkeypatch, exc, status):
    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(releases, "urlopen", fail)
    result = releases.check_latest()
    assert result["status"] == status and result["message"] and "url" not in result


def test_new_release_uses_a_separate_asset_for_safe_legacy_upgrade(monkeypatch):
    monkeypatch.setattr(releases, "__version__", "1.0.1")
    asset = {"name": "ClipperHub-Windows-v1.1.0.zip", "digest": "sha256:" + "1" * 64, "size": 123}
    payload = {"tag_name": "v1.1.0", "assets": [asset]}
    monkeypatch.setattr(releases, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    assert releases.check_latest()["patch"]["size"] == 123
    asset["name"] = "ClipperHub-v1.1.0.zip"
    assert "patch" not in releases.check_latest()


def test_update_replaces_only_application_files(tmp_path, monkeypatch):
    protected = {".env", "livewatch.toml", "livewatch_data/clips/clip.mp4",
                 "livewatch_data/schedule.json", "livewatch_backups/fixture.toml", "requirements.txt"}
    for name in protected | releases.RUNTIME_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old")
    archive = io.BytesIO()
    with ZipFile(archive, "w") as patch:
        for name in releases.ARCHIVE_FILES:
            patch.writestr(name, '__version__ = "1.1.0"\n' if name == "clipperhub/__init__.py" else "new")
    blob = archive.getvalue()
    monkeypatch.setattr(releases, "urlopen", lambda *args, **kwargs: io.BytesIO(blob))
    latest = {"version": "1.1.0", "patch": {"tag": "v1.1.0", "size": len(blob),
                                          "digest": hashlib.sha256(blob).hexdigest()}}
    files = releases.download_update(latest)
    with pytest.raises(releases.UpdateError, match="overlaps user data"):
        releases.install_update(tmp_path, files, (tmp_path / "clipperhub" / "config.py",))
    releases.install_update(tmp_path, files, tuple(tmp_path / name for name in protected))
    assert (tmp_path / "clipperhub" / "config.py").read_bytes() == b"new"
    assert all((tmp_path / name).read_bytes() == b"old" for name in protected)
    latest["patch"]["digest"] = "0" * 64
    with pytest.raises(releases.UpdateError, match="checksum"):
        releases.download_update(latest)


def test_update_rolls_back_after_write_failure(tmp_path, monkeypatch):
    files = dict.fromkeys(sorted(releases.RUNTIME_FILES), b"new")
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    replace = releases.os.replace

    def fail_second(source, target):
        if source.name == "1.new":
            raise OSError("file locked")
        replace(source, target)

    monkeypatch.setattr(releases.os, "replace", fail_second)
    with pytest.raises(OSError, match="file locked"):
        releases.install_update(tmp_path, files, ())
    assert all((tmp_path / name).read_bytes() == b"original" for name in files)
