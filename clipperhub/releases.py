"""Check stable ClipperHub releases and install verified application files."""
import ast
import hashlib
import io
import json
import os
import re
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from . import __version__

REPOSITORY = "OctavGFX/ClipperHub"
CHECK_INTERVAL = 5 * 60
MAX_PATCH = 16 * 1024 * 1024
_TAG = re.compile(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
MODULES = """
__init__ __main__ captions chat config delivery_queue detector facecam local_ai music
poster procs publish quality_video recorder releases render replay service settings_schema
tomlwrite transcribe video_watch viral webui youtube_bridge
""".split()
RUNTIME_FILES = frozenset(f"clipperhub/{name}.py" for name in MODULES) | {
    "clipperhub/web/index.html", "fonts/Montserrat-ExtraBold.ttf", "fonts/Montserrat-OFL.txt",
}
ARCHIVE_FILES = RUNTIME_FILES | {
    ".gitignore", "LICENSE", "ClipperHub.bat", "clipperhub.example.toml", "requirements.txt",
}


class UpdateError(Exception):
    pass


def check_latest() -> dict:
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "ClipperHub"})
    try:
        with urlopen(request, timeout=8) as response:
            release = json.loads(response.read(2_000_000))
    except HTTPError as exc:
        if exc.code == 404:
            return {"status": "unavailable", "message": "No public ClipperHub release found."}
        return {"status": "error", "message": f"GitHub release check failed (HTTP {exc.code})."}
    except (URLError, TimeoutError, ValueError):
        return {"status": "error", "message": "Could not check GitHub for updates."}
    tag = release.get("tag_name") if isinstance(release, dict) else None
    version = _TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not version or release.get("draft") or release.get("prerelease"):
        return {"status": "error", "message": "GitHub did not return a stable ClipperHub version."}
    latest = tuple(map(int, version.groups()))
    if latest <= tuple(map(int, __version__.split("."))):
        return {"status": "current"}
    number = ".".join(version.groups())
    assets = release.get("assets")
    asset = next((a for a in assets if isinstance(a, dict)
                  and a.get("name") == f"ClipperHub-v{number}.zip"), {}) if isinstance(assets, list) else {}
    digest, size = asset.get("digest"), asset.get("size")
    patch = ({"tag": tag, "digest": digest[7:], "size": size}
             if isinstance(digest, str) and _DIGEST.fullmatch(digest)
             and type(size) is int and 0 < size <= MAX_PATCH else None)
    return {"status": "update", "version": number,
            "url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
            **({"patch": patch} if patch else {})}


def download_update(latest: dict) -> dict[str, bytes]:
    patch = latest["patch"]
    url = f'https://github.com/{REPOSITORY}/releases/download/{patch["tag"]}/ClipperHub-v{latest["version"]}.zip'
    try:
        with urlopen(Request(url, headers={"User-Agent": "ClipperHub"}), timeout=15) as response:
            data = response.read(patch["size"] + 1)
    except OSError as exc:
        raise UpdateError("Could not download the update from GitHub.") from exc
    if len(data) != patch["size"] or hashlib.sha256(data).hexdigest() != patch["digest"]:
        raise UpdateError("The downloaded update did not match GitHub's SHA-256 checksum.")
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if ({entry.filename for entry in entries} != ARCHIVE_FILES or len(entries) != len(ARCHIVE_FILES)
                    or sum(entry.file_size for entry in entries) > MAX_PATCH):
                raise UpdateError("The update does not match the supported application file list.")
            files = {name: archive.read(name) for name in RUNTIME_FILES}
        tree = ast.parse(files["clipperhub/__init__.py"])
        version = next((node.value.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
                        and isinstance(node.value, ast.Constant)), None)
        if version != latest["version"]:
            raise UpdateError("The archive version does not match the GitHub release.")
        return files
    except (BadZipFile, RuntimeError, SyntaxError, NotImplementedError) as exc:
        raise UpdateError("GitHub returned an invalid update archive.") from exc


def install_update(root: Path, files: dict[str, bytes], protected: tuple[Path, ...]) -> None:
    if files.keys() != RUNTIME_FILES or (root / ".git").exists():
        raise UpdateError("Updates can only replace known files in a portable ClipperHub installation.")
    targets = {name: root / name for name in files}
    for path in targets.values():
        for component in (path, *path.parents):
            if component == root:
                break
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise UpdateError("The installation contains links or redirected application files.")
        if not path.is_file() or any(path == p or p in path.parents for p in protected):
            raise UpdateError("An application file is missing or overlaps user data.")
    with TemporaryDirectory(prefix=".clipperhub-update-", dir=root) as folder:
        stage = Path(folder)
        installed = []
        for index, (name, path) in enumerate(targets.items()):
            (stage / f"{index}.new").write_bytes(files[name])
            (stage / f"{index}.old").write_bytes(path.read_bytes())
        try:
            for index, path in enumerate(targets.values()):
                os.replace(stage / f"{index}.new", path)
                installed.append((index, path))
        except OSError:
            for index, path in reversed(installed):
                os.replace(stage / f"{index}.old", path)
            raise
