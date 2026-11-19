from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from clipperhub.releases import ARCHIVE_FILES


def export(destination):
    root = Path(__file__).resolve().parents[1]
    with ZipFile(destination, "x", ZIP_DEFLATED) as archive:
        for name in sorted(ARCHIVE_FILES):
            archive.write(root / name, name)
