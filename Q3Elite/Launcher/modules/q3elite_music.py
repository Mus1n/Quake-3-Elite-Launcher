#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Q3Elite Music component — Step 18

Remote:
  Quake 3 Arena/z-Music-Playlist-by-Mus1n.pk3dir/music/*

Local:
  GAME_ROOT/baseq3/mods/osp/z-Music-Playlist-by-Mus1n.pk3dir/music/*

1.ogg ... 5.ogg are BASE tracks and remain managed by Q3Elite/Manifest.json.
Everything else is the optional dynamic Music Addon.

No song count or byte size is hard-coded: both come from live pCloud metadata.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import os
import zipfile

import pcloud_q3elite as pcloud

GAME_ROOT = pcloud.ROOT
LOCAL_ROOT = (
    GAME_ROOT
    / "baseq3"
    / "mods"
    / "osp"
    / "z-Music-Playlist-by-Mus1n.pk3dir"
    / "music"
)
BASE_TRACKS = {f"{n}.ogg".casefold() for n in range(1, 6)}
APPDATA = Path(os.environ.get("APPDATA", Path.home()))
CACHE_DIR = APPDATA / "Quake 3 Elite" / "Launcher" / "cache"
MUSIC_ZIP = CACHE_DIR / "Q3Elite_Music.zip"


def human_size(value):
    n = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def catalog():
    files = pcloud.music_files()
    base, optional = [], []

    for item in files:
        # BASE classification is intentionally only for root music/1.ogg..5.ogg.
        rel = str(PurePosixPath(item["relative"]))
        if "/" not in rel and rel.casefold() in BASE_TRACKS:
            base.append(item)
        else:
            optional.append(item)

    return {
        "all": files,
        "base": base,
        "optional": optional,
        "all_size": sum(x["size"] for x in files),
        "base_size": sum(x["size"] for x in base),
        "optional_size": sum(x["size"] for x in optional),
    }


def describe():
    data = catalog()
    return {
        "base_tracks": len(data["base"]),
        "base_bytes": data["base_size"],
        "base_human": human_size(data["base_size"]),
        "optional_tracks": len(data["optional"]),
        "optional_bytes": data["optional_size"],
        "optional_human": human_size(data["optional_size"]),
        "total_tracks": len(data["all"]),
        "total_bytes": data["all_size"],
        "total_human": human_size(data["all_size"]),
    }


def _download_item(item, control=None, progress_callback=None):
    from download_tools import downloader

    rel = Path(*PurePosixPath(item["relative"]).parts)
    destination = LOCAL_ROOT / rel
    destination.parent.mkdir(parents=True, exist_ok=True)

    # For optional music there is no SHA-256 entry in Q3Elite Manifest.
    # pCloud's live byte size is therefore the install/resume integrity guard.
    if destination.is_file() and destination.stat().st_size == item["size"]:
        print(f"[current] {item['relative']}")
        return False

    info = pcloud.resolve_entry(item)
    print(f"[music] {item['relative']} ({human_size(item['size'])})")

    if destination.is_file():
        destination.unlink()

    result = downloader(
        info["url"],
        str(destination.parent),
        destination.name,
        skip=True,
        control=control,
        progress_callback=progress_callback,
        expected_size=item["size"],
        use_part_file=True,
        timeout_value=30,
    )
    if not result:
        raise RuntimeError(f"Music download failed/cancelled: {item['relative']}")

    final = Path(result)
    if not final.is_file() or final.stat().st_size != item["size"]:
        raise RuntimeError(f"Music size verification failed: {item['relative']}")

    print(f"[installed] {item['relative']}")
    return True


def _member_relative(name):
    """Map ZIP member to path relative to remote ...pk3dir/music folder."""
    parts = PurePosixPath(str(name).replace("\\", "/").lstrip("/")).parts
    if not parts:
        return None
    # folderid=.../music normally gives tracks at ZIP root.
    # Also accept archives containing a music/ prefix or full parent structure.
    for i, part in enumerate(parts):
        if part.casefold() == "music":
            rest = parts[i + 1:]
            if rest:
                return str(PurePosixPath(*rest))
    return str(PurePosixPath(*parts))


def _extract_optional_zip(zip_path, items):
    wanted = {
        str(PurePosixPath(x["relative"])).casefold(): x
        for x in items
    }
    extracted = set()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _member_relative(info.filename)
            if not rel:
                continue
            item = wanted.get(rel.casefold())
            if item is None:
                continue  # skips base 1.ogg..5.ogg and unmanaged content

            dest = LOCAL_ROOT / Path(*PurePosixPath(rel).parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".music.tmp")
            written = 0
            try:
                with zf.open(info, "r") as srcf, tmp.open("wb") as dstf:
                    while True:
                        block = srcf.read(4 * 1024 * 1024)
                        if not block:
                            break
                        dstf.write(block)
                        written += len(block)
                if written != int(item["size"]):
                    raise RuntimeError(f"Music ZIP size verification failed: {rel}")
                os.replace(tmp, dest)
                extracted.add(rel.casefold())
            finally:
                tmp.unlink(missing_ok=True)
    print(f"[music bulk] Extracted and verified: {len(extracted)} / {len(items)}")
    return extracted


def install_optional(control=None, progress_callback=None):
    from download_tools import downloader

    data = catalog()
    items = data["optional"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print(" Q3Elite optional Music Addon")
    print("=" * 72)
    print(f"Tracks: {len(items)}")
    print(f"Size:   {human_size(data['optional_size'])}")

    stale_part = MUSIC_ZIP.with_name(MUSIC_ZIP.name + ".part")
    if stale_part.exists():
        print(
            f"[music bulk] Previous dynamic ZIP partial found "
            f"({human_size(stale_part.stat().st_size)}). Restarting from 0."
        )
        stale_part.unlink()

    reuse = MUSIC_ZIP.is_file() and zipfile.is_zipfile(MUSIC_ZIP)
    if reuse:
        print(f"[music bulk] Complete cached Music ZIP found ({human_size(MUSIC_ZIP.stat().st_size)}). Reusing it.")
    elif MUSIC_ZIP.exists():
        print("[music bulk] Cached Music ZIP is invalid; removing it.")
        MUSIC_ZIP.unlink()

    if not reuse:
        print("\nDownloading Music Playlist as one pCloud ZIP...")
        url = pcloud.pubzip_url(f"{pcloud.MUSIC}/music", MUSIC_ZIP.name)
        result = downloader(
            url, str(CACHE_DIR), MUSIC_ZIP.name, skip=True,
            control=control, progress_callback=progress_callback,
            expected_size=None, use_part_file=True, timeout_value=60,
        )
        if not result:
            raise RuntimeError("Music bulk ZIP download failed/cancelled.")

    if not zipfile.is_zipfile(MUSIC_ZIP):
        raise RuntimeError("pCloud getpubzip did not return a valid Music ZIP.")

    _extract_optional_zip(MUSIC_ZIP, items)

    # Repair/check pass. Usually every track is already current.
    downloaded = 0
    print("\n[music bulk] Running verification/repair pass...")
    for index, item in enumerate(items, 1):
        print(f"\n--- [{index}/{len(items)}] {item['relative']} ---")
        if _download_item(item, control, progress_callback):
            downloaded += 1

    print(f"\n[music cache] Kept: {MUSIC_ZIP}")
    print(f"[OK] Music Addon ready. Individual downloads/repairs: {downloaded}")
    return {
        "tracks": len(items), "bytes": data["optional_size"],
        "downloaded": downloaded,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("info", "install"),
        nargs="?",
        default="info",
    )
    args = parser.parse_args()

    if args.command == "info":
        info = describe()
        print("=" * 72)
        print(" Q3Elite Music catalog")
        print("=" * 72)
        print(f"Base music:     {info['base_tracks']} tracks / {info['base_human']}")
        print(f"Optional addon: {info['optional_tracks']} tracks / {info['optional_human']}")
        print(f"Remote total:   {info['total_tracks']} tracks / {info['total_human']}")
    else:
        from download_tools import DownloadControl
        install_optional(control=DownloadControl())


if __name__ == "__main__":
    main()
