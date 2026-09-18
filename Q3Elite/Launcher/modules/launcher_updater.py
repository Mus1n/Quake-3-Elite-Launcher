#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Q3Elite Launcher self-updater.

Remote:
  launcher_version.txt   -> lightweight version check
  launcher_manifest.json -> SHA-256 manifest, fetched only when update/repair is needed

Only Q3Elite/Launcher/ files may be updated.
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from base_methods import LAUNCHER_DIR, MODULES_DIR, Q3ELITE_LAUNCHER_DATA_DIR
import download_tools as dt


GITHUB_OWNER = "Mus1n"
GITHUB_REPO = "Quake-3-Elite-Launcher"
GITHUB_BRANCH = "main"

REMOTE_ROOT = (
    f"https://raw.githubusercontent.com/"
    f"{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
)
REMOTE_VERSION_URL = f"{REMOTE_ROOT}/launcher_version.txt"
REMOTE_MANIFEST_URL = f"{REMOTE_ROOT}/launcher_manifest.json"

LOCAL_VERSION_FILE = LAUNCHER_DIR / "launcher_version.txt"

SELF_UPDATE_DIR = Q3ELITE_LAUNCHER_DATA_DIR / "self_update"
STAGING_DIR = SELF_UPDATE_DIR / "staging"
PENDING_FILE = SELF_UPDATE_DIR / "pending_update.json"

HELPER_PATH = MODULES_DIR / "launcher_update_helper.py"
LAUNCH_PATH = MODULES_DIR / "launch.pyw"

ALLOWED_PREFIX = "Q3Elite/Launcher/"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _read_text_url(url, timeout=10):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Q3Elite-Launcher-Self-Updater/1.0",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8-sig").strip()


def _read_json_url(url, timeout=10):
    return json.loads(_read_text_url(url, timeout=timeout))


def _local_version():
    try:
        version = LOCAL_VERSION_FILE.read_text(
            encoding="utf-8-sig"
        ).strip()
        return version or "unknown"
    except OSError:
        return "unknown"


def _normalize_manifest_path(value):
    if not isinstance(value, str):
        raise ValueError("Manifest file path must be a string.")

    value = value.replace("\\", "/").lstrip("/")
    path = PurePosixPath(value)

    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe manifest path: {value}")

    normalized = path.as_posix()

    if not normalized.startswith(ALLOWED_PREFIX):
        raise ValueError(
            f"Self-update path is outside {ALLOWED_PREFIX}: {normalized}"
        )

    launcher_relative = normalized[len(ALLOWED_PREFIX):]

    if not launcher_relative:
        raise ValueError(f"Invalid launcher path: {normalized}")

    return normalized, launcher_relative


def _parse_manifest(data):
    if not isinstance(data, dict) or not data:
        raise ValueError("launcher_manifest.json is empty or invalid.")

    parsed = []

    for raw_path, raw_hash in data.items():
        repo_path, launcher_relative = _normalize_manifest_path(raw_path)
        expected_hash = str(raw_hash).strip().lower()

        if (
            len(expected_hash) != 64
            or any(c not in "0123456789abcdef" for c in expected_hash)
        ):
            raise ValueError(f"Invalid SHA-256 for {repo_path}")

        parsed.append({
            "repo_path": repo_path,
            "launcher_relative": launcher_relative,
            "sha256": expected_hash,
        })

    return parsed


def get_remote_version():
    version = _read_text_url(REMOTE_VERSION_URL)
    if not version:
        raise ValueError("Remote launcher_version.txt is empty.")
    return version


def get_remote_manifest():
    return _parse_manifest(_read_json_url(REMOTE_MANIFEST_URL))


def _find_changed_files(manifest):
    changed = []

    for entry in manifest:
        local_path = LAUNCHER_DIR / Path(entry["launcher_relative"])

        if not local_path.is_file():
            changed.append(entry)
            continue

        try:
            if _sha256(local_path) != entry["sha256"]:
                changed.append(entry)
        except OSError:
            changed.append(entry)

    return changed


