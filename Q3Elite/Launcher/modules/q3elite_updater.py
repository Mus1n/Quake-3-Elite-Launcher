#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Q3Elite updater — Step 16

Fast normal launch:
  remote Version.json == local Version.json
    -> do NOT download remote Manifest.json
    -> use local Manifest.json
    -> existence check only
    -> repair only missing managed files

Version update:
  remote Version.json != local Version.json
    -> download remote Manifest.json metadata
    -> diff local manifest vs remote manifest
    -> unchanged hashes: skip
    -> new/changed hashes: hash only those candidate local files
    -> download only files not already equal to the new hash
    -> verify every downloaded file
    -> apply explicit remote "deleted"
    -> save Manifest.json
    -> save Version.json LAST

Full Verify / Repair:
    -> SHA-256 all applicable managed files
    -> repair missing/corrupt files

Profiles:
  full -> core + external baseq3/maps/*
  core -> ignore external baseq3/maps/*
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

import pcloud_q3elite as pcloud

GAME_ROOT = pcloud.ROOT
Q3ELITE_DIR = GAME_ROOT / "Q3Elite"
LOCAL_MANIFEST = Q3ELITE_DIR / "Manifest.json"
LOCAL_VERSION = Q3ELITE_DIR / "Version.json"

REMOTE_MANIFEST = "Q3Elite/Manifest.json"
REMOTE_VERSION = "Q3Elite/Version.json"

FULL = "full"
CORE = "core"

# Installed once from the manifest, then owned by the user.
USER_CONFIG = "baseq3/mods/osp/UserConfig.cfg"

# These paths must never be removed by Manifest.json "deleted" entries.
PRESERVED_FILES = {
    "q3elite/.q3eliteignore",
    "q3elite/q3eliteignore",  # defensive: pCloud non-ZIP mode may strip the dot
    USER_CONFIG.casefold(),
}
PRESERVED_PREFIXES = (
    "q3elite/update/",
)


def is_user_config(path):
    return norm(path).casefold() == USER_CONFIG.casefold()


def is_preserved_from_delete(path):
    p = norm(path).casefold()
    return p in PRESERVED_FILES or any(p.startswith(prefix) for prefix in PRESERVED_PREFIXES)


def norm(value):
    return str(PurePosixPath(str(value).replace("\\", "/").lstrip("/")))


def is_external_map(path):
    return norm(path).casefold().startswith("baseq3/maps/")


def is_official_q3_pak(path):
    p = norm(path).casefold()
    return (
        p.startswith("baseq3/pak")
        and p.endswith(".pk3")
        and len(p) == len("baseq3/pak0.pk3")
        and p[len("baseq3/pak")] in "012345678"
    )


def _component_preferences():
    """Read Step 18B component policy. Its defaults are Maps/Music/Autoexec OFF."""
    try:
        import q3elite_components
        return q3elite_components.load_state()
    except Exception:
        return None


def applies(path, profile):
    p = norm(path)

    # Official pak0.pk3-pak8.pk3 are always handled by pak_verifier.py.
    if is_official_q3_pak(p):
        return False

    prefs = _component_preferences()

    # Once Step 18B component state exists, it is authoritative.
    if prefs is not None:
        # The 26 QLmaps single-player PK3s live under baseq3/maps/, but they are
        # required Basic content and must be updated even when External Maps is OFF.
        try:
            import q3elite_components
            basic_map = q3elite_components.is_basic_map(p)
        except Exception:
            basic_map = False
        if is_external_map(p) and not basic_map and not prefs.get("external_maps", False):
            return False
        if p.casefold() == "baseq3/mods/osp/autoexec.cfg".casefold() and not prefs.get("autoexec_update", False):
            return False
        return True

    # Backwards compatibility for installations made before Step 18B.
    return not (profile == CORE and is_external_map(p))


def sha256_file(path, chunk_size=4 * 1024 * 1024):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def version_value(data):
    if isinstance(data, dict):
        value = data.get("version")
    else:
        value = data
    if value is None:
        raise RuntimeError("Version metadata has no 'version' value.")
    return str(value).strip()


def validate_manifest(data):
    if not isinstance(data, dict):
        raise RuntimeError("Manifest root must be an object.")
    if not isinstance(data.get("files"), dict):
        raise RuntimeError("Manifest has no valid 'files' object.")
    if not isinstance(data.get("deleted", []), list):
        raise RuntimeError("Manifest 'deleted' must be a list.")

    clean = {}
    seen = set()
    for raw_path, raw_hash in data["files"].items():
        rel = norm(raw_path)
        key = rel.casefold()
        digest = str(raw_hash).strip().lower()
        if key in seen:
            raise RuntimeError(f"Duplicate manifest path (case-insensitive): {rel}")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RuntimeError(f"Invalid SHA-256 in manifest: {rel}")
        seen.add(key)
        clean[rel] = digest

    deleted = []
    deleted_seen = set()
    for raw in data.get("deleted", []):
        rel = norm(raw)
        key = rel.casefold()
        if key in seen:
            # A live file always wins over stale cumulative deleted metadata.
            continue
        if key not in deleted_seen:
            deleted.append(rel)
            deleted_seen.add(key)

    return {
        "format": data.get("format", 1),
        "files": clean,
        "deleted": deleted,
    }


def load_local_manifest():
    if not LOCAL_MANIFEST.is_file():
        raise FileNotFoundError(f"Missing local manifest: {LOCAL_MANIFEST}")
    return validate_manifest(load_json(LOCAL_MANIFEST))


def load_local_version():
    if not LOCAL_VERSION.is_file():
        return None, None
    data = load_json(LOCAL_VERSION)
    return version_value(data), data


def remote_path_for(rel):
    return pcloud.remote_for(rel, is_external_map(rel))


def resolve_remote(rel):
    remote = remote_path_for(rel)
    info = pcloud.resolve(remote)
    info["remote_path"] = remote
    return info


def fetch_remote_json(rel):
    """
    Metadata files are NOT payload entries in Manifest.json.
    Resolve them separately from pCloud and read them into memory.
    """
    info = resolve_remote(rel)
    import urllib.request

    req = urllib.request.Request(
        info["url"],
        headers={"User-Agent": pcloud.UA},
    )
    with urllib.request.urlopen(req, timeout=pcloud.TIMEOUT) as response:
        raw = response.read()

    if info.get("size") and len(raw) != int(info["size"]):
        raise RuntimeError(
            f"Metadata size mismatch for {rel}: "
            f"{len(raw)} != {info['size']}"
        )

    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Invalid remote JSON: {rel}") from exc


def managed_for_profile(manifest, profile):
    return {
        rel: digest
        for rel, digest in manifest["files"].items()
        if applies(rel, profile)
    }


def existence_scan(manifest, profile):
    missing = []
    managed = managed_for_profile(manifest, profile)
    for rel in managed:
        if not (GAME_ROOT / Path(rel)).is_file():
            missing.append(rel)
    return managed, missing


def full_hash_scan(manifest, profile):
    managed = managed_for_profile(manifest, profile)
    missing, changed, current = [], [], []

    for rel, wanted in managed.items():
        target = GAME_ROOT / Path(rel)
        if not target.is_file():
            missing.append(rel)
        elif is_user_config(rel):
            # Install the default config when missing, then preserve user edits.
            current.append(rel)
        elif sha256_file(target).lower() == wanted:
            current.append(rel)
        else:
            changed.append(rel)

    return managed, missing, changed, current


def manifest_diff(local_manifest, remote_manifest, profile):
    old = {
        rel.casefold(): (rel, digest)
        for rel, digest in managed_for_profile(local_manifest, profile).items()
    }
    new = managed_for_profile(remote_manifest, profile)

    unchanged = []
    candidates = []

    for rel, new_hash in new.items():
        old_entry = old.get(rel.casefold())
        if old_entry and old_entry[1].lower() == new_hash.lower():
            unchanged.append(rel)
        else:
            candidates.append(rel)

    return new, unchanged, candidates


def download_one(rel, wanted_hash, control=None, progress_callback=None):
    from download_tools import downloader

    destination = GAME_ROOT / Path(rel)

    # UserConfig.cfg is downloaded only when missing. Existing user edits win.
    if is_user_config(rel) and destination.is_file():
        print(f"[preserved user config] {rel}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    info = resolve_remote(rel)

    print(f"[pCloud] {info['remote_path']}")
    print(f"[fileid] {info['fileid']}")
    print(f"[size]   {info['size']} bytes")

    # This final path has already been proven absent/wrong for the desired
    # release. Preserve any .part file so downloader can Range-resume.
    if destination.is_file():
        destination.unlink()

    result = downloader(
        info["url"],
        str(destination.parent),
        destination.name,
        skip=True,
        control=control,
        progress_callback=progress_callback,
        expected_size=info["size"],
        use_part_file=True,
        timeout_value=30,
    )
    if not result:
        raise RuntimeError(f"Download failed/cancelled: {rel}")

    result = Path(result)

    if is_user_config(rel):
        # Install the default once, but never treat it as immutable payload.
        # After this point the user may edit it freely.
        print(f"[installed user config] {rel}")
        return

    actual = sha256_file(result)
    if actual.lower() != wanted_hash.lower():
        bad = result.with_name(result.name + ".bad")
        try:
            os.replace(result, bad)
        except OSError:
            result.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA-256 mismatch for {rel}\n"
            f"Expected: {wanted_hash}\n"
            f"Actual:   {actual}"
        )

    print(f"[verified] {rel}")


def repair_list(paths, managed, control=None, progress_callback=None):
    for index, rel in enumerate(paths, 1):
        if control and control.cancelled:
            raise RuntimeError("Q3Elite update cancelled.")
        print(f"\n--- [{index}/{len(paths)}] {rel} ---")
        download_one(
            rel,
            managed[rel],
            control=control,
            progress_callback=progress_callback,
        )


def apply_deleted(remote_manifest, profile):
    removed = []
    for rel in remote_manifest.get("deleted", []):
        rel = norm(rel)
        if not applies(rel, profile):
            continue
        if is_preserved_from_delete(rel):
            print(f"[preserved] {rel}")
            continue
        target = GAME_ROOT / Path(rel)
        # Never recursively delete arbitrary directories.
        if target.is_file() or target.is_symlink():
            target.unlink()
            removed.append(rel)
            print(f"[deleted] {rel}")
    return removed


def fast_current(local_manifest, profile, control=None, progress_callback=None):
    managed, missing = existence_scan(local_manifest, profile)

    print(f"Managed: {len(managed)}")
    print(f"Missing: {len(missing)}")

    if missing:
        print(f"Repair needed: {len(missing)} missing file(s).")
        repair_list(
            missing,
            managed,
            control=control,
            progress_callback=progress_callback,
        )
    else:
        print("[OK] Fast existence check passed.")

    return {
        "status": "current",
        "missing_repaired": missing,
    }


def update_release(local_manifest, remote_manifest, remote_version_data,
                   local_version, remote_version, profile,
                   control=None, progress_callback=None):
    managed, unchanged, candidates = manifest_diff(
        local_manifest, remote_manifest, profile
    )

    print(f"Unchanged by manifest: {len(unchanged)}")
    print(f"New/changed candidates: {len(candidates)}")

    # Hash only candidate files. If the user already has the exact new file,
    # avoid downloading it.
    needed = []
    already_new = []
    for rel in candidates:
        target = GAME_ROOT / Path(rel)
        wanted = managed[rel]
        if is_user_config(rel) and target.is_file():
            already_new.append(rel)
        elif target.is_file() and sha256_file(target).lower() == wanted:
            already_new.append(rel)
        else:
            needed.append(rel)

    print(f"Already matches new release: {len(already_new)}")
    print(f"Download needed: {len(needed)}")

    repair_list(
        needed,
        managed,
        control=control,
        progress_callback=progress_callback,
    )

    removed = apply_deleted(remote_manifest, profile)

    # Verify only files touched/accepted as new candidates.
    for rel in candidates:
        target = GAME_ROOT / Path(rel)
        if not target.is_file():
            raise RuntimeError(f"Final verification: missing {rel}")
        if is_user_config(rel):
            continue
        if sha256_file(target).lower() != managed[rel]:
            raise RuntimeError(f"Final verification failed: {rel}")

    # Commit metadata only after the payload update has succeeded.
    # Manifest first, Version LAST.
    atomic_write_json(LOCAL_MANIFEST, remote_manifest)
    atomic_write_json(LOCAL_VERSION, remote_version_data)

    print()
    print(f"[OK] Q3Elite updated: {local_version} -> {remote_version}")
    print(f"Downloaded: {len(needed)}")
    print(f"Deleted:    {len(removed)}")

    return {
        "status": "updated",
        "from_version": local_version,
        "to_version": remote_version,
        "downloaded": needed,
        "deleted": removed,
    }


def recover_from_remote_manifest(remote_manifest, remote_version_data,
                                 remote_version, profile,
                                 control=None, progress_callback=None):
    """
    Recover missing/corrupt local updater metadata using only the current
    remote release.

    This is intentionally a full SHA scan, not a full re-download:
      - matching files are kept
      - missing/corrupt/outdated files are downloaded
      - Manifest.json is restored
      - Version.json is committed LAST
    """
    print()
    print("Local Manifest.json is missing or invalid.")
    print("Recovering against the current remote release...")
    print("This requires a one-time full SHA-256 scan, but only bad/missing")
    print("files will be downloaded.")

    managed, missing, changed, current = full_hash_scan(
        remote_manifest, profile
    )
    needed = missing + changed

    print()
    print(f"Managed:         {len(managed)}")
    print(f"Current:         {len(current)}")
    print(f"Missing:         {len(missing)}")
    print(f"Changed:         {len(changed)}")
    print(f"Download needed: {len(needed)}")

    repair_list(
        needed,
        managed,
        control=control,
        progress_callback=progress_callback,
    )

    removed = apply_deleted(remote_manifest, profile)

    # Final verification only for files repaired in this recovery. All other
    # managed files were already SHA-verified by full_hash_scan().
    for rel in needed:
        target = GAME_ROOT / Path(rel)
        if not target.is_file():
            raise RuntimeError(f"Recovery verification: missing {rel}")
        if is_user_config(rel):
            # UserConfig is install-once. Presence is enough; from now on it is
            # user-owned and must never be SHA-verified or overwritten.
            continue
        if sha256_file(target).lower() != managed[rel]:
            raise RuntimeError(f"Recovery verification failed: {rel}")

    # Metadata becomes authoritative only after payload recovery succeeded.
    atomic_write_json(LOCAL_MANIFEST, remote_manifest)
    atomic_write_json(LOCAL_VERSION, remote_version_data)

    print()
    print("[OK] Q3Elite metadata/install recovery complete.")
    print(f"Recovered version: {remote_version}")
    print(f"Downloaded:        {len(needed)}")
    print(f"Deleted:           {len(removed)}")

    return {
        "status": "recovered",
        "to_version": remote_version,
        "downloaded": needed,
        "deleted": removed,
    }


def check_and_update(profile=FULL, control=None, progress_callback=None):
    print("=" * 72)
    print(" Q3Elite updater — Step 18.1")
    print("=" * 72)
    print(f"GAME_ROOT: {GAME_ROOT}")
    print(f"Profile:   {profile}")
    print(f"pCloud:    {pcloud.PCLOUD_DCONF}")

    # Version metadata is useful but recoverable. A missing/corrupt local
    # Version.json must not prevent recovery from the current remote release.
    try:
        local_version, local_version_data = load_local_version()
    except Exception as error:
        print(f"[warning] Local Version.json is invalid: {error}")
        local_version, local_version_data = None, None

    # Local manifest is also recoverable. Do not abort startup merely because
    # it was accidentally deleted or corrupted.
    local_manifest = None
    local_manifest_error = None
    try:
        local_manifest = load_local_manifest()
    except Exception as error:
        local_manifest_error = error

    print("\nChecking remote Version.json...")
    remote_version_data = fetch_remote_json(REMOTE_VERSION)
    remote_version = version_value(remote_version_data)

    print(f"Local version:  {local_version or '<missing/invalid>'}")
    print(f"Remote version: {remote_version}")

    # FAST PATH: valid local manifest + same version.
    if local_manifest is not None and local_version == remote_version:
        print("\nVersion is current.")
        print("Remote Manifest.json will NOT be downloaded.")
        return fast_current(
            local_manifest,
            profile,
            control=control,
            progress_callback=progress_callback,
        )

    # RECOVERY PATH: without a trustworthy local manifest there is nothing
    # safe to diff. Fetch only the current remote manifest and verify the
    # existing installation against it.
    if local_manifest is None:
        print()
        print(f"[recovery] {local_manifest_error}")
        print("Downloading current remote Manifest.json...")
        remote_manifest = validate_manifest(fetch_remote_json(REMOTE_MANIFEST))

        return recover_from_remote_manifest(
            remote_manifest,
            remote_version_data,
            remote_version,
            profile,
            control=control,
            progress_callback=progress_callback,
        )

    # NORMAL UPDATE PATH: old local manifest exists, so retain the optimized
    # manifest-to-manifest diff introduced in Step 16.
    print("\nNew/different release detected.")
    print("Downloading remote Manifest.json...")
    remote_manifest = validate_manifest(fetch_remote_json(REMOTE_MANIFEST))

    return update_release(
        local_manifest,
        remote_manifest,
        remote_version_data,
        local_version,
        remote_version,
        profile,
        control=control,
        progress_callback=progress_callback,
    )


def verify_and_repair(profile=FULL, control=None, progress_callback=None):
    """
    Full Verify / Repair.

    Step 18.2: --verify is self-healing too. If local Manifest.json is
    missing/corrupt, fetch the current remote Version + Manifest and recover
    against those instead of aborting.
    """
    try:
        manifest = load_local_manifest()
    except Exception as error:
        print("=" * 72)
        print(" Q3Elite full Verify / Repair")
        print("=" * 72)
        print(f"[recovery] {error}")
        print("Local manifest cannot be used.")
        print("Downloading current remote Version.json + Manifest.json...")

        remote_version_data = fetch_remote_json(REMOTE_VERSION)
        remote_version = version_value(remote_version_data)
        remote_manifest = validate_manifest(fetch_remote_json(REMOTE_MANIFEST))

        return recover_from_remote_manifest(
            remote_manifest,
            remote_version_data,
            remote_version,
            profile,
            control=control,
            progress_callback=progress_callback,
        )

    managed, missing, changed, current = full_hash_scan(manifest, profile)
    needed = missing + changed

    print("=" * 72)
    print(" Q3Elite full Verify / Repair")
    print("=" * 72)
    print(f"Managed: {len(managed)}")
    print(f"Current: {len(current)}")
    print(f"Missing: {len(missing)}")
    print(f"Changed: {len(changed)}")

    repair_list(
        needed,
        managed,
        control=control,
        progress_callback=progress_callback,
    )

    print(f"\n[OK] Verify / Repair complete. Repaired: {len(needed)}")
    return {
        "status": "verified",
        "repaired": needed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=(FULL, CORE),
        default=FULL,
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="SHA-256 all managed files and repair corruption.",
    )
    args = parser.parse_args()

    from download_tools import DownloadControl
    control = DownloadControl()

    if args.verify:
        verify_and_repair(args.profile, control=control)
    else:
        check_and_update(args.profile, control=control)


if __name__ == "__main__":
    main()
