#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Publish metadata generator for Q3Elite Launcher.

Run from repository root:
    py generate_launcher_manifest.py 1.02

Creates/updates:
    launcher_version.txt
    launcher_manifest.json

No SHA-256 values are entered manually.
"""

import hashlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
LAUNCHER_ROOT = REPO_ROOT / "Q3Elite" / "Launcher"

VERSION_FILE = REPO_ROOT / "launcher_version.txt"
MANIFEST_FILE = REPO_ROOT / "launcher_manifest.json"

EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    "cache",
    "temp_files",
}

EXCLUDED_FILES = {
    "launcher_version.txt",
    "launcher_manifest.json",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".part",
    ".update",
    ".log",
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def should_include(path):
    relative = path.relative_to(LAUNCHER_ROOT)

    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False

    if path.name in EXCLUDED_FILES:
        return False

    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False

    return path.is_file()


def main():
    if len(sys.argv) != 2:
        print("Usage: py generate_launcher_manifest.py <version>")
        raise SystemExit(2)

    version = sys.argv[1].strip()

    if not version:
        raise SystemExit("Version cannot be empty.")

    if not LAUNCHER_ROOT.is_dir():
        raise SystemExit(f"Launcher directory not found: {LAUNCHER_ROOT}")

    files = {}

    for path in sorted(LAUNCHER_ROOT.rglob("*")):
        if not should_include(path):
            continue

        repo_relative = path.relative_to(REPO_ROOT).as_posix()
        files[repo_relative] = sha256(path)

    VERSION_FILE.write_text(version + "\n", encoding="utf-8")

    MANIFEST_FILE.write_text(
        json.dumps(files, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("Q3Elite Launcher release metadata generated.")
    print(f"Version:  {version}")
    print(f"Files:    {len(files)}")
    print(f"Version:  {VERSION_FILE}")
    print(f"Manifest: {MANIFEST_FILE}")
    print()
    print("Commit the changed launcher files together with:")
    print("  launcher_version.txt")
    print("  launcher_manifest.json")


if __name__ == "__main__":
    main()
