#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Q3Elite Launcher self-updater.

Remote:
  Launcher_Version.json  -> version/update type/changelog
  Launcher_manifest.json -> SHA-256 manifest, fetched only when update/repair is needed

Only Q3Elite/Launcher/ files may be updated.
"""

import hashlib
import json
import os
import subprocess
import shutil
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
REMOTE_VERSION_URL = f"{REMOTE_ROOT}/Q3Elite/Launcher/Launcher_Version.json"
REMOTE_MANIFEST_URL = f"{REMOTE_ROOT}/Q3Elite/Launcher/Launcher_manifest.json"

LOCAL_VERSION_FILE = LAUNCHER_DIR / "Launcher_Version.json"
LOCAL_MANIFEST_FILE = LAUNCHER_DIR / "Launcher_manifest.json"

SELF_UPDATE_DIR = Q3ELITE_LAUNCHER_DATA_DIR / "self_update"
STAGING_DIR = SELF_UPDATE_DIR / "staging"
PENDING_FILE = SELF_UPDATE_DIR / "pending_update.json"

HELPER_SOURCE_PATH = MODULES_DIR / "launcher_update_helper.py"
HELPER_RUN_PATH = SELF_UPDATE_DIR / "launcher_update_helper.py"
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


def _local_version_info():
    try:
        data = json.loads(
            LOCAL_VERSION_FILE.read_text(encoding="utf-8-sig")
        )
        if not isinstance(data, dict):
            raise ValueError("Local Launcher_Version.json is invalid.")
        return data
    except Exception:
        return {"version": "unknown"}


def _local_version():
    return str(_local_version_info().get("version", "unknown")).strip() or "unknown"


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
        raise ValueError("Launcher_manifest.json is empty or invalid.")

    # v1 compatibility: {repo_path: sha256}
    if "files" not in data:
        raw_files = data
        raw_deleted = []
    else:
        raw_files = data.get("files", {})
        raw_deleted = data.get("deleted", [])

    if not isinstance(raw_files, dict):
        raise ValueError("Manifest 'files' must be an object.")
    if not isinstance(raw_deleted, list):
        raise ValueError("Manifest 'deleted' must be an array.")

    parsed_files = []
    for raw_path, raw_hash in raw_files.items():
        repo_path, launcher_relative = _normalize_manifest_path(raw_path)
        expected_hash = str(raw_hash).strip().lower()
        if len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
            raise ValueError(f"Invalid SHA-256 for {repo_path}")
        parsed_files.append({
            "repo_path": repo_path,
            "launcher_relative": launcher_relative,
            "sha256": expected_hash,
        })

    deleted = []
    for raw_path in raw_deleted:
        repo_path, launcher_relative = _normalize_manifest_path(raw_path)
        deleted.append({
            "repo_path": repo_path,
            "launcher_relative": launcher_relative,
        })

    return {
        "files": parsed_files,
        "deleted": deleted,
        "raw": {
            "format": 2,
            "files": {e["repo_path"]: e["sha256"] for e in parsed_files},
            "deleted": [e["repo_path"] for e in deleted],
        },
    }


def _github_tree_files():
    api_url = (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/"
        f"git/trees/{GITHUB_BRANCH}?recursive=1"
    )
    data = _read_json_url(api_url, timeout=15)
    if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
        raise ValueError("GitHub tree response is invalid.")

    excluded = {
        "Q3Elite/Launcher/Launcher_Version.json",
        "Q3Elite/Launcher/Launcher_manifest.json",
        "Q3Elite/Launcher/update/generate_launcher_manifest.py",
    }
    entries = []
    for item in data["tree"]:
        repo_path = str(item.get("path", "")).replace("\\", "/")
        if item.get("type") != "blob" or not repo_path.startswith(ALLOWED_PREFIX) or repo_path in excluded:
            continue
        _, rel = _normalize_manifest_path(repo_path)
        entries.append({"repo_path": repo_path, "launcher_relative": rel, "sha256": None})
    if not entries:
        raise ValueError("No launcher files found in GitHub tree.")
    return entries


def _all_remote_launcher_files_without_manifest():
    return _github_tree_files()


def get_remote_version_info():
    data = _read_json_url(REMOTE_VERSION_URL)
    if not isinstance(data, dict) or not str(data.get("version", "")).strip():
        raise ValueError("Remote Launcher_Version.json is invalid.")
    return data


def get_remote_version():
    return str(get_remote_version_info()["version"]).strip()


def get_remote_manifest():
    return _parse_manifest(_read_json_url(REMOTE_MANIFEST_URL))


def get_local_manifest():
    if not LOCAL_MANIFEST_FILE.is_file():
        return None
    try:
        return _parse_manifest(json.loads(LOCAL_MANIFEST_FILE.read_text(encoding="utf-8")))
    except Exception as error:
        print(f"[warning] Local launcher manifest is invalid: {error}")
        return None


def _find_changed_files(files):
    changed = []
    for entry in files:
        local = LAUNCHER_DIR / entry["launcher_relative"]
        if not local.is_file():
            changed.append(entry)
            continue
        if entry.get("sha256") and _sha256(local) != entry["sha256"]:
            changed.append(entry)
    return changed


def _find_missing_files(files):
    return [
        entry for entry in files
        if not (LAUNCHER_DIR / entry["launcher_relative"]).is_file()
    ]


def _existing_deleted_entries(deleted):
    return [
        entry for entry in deleted
        if (LAUNCHER_DIR / entry["launcher_relative"]).exists()
    ]


def check_for_update():
    """
    Fast normal startup:
      * compare remote/local version first;
      * same version -> use LOCAL manifest and check existence only (no hashes);
      * different version -> fetch REMOTE manifest and hash/compare files;
      * missing metadata -> bootstrap fallback.
    """
    local_info = _local_version_info()
    local_version = str(local_info.get("version", "unknown")).strip() or "unknown"
    local_version_missing = not LOCAL_VERSION_FILE.is_file()

    try:
        remote_info = get_remote_version_info()
        remote_version = str(remote_info["version"]).strip()
    except Exception as version_error:
        print(f"[warning] Launcher version metadata unavailable: {version_error}")
        print("Trying full launcher bootstrap from GitHub...")
        try:
            files = _all_remote_launcher_files_without_manifest()
        except Exception as tree_error:
            print(f"[offline] Launcher update check unavailable: {tree_error}")
            return None
        return {
            "local_version": local_version,
            "remote_version": local_version,
            "remote_version_info": local_info if not local_version_missing else {
                "version": "unknown", "update_type": "optional", "changelog": []
            },
            "files": files,
            "deleted": [],
            "manifest_data": None,
            "full_bootstrap": True,
        }

    print(f"Local launcher version:  {local_version}")
    print(f"Remote launcher version: {remote_version}")

    # SAME VERSION: no remote manifest download and no hashing.
    if local_version == remote_version and not local_version_missing:
        local_manifest = get_local_manifest()
        if local_manifest is None:
            print("Local Launcher_manifest.json is missing.")
            print("Downloading remote manifest for repair...")
            try:
                remote_manifest = get_remote_manifest()
            except Exception as error:
                print(f"[offline] Launcher manifest unavailable: {error}")
                return None
            missing = _find_missing_files(remote_manifest["files"])
            deletions = _existing_deleted_entries(remote_manifest["deleted"])
            if not missing and not deletions:
                # Still install the missing local manifest through helper.
                return {
                    "local_version": local_version,
                    "remote_version": remote_version,
                    "remote_version_info": remote_info,
                    "files": [],
                    "deleted": [],
                    "manifest_data": remote_manifest["raw"],
                }
            return {
                "local_version": local_version,
                "remote_version": remote_version,
                "remote_version_info": remote_info,
                "files": missing,
                "deleted": deletions,
                "manifest_data": remote_manifest["raw"],
            }

        missing = _find_missing_files(local_manifest["files"])
        deletions = _existing_deleted_entries(local_manifest["deleted"])

        if not missing and not deletions:
            print("Launcher is up to date. Local file list is complete.")
            return None

        if missing:
            print(f"Launcher repair required: {len(missing)} file(s) missing.")
        if deletions:
            print(f"Launcher cleanup required: {len(deletions)} obsolete file(s).")

        return {
            "local_version": local_version,
            "remote_version": remote_version,
            "remote_version_info": remote_info,
            "files": missing,
            "deleted": deletions,
            "manifest_data": local_manifest["raw"],
        }

    print("New launcher version available.")
    print("Downloading launcher manifest...")

    try:
        remote_manifest = get_remote_manifest()
    except Exception as manifest_error:
        print(f"[warning] Launcher manifest unavailable: {manifest_error}")
        print("Falling back to complete Launcher download from GitHub...")
        try:
            files = _all_remote_launcher_files_without_manifest()
        except Exception as tree_error:
            print(f"[offline] Launcher file list unavailable: {tree_error}")
            return None
        return {
            "local_version": local_version,
            "remote_version": remote_version,
            "remote_version_info": remote_info,
            "files": files,
            "deleted": [],
            "manifest_data": None,
            "full_bootstrap": True,
        }

    changed = remote_manifest["files"] if local_version_missing else _find_changed_files(remote_manifest["files"])
    deletions = _existing_deleted_entries(remote_manifest["deleted"])

    # Even if payload bytes are already current, helper must commit version +
    # local manifest and perform deletions.
    return {
        "local_version": local_version,
        "remote_version": remote_version,
        "remote_version_info": remote_info,
        "files": changed,
        "deleted": deletions,
        "manifest_data": remote_manifest["raw"],
        "full_bootstrap": local_version_missing,
    }


def check_for_repair():
    try:
        remote_info = get_remote_version_info()
        remote_version = str(remote_info["version"]).strip()
        manifest = get_remote_manifest()
        changed = _find_changed_files(manifest["files"])
        deletions = _existing_deleted_entries(manifest["deleted"])
    except Exception as error:
        print(f"[offline] Launcher repair check unavailable: {error}")
        return None

    if not changed and not deletions:
        print("Launcher integrity check passed.")
        return None

    return {
        "local_version": _local_version(),
        "remote_version": remote_version,
        "remote_version_info": remote_info,
        "files": changed,
        "deleted": deletions,
        "manifest_data": manifest["raw"],
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
        expected_hash = entry.get("sha256")

        if expected_hash and actual_hash != expected_hash:
            try:
                downloaded.unlink()
            except OSError:
                pass

            raise RuntimeError(
                f"SHA-256 mismatch for {entry['launcher_relative']}.\n"
                f"Expected: {expected_hash}\n"
                f"Actual:   {actual_hash}"
            )

        # With the GitHub-tree bootstrap fallback there is no generated
        # manifest hash available. Record the hash of the staged bytes so the
        # detached helper still verifies staging before applying it.
        staged_files.append({
            "relative": entry["launcher_relative"],
            "sha256": expected_hash or actual_hash,
        })

    pending = {
        "version": update_info["remote_version"],
        "version_info": update_info["remote_version_info"],
        "launcher_dir": str(LAUNCHER_DIR),
        "launch_path": str(LAUNCH_PATH),
        "staging_dir": str(STAGING_DIR),
        "files": staged_files,
        "deleted": [
            entry["launcher_relative"]
            for entry in update_info.get("deleted", [])
        ],
        "manifest_data": update_info.get("manifest_data"),
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
    if not HELPER_SOURCE_PATH.is_file():
        raise FileNotFoundError(
            f"Update helper not found: {HELPER_SOURCE_PATH}"
        )

    if not PENDING_FILE.is_file():
        raise FileNotFoundError(f"Pending update not found: {PENDING_FILE}")

    # Run the helper from AppData, outside Q3Elite/Launcher. This allows the
    # update to safely replace launcher_update_helper.py itself after exit.
    SELF_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HELPER_SOURCE_PATH, HELPER_RUN_PATH)

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
            str(HELPER_RUN_PATH),
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
