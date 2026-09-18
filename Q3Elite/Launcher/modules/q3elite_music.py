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


def install_optional(control=None, progress_callback=None):
    data = catalog()
    items = data["optional"]

    print("=" * 72)
    print(" Q3Elite optional Music Addon")
    print("=" * 72)
    print(f"Tracks: {len(items)}")
    print(f"Size:   {human_size(data['optional_size'])}")

    downloaded = 0
    for index, item in enumerate(items, 1):
        print(f"\n--- [{index}/{len(items)}] ---")
        if _download_item(item, control, progress_callback):
            downloaded += 1

    print(f"\n[OK] Music Addon ready. Downloaded: {downloaded}")
    return {
        "tracks": len(items),
        "bytes": data["optional_size"],
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
