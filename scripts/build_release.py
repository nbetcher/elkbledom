"""Build a reproducible flat integration ZIP and verify every archived byte."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "elkbledom"


def build_release(output: Path | None = None) -> Path:
    """Build from the current component tree without replacing prior artifacts."""
    version = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))["version"]
    destination = output or ROOT / "dist" / f"elkbledom-{version}.zip"
    files = {
        path.relative_to(COMPONENT).as_posix(): path.read_bytes()
        for path in sorted(COMPONENT.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "x", compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)

    with ZipFile(destination) as archive:
        if set(archive.namelist()) != set(files) or len(archive.namelist()) != len(files):
            raise ValueError("Release ZIP does not contain exactly the integration files")
        for name, data in files.items():
            if archive.read(name) != data:
                raise ValueError(f"Release ZIP differs from source: {name}")
        if archive.testzip() is not None:
            raise ValueError("Release ZIP has an invalid checksum")

    sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"Verified {len(files)} files: {destination}")
    print(f"SHA256 {sha256}")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    build_release(args.output)