def check_for_update():
    """
    Fast normal-start check.

    If versions match, no manifest is downloaded and no local hashes are scanned.
    GitHub/network failure is treated as offline and never blocks the launcher.
    """
    local_version = _local_version()

    try:
        remote_version = get_remote_version()
    except Exception as error:
        print(f"[offline] Launcher update check unavailable: {error}")
        return None

    print(f"Local launcher version:  {local_version}")
    print(f"Remote launcher version: {remote_version}")

    if local_version == remote_version:
        print("Launcher is up to date.")
        return None

    print("New launcher version available.")
    print("Downloading launcher manifest...")

    try:
        manifest = get_remote_manifest()
        changed = _find_changed_files(manifest)
    except Exception as error:
        print(f"[offline] Launcher manifest unavailable: {error}")
        return None

    # Version changed but files are already identical (e.g. interrupted final
    # version write). Only synchronize the local version.
    if not changed:
        LOCAL_VERSION_FILE.write_text(
            remote_version + "\n",
            encoding="utf-8",
        )
        print("Launcher files are already current; version synchronized.")
        return None

    return {
        "local_version": local_version,
        "remote_version": remote_version,
        "files": changed,
    }


def check_for_repair():
    """
    Full integrity check. Unlike normal startup this always downloads the
    manifest and hashes all launcher files listed there.
    """
    try:
        remote_version = get_remote_version()
        manifest = get_remote_manifest()
        changed = _find_changed_files(manifest)
    except Exception as error:
        print(f"[offline] Launcher repair check unavailable: {error}")
        return None

    if not changed:
        print("Launcher integrity check passed.")
        return None

    return {
        "local_version": _local_version(),
        "remote_version": remote_version,
        "files": changed,
    }


def stage_update(update_info, control=None, progress_callback=None):
    """
    Download only changed/missing files into AppData and verify SHA-256.
    """
    SELF_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    staged_files = []

    for entry in update_info["files"]:
        relative = Path(entry["launcher_relative"])
        staged_path = STAGING_DIR / relative
        staged_path.parent.mkdir(parents=True, exist_ok=True)

        url = (
            f"{REMOTE_ROOT}/"
            f"{urllib.parse.quote(entry['repo_path'], safe='/')}"
        )

        print(f"Updating launcher file: {entry['launcher_relative']}")

        downloaded = dt.downloader(
            url,
            str(staged_path.parent),
            staged_path.name,
            skip=False,
            control=control,
            progress_callback=progress_callback,
            use_part_file=True,
        )

        if not downloaded:
            raise RuntimeError(
                f"Failed to download: {entry['launcher_relative']}"
            )

        downloaded = Path(downloaded)
        actual_hash = _sha256(downloaded)

        if actual_hash != entry["sha256"]:
            try:
                downloaded.unlink()
            except OSError:
                pass

            raise RuntimeError(
                f"SHA-256 mismatch for {entry['launcher_relative']}.\n"
                f"Expected: {entry['sha256']}\n"
                f"Actual:   {actual_hash}"
            )

        staged_files.append({
            "relative": entry["launcher_relative"],
            "sha256": entry["sha256"],
        })

    pending = {
        "version": update_info["remote_version"],
        "launcher_dir": str(LAUNCHER_DIR),
        "launch_path": str(LAUNCH_PATH),
        "staging_dir": str(STAGING_DIR),
        "files": staged_files,
    }

    PENDING_FILE.write_text(
        json.dumps(pending, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return len(staged_files)


def launch_apply_helper(parent_pid=None):
    """
    Start detached helper. Caller should quit its Qt application immediately
    after this succeeds.
    """
    if not HELPER_PATH.is_file():
        raise FileNotFoundError(f"Update helper not found: {HELPER_PATH}")

    if not PENDING_FILE.is_file():
        raise FileNotFoundError(f"Pending update not found: {PENDING_FILE}")

    if parent_pid is None:
        parent_pid = os.getpid()

    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )

    subprocess.Popen(
        [
            sys.executable,
            str(HELPER_PATH),
            "--apply",
            str(PENDING_FILE),
            "--wait-pid",
            str(parent_pid),
        ],
        cwd=str(LAUNCHER_DIR),
        close_fds=True,
        creationflags=creationflags,
    )

    return True
