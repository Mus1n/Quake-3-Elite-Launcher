#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Detached Q3Elite Launcher update helper.

This file deliberately uses only the Python standard library. It must keep
working while the rest of the launcher is being replaced.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def pid_exists(pid):
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000

        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
            False,
            pid,
        )
        if not handle:
            return False

        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            return result == 0x00000102  # WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_exit(pid, timeout=30):
    deadline = time.monotonic() + timeout
    while pid_exists(pid):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Launcher process {pid} did not exit within {timeout}s."
            )
        time.sleep(0.2)


def atomic_copy(source, destination):
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_name(destination.name + ".new")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def restart_launcher(launch_path, launcher_dir):
    launch_path = Path(launch_path)
    launcher_dir = Path(launcher_dir)

    if not launch_path.is_file():
        raise FileNotFoundError(f"Launcher entrypoint not found: {launch_path}")

    pythonw = Path(sys.executable)
    if pythonw.name.lower() == "python.exe":
        candidate = pythonw.with_name("pythonw.exe")
        if candidate.is_file():
            pythonw = candidate

    subprocess.Popen(
        [str(pythonw), str(launch_path)],
        cwd=str(launcher_dir),
        close_fds=True,
    )


def apply_update(pending_path, wait_pid):
    pending_path = Path(pending_path)
    data = json.loads(pending_path.read_text(encoding="utf-8"))

    launcher_dir = Path(data["launcher_dir"]).resolve()
    staging_dir = Path(data["staging_dir"]).resolve()
    launch_path = Path(data["launch_path"]).resolve()

    wait_for_exit(wait_pid)

    # Verify the complete staged set once more after the launcher has exited.
    for entry in data["files"]:
        source = staging_dir / entry["relative"]
        if not source.is_file():
            raise FileNotFoundError(f"Staged update file missing: {source}")

        actual = sha256(source)
        if actual != entry["sha256"]:
            raise RuntimeError(
                f"Staged SHA-256 mismatch: {entry['relative']}"
            )

    # Apply changed files.
    for entry in data["files"]:
        source = staging_dir / entry["relative"]
        destination = launcher_dir / entry["relative"]
        atomic_copy(source, destination)

    # Remove files explicitly retired by the cumulative manifest.
    # Safety: only relative paths below launcher_dir are accepted.
    for relative in data.get("deleted", []):
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(f"Unsafe deleted path: {relative}")
        target = (launcher_dir / rel).resolve()
        try:
            target.relative_to(launcher_dir)
        except ValueError:
            raise RuntimeError(f"Deleted path escapes Launcher directory: {relative}")
        if target.is_file() or target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)

    # Save the exact manifest used for this update locally. On equal-version
    # starts the launcher uses it only to check file existence (fast path).
    manifest_data = data.get("manifest_data")
    if manifest_data is not None:
        manifest_file = launcher_dir / "Launcher_manifest.json"
        manifest_temp = manifest_file.with_name(manifest_file.name + ".new")
        manifest_temp.write_text(
            json.dumps(manifest_data, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_temp, manifest_file)

    # Commit Launcher_Version.json LAST. If any operation above fails,
    # the launcher is never falsely marked as current.
    version_file = launcher_dir / "Launcher_Version.json"
    version_temp = version_file.with_name(version_file.name + ".new")
    version_temp.write_text(
        json.dumps(
            data["version_info"],
            indent=4,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(version_temp, version_file)

    try:
        pending_path.unlink()
    except OSError:
        pass

    try:
        shutil.rmtree(staging_dir)
    except OSError:
        pass

    restart_launcher(launch_path, launcher_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", required=True)
    parser.add_argument("--wait-pid", required=True, type=int)
    args = parser.parse_args()

    try:
        apply_update(args.apply, args.wait_pid)
    except Exception as error:
        # Keep a persistent failure log because the main GUI is already closed.
        try:
            pending = Path(args.apply)
            log_dir = pending.parent
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "self_update_error.log").write_text(
                f"{type(error).__name__}: {error}\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
