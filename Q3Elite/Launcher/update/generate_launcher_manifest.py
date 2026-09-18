#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Generate Launcher_manifest.json for Q3Elite Launcher.

The script may be placed anywhere inside:
    <repo root>/Q3Elite/Launcher/

It automatically finds the Git repository root and uses Git itself to decide
which Launcher files belong to the project.

Included:
    - tracked files
    - new/untracked files that are NOT ignored by .gitignore

Excluded:
    - everything ignored by .gitignore
    - this generator itself

Output:
    <repo root>/Q3Elite/Launcher/Launcher_manifest.json

Run:
    py generate_launcher_manifest.py
"""

import hashlib
import json
import subprocess
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
LAUNCHER_PREFIX = "Q3Elite/Launcher/"


def run_git(args, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(message or "Git command failed.")

    return result.stdout


def find_repo_root():
    """
    Find the repository root with Git, starting from the generator's folder.
    """
    try:
        root = run_git(
            ["rev-parse", "--show-toplevel"],
            SCRIPT_PATH.parent,
        ).strip()
    except FileNotFoundError:
        raise RuntimeError(
            "Git was not found.\n"
            "Install Git or run the script on a system where git.exe is available."
        )

    if not root:
        raise RuntimeError("Could not determine Git repository root.")

    return Path(root).resolve()


def sha256(path):
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest().lower()


def git_launcher_files(repo_root):
    """
    Return:
      1. files already tracked by Git
      2. new/untracked files that are not ignored by .gitignore

    `git ls-files --others --exclude-standard` makes Git interpret .gitignore,
    .git/info/exclude and the user's standard Git excludes itself.
    """
    tracked = run_git(
        ["ls-files", "--", "Q3Elite/Launcher"],
        repo_root,
    ).splitlines()

    untracked = run_git(
        [
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "Q3Elite/Launcher",
        ],
        repo_root,
    ).splitlines()

    files = set()

    for raw_path in tracked + untracked:
        repo_path = raw_path.strip().replace("\\", "/")

        if not repo_path.startswith(LAUNCHER_PREFIX):
            continue

        absolute_path = (repo_root / Path(repo_path)).resolve()

        # A tracked file may have been deleted locally but not committed yet.
        if not absolute_path.is_file():
            continue

        # Files used to describe/generate the update are metadata/tools,
        # not launcher payload files.
        if repo_path in {
            "Q3Elite/Launcher/Launcher_Version.json",
            "Q3Elite/Launcher/Launcher_manifest.json",
        }:
            continue

        # The developer-only generator should never be distributed by
        # the launcher self-updater, regardless of where it is stored.
        try:
            if absolute_path == SCRIPT_PATH:
                continue
        except OSError:
            pass

        files.add(repo_path)

    return sorted(files)


def main():
    try:
        repo_root = find_repo_root()
    except (RuntimeError, FileNotFoundError) as error:
        raise SystemExit(f"\nERROR: {error}\n")

    launcher_root = repo_root / "Q3Elite" / "Launcher"
    manifest_file = launcher_root / "Launcher_manifest.json"

    if not launcher_root.is_dir():
        raise SystemExit(
            "\nERROR: Q3Elite/Launcher was not found in the Git repository:\n"
            f"  {repo_root}\n"
        )

    try:
        files = git_launcher_files(repo_root)
    except RuntimeError as error:
        raise SystemExit(f"\nERROR: {error}\n")

    print()
    print("Q3Elite Launcher Manifest Generator")
    print("===================================")
    print()
    print(f"Script:   {SCRIPT_PATH}")
    print(f"Repo:     {repo_root}")
    print(f"Launcher: {launcher_root}")
    print(f"Gitignore:{repo_root / '.gitignore'}")
    print()
    print("Using Git tracked + non-ignored untracked files.")
    print("Calculating SHA-256...")
    print()

    manifest = {}

    for repo_path in files:
        absolute_path = repo_root / Path(repo_path)
        manifest[repo_path] = sha256(absolute_path)
        print(f"  {repo_path}")

    manifest_file.write_text(
        json.dumps(manifest, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("========================================")
    print(" Launcher manifest generated")
    print("========================================")
    print()
    print(f"Files:    {len(manifest)}")
    print(f"Manifest: {manifest_file}")
    print()
    print("Ignored files were filtered by Git/.gitignore.")
    print("No SHA-256 values need to be entered manually.")


if __name__ == "__main__":
    main()
