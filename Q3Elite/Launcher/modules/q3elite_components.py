#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Q3Elite component manager — Step 18B

Model:
  Basic              - always installed
  External Maps      - optional
  Music Playlist     - optional (tracks outside base 1..5)
  Autoexec Update    - optional policy: when OFF, updater must preserve autoexec.cfg

State:
  %APPDATA%\Quake 3 Elite\Launcher\components.json

Fresh Basic installation is manifest-driven and SHA-256 verified.
This first 18B backend intentionally favors correctness; the GUI can call the same
functions later. Components can be added/removed after installation.
"""

from __future__ import annotations

import argparse, hashlib, json, os, shutil, zipfile
from pathlib import Path, PurePosixPath

import pcloud_q3elite as pcloud
import q3elite_music as music
from download_tools import DownloadControl, downloader

ROOT = pcloud.ROOT
APPDATA = Path(os.environ.get("APPDATA", Path.home()))
STATE_FILE = APPDATA / "Quake 3 Elite" / "Launcher" / "components.json"
TEMP_DIR = APPDATA / "Quake 3 Elite" / "Temp"
BASIC_ZIP = TEMP_DIR / "Q3Elite_Basic.zip"

REMOTE_MANIFEST = "Q3Elite/Manifest.json"
REMOTE_VERSION = "Q3Elite/Version.json"
LOCAL_MANIFEST = ROOT / "Q3Elite" / "Manifest.json"
LOCAL_VERSION = ROOT / "Q3Elite" / "Version.json"

AUTOEXEC = "baseq3/mods/OSP/autoexec.cfg"
MAP_PREFIX = "baseq3/maps/"
MUSIC_LOCAL_PREFIX = "baseq3/mods/osp/z-Music-Playlist-by-Mus1n.pk3dir/music/"
BASE_MUSIC = {f"{MUSIC_LOCAL_PREFIX}{n}.ogg".casefold() for n in range(1, 6)}


def norm(v):
    return str(PurePosixPath(str(v).replace("\\", "/").lstrip("/")))


def sha256_file(path, chunk=4 * 1024 * 1024):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_state():
    return {
        "format": 1,
        "basic": False,
        "external_maps": False,
        "music_playlist": False,
        "autoexec_update": False,
        "music_hashes": {},
    }


def load_state():
    if not STATE_FILE.is_file():
        return default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        base = default_state()
        base.update(data if isinstance(data, dict) else {})
        if not isinstance(base.get("music_hashes"), dict):
            base["music_hashes"] = {}
        return base
    except Exception:
        return default_state()


def save_state(state):
    atomic_json(STATE_FILE, state)


def fetch_json(remote):
    info = pcloud.resolve(pcloud.remote_for(remote))
    import urllib.request
    req = urllib.request.Request(info["url"], headers={"User-Agent": "Q3Elite-18B"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def remote_release():
    version = fetch_json(REMOTE_VERSION)
    manifest = fetch_json(REMOTE_MANIFEST)
    files = {norm(k): str(v).lower() for k, v in manifest.get("files", {}).items()}
    return version, manifest, files


def classify(rel):
    p = norm(rel)
    cf = p.casefold()
    if cf.startswith(MAP_PREFIX):
        return "maps"
    if cf == AUTOEXEC.casefold():
        return "autoexec"
    if cf in BASE_MUSIC:
        return "basic"
    return "basic"


def selected_basic_files(files, autoexec_update=False):
    # autoexec.cfg is still installed on first Basic install; the checkbox controls
    # whether later Q3Elite updates are allowed to replace it.
    return {p:h for p,h in files.items() if classify(p) != "maps"}


def selected_map_files(files):
    return {p:h for p,h in files.items() if classify(p) == "maps"}


def _size_index(remote_folder):
    """Build relative-path -> size from the cached pCloud tree; no download-URL calls."""
    return {
        norm(item["relative"]).casefold(): int(item.get("size", 0))
        for item in pcloud.list_folder(remote_folder, recursive=True)
    }


def catalog():
    version, manifest, files = remote_release()
    basic = selected_basic_files(files)
    maps = selected_map_files(files)

    # IMPORTANT: info/catalog must be cheap. The old 18B draft called resolve()
    # once per ~3000 files, which requested a temporary download URL for every file.
    # Here one cached showpublink tree is used and sizes are read directly from metadata.
    core_sizes = _size_index(pcloud.CORE)
    map_sizes = _size_index(pcloud.MAPS)

    def size_for(rel):
        remote = pcloud.remote_for(rel, is_map=classify(rel) == "maps")
        if classify(rel) == "maps":
            prefix = norm(pcloud.MAPS) + "/"
            key = norm(remote)[len(prefix):].casefold()
            return map_sizes.get(key, 0)

        # Base music 1..5 is hosted outside CORE.
        if norm(rel).casefold() in BASE_MUSIC:
            entry = pcloud.find(remote)
            return int(entry.get("size", 0))

        prefix = norm(pcloud.CORE) + "/"
        key = norm(remote)[len(prefix):].casefold()
        return core_sizes.get(key, 0)

    basic_bytes = sum(size_for(rel) for rel in basic)
    maps_bytes = sum(size_for(rel) for rel in maps)
    mi = music.describe()
    autoexec_key = next((rel for rel in files if rel.casefold() == AUTOEXEC.casefold()), None)
    autoexec_bytes = size_for(autoexec_key) if autoexec_key else 0
    return {
        "version": version,
        "manifest": manifest,
        "files": files,
        "basic_files": len(basic),
        "basic_bytes": basic_bytes,
        "maps_files": len(maps),
        "maps_bytes": maps_bytes,
        "music_tracks": mi["optional_tracks"],
        "music_bytes": mi["optional_bytes"],
        "autoexec_bytes": autoexec_bytes,
    }


def human(n):
    n=float(n)
    for u in ("B","KB","MB","GB","TB"):
        if n < 1024 or u=="TB":
            return f"{int(n)} B" if u=="B" else f"{n:.1f} {u}"
        n/=1024


def _download_managed(rel, digest, control=None, progress_callback=None):
    dest = ROOT / Path(rel)
    if dest.is_file() and sha256_file(dest).lower() == digest.lower():
        print(f"[current] {rel}")
        return False
    info = pcloud.resolve(pcloud.remote_for(rel, is_map=classify(rel)=="maps"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[download] {rel} ({human(info['size'])})")
    result = downloader(
        info["url"], str(dest.parent), dest.name,
        skip=True, control=control, progress_callback=progress_callback,
        expected_size=info["size"], use_part_file=True, timeout_value=30,
    )
    if not result:
        raise RuntimeError(f"Download failed/cancelled: {rel}")
    if sha256_file(dest).lower() != digest.lower():
        raise RuntimeError(f"SHA-256 verification failed: {rel}")
    print(f"[verified] {rel}")
    return True


def _install_group(group, control=None, progress_callback=None):
    downloaded=0
    total=len(group)
    for i,(rel,digest) in enumerate(group.items(),1):
        print(f"\n--- [{i}/{total}] {rel} ---")
        if _download_managed(rel,digest,control,progress_callback):
            downloaded+=1
    return downloaded


def _basic_core_group(files):
    """Basic files physically stored inside the remote CORE folder."""
    return {
        rel: digest for rel, digest in selected_basic_files(files).items()
        if rel.casefold() not in BASE_MUSIC
    }


def _basic_missing_ratio(group):
    if not group:
        return 0.0
    missing = 0
    for rel in group:
        if not (ROOT / Path(rel)).is_file():
            missing += 1
    return missing / len(group)


def _zip_member_to_managed(member_name, wanted_cf):
    """Map a pCloud ZIP member to a managed local path, ignoring archive prefix."""
    raw = norm(member_name)
    parts = PurePosixPath(raw).parts
    if not parts:
        return None

    # pCloud may package the selected folder itself and/or its parents.
    # Locate the first local managed root inside the archive path.
    for root_name in ("baseq3", "Q3Elite"):
        for i, part in enumerate(parts):
            if part.casefold() == root_name.casefold():
                candidate = norm(str(PurePosixPath(*parts[i:])))
                return wanted_cf.get(candidate.casefold())
    return None


def _extract_basic_zip(zip_path, group):
    """Selectively extract ONLY files owned by the Basic manifest."""
    wanted_cf = {norm(rel).casefold(): norm(rel) for rel in group}
    extracted = set()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _zip_member_to_managed(info.filename, wanted_cf)
            if not rel:
                continue

            dest = ROOT / Path(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".bulk.tmp")

            h = hashlib.sha256()
            try:
                with zf.open(info, "r") as srcf, tmp.open("wb") as dstf:
                    while True:
                        block = srcf.read(4 * 1024 * 1024)
                        if not block:
                            break
                        dstf.write(block)
                        h.update(block)

                expected = group[rel]
                if h.hexdigest().lower() != expected.lower():
                    raise RuntimeError(f"SHA-256 verification failed in Basic ZIP: {rel}")

                os.replace(tmp, dest)
                extracted.add(rel.casefold())
            finally:
                tmp.unlink(missing_ok=True)

    print(f"[bulk] Extracted and SHA-256 verified: {len(extracted)} / {len(group)}")
    return extracted


def _bulk_install_basic_core(files, control=None, progress_callback=None):
    """
    Fresh-install accelerator.

    Downloads the remote Quake 3 Elite folder as one streamed pCloud ZIP,
    but extracts ONLY Basic files present in Manifest.json. This prevents
    launcher/control/unmanaged files from being overwritten by the archive.
    """
    group = _basic_core_group(files)
    if not group:
        return 0

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    url = pcloud.pubzip_url(pcloud.CORE, "Q3Elite_Basic.zip")

    print("\nFresh Basic install detected.")
    print("Using pCloud bulk ZIP instead of thousands of individual requests...")
    print("The ZIP is streamed by pCloud; displayed transfer size may be unknown.")

    result = downloader(
        url,
        str(TEMP_DIR),
        BASIC_ZIP.name,
        skip=True,
        control=control,
        progress_callback=progress_callback,
        expected_size=None,
        use_part_file=True,
        timeout_value=60,
    )
    if not result:
        raise RuntimeError("Basic bulk ZIP download failed/cancelled.")

    try:
        extracted = _extract_basic_zip(Path(result), group)
    except zipfile.BadZipFile as exc:
        # Preserve a bad archive only as .bad for diagnosis; fallback below can
        # still repair/download individual files.
        bad = BASIC_ZIP.with_suffix(".zip.bad")
        bad.unlink(missing_ok=True)
        try:
            os.replace(BASIC_ZIP, bad)
        except OSError:
            pass
        print(f"[bulk] Invalid ZIP: {exc}")
        return 0
    else:
        BASIC_ZIP.unlink(missing_ok=True)
        return len(extracted)


def install_basic(external_maps=False, music_playlist=False, autoexec_update=False,
                  control=None, progress_callback=None):
    control = control or DownloadControl()
    version, manifest, files = remote_release()
    basic = selected_basic_files(files, autoexec_update)

    print("="*72)
    print(" Q3Elite Basic installation — Step 18B")
    print("="*72)
    print(f"Basic files: {len(basic)}")
    print(f"External Maps: {'YES' if external_maps else 'NO'}")
    print(f"Music Playlist: {'YES' if music_playlist else 'NO'}")
    print(f"Autoexec Update: {'YES' if autoexec_update else 'NO'}")

    # Bulk is used only when this is genuinely a fresh Basic install.
    # Existing/mostly-complete installations keep the efficient per-file
    # SHA repair path.
    core_group = _basic_core_group(files)
    missing_ratio = _basic_missing_ratio(core_group)
    bulk_extracted = 0

    if missing_ratio >= 0.80:
        try:
            bulk_extracted = _bulk_install_basic_core(
                files, control, progress_callback
            )
        except Exception as exc:
            print(f"[bulk] Bulk install unavailable: {exc}")
            print("[bulk] Falling back to individual SHA-256 repair...")

    # Always finish with the normal manifest-driven path. It skips every
    # correctly extracted file and downloads only anything missing/corrupt,
    # including base music 1..5 which lives outside the CORE folder.
    downloaded = _install_group(basic, control, progress_callback)

    if external_maps:
        downloaded += _install_group(selected_map_files(files), control, progress_callback)

    # Commit release metadata only after required payload is valid.
    atomic_json(LOCAL_MANIFEST, manifest)
    atomic_json(LOCAL_VERSION, version)

    state=load_state()
    state["basic"]=True
    state["external_maps"]=bool(external_maps)
    state["autoexec_update"]=bool(autoexec_update)
    save_state(state)

    if music_playlist:
        install_music(control, progress_callback)

    print(f"\n[OK] Basic ready.")
    print(f"Bulk extracted/verified: {bulk_extracted}")
    print(f"Individual downloads:    {downloaded}")
    return load_state()


def install_maps(control=None, progress_callback=None):
    _, _, files = remote_release()
    count=_install_group(selected_map_files(files), control or DownloadControl(), progress_callback)
    state=load_state(); state["external_maps"]=True; save_state(state)
    print(f"\n[OK] External Maps installed. Downloaded: {count}")


def uninstall_maps():
    _, _, files = remote_release()
    group=selected_map_files(files)
    removed=modified=missing=0
    for rel,digest in group.items():
        path=ROOT/Path(rel)
        if not path.exists():
            missing+=1; continue
        # SHA protects user-modified/replaced maps from deletion.
        if path.is_file() and sha256_file(path).lower()==digest.lower():
            path.unlink(); removed+=1; print(f"[removed] {rel}")
        else:
            modified+=1; print(f"[preserved modified] {rel}")
    state=load_state(); state["external_maps"]=False; save_state(state)
    print(f"[OK] Maps removal: removed={removed}, preserved_modified={modified}, missing={missing}")


def install_music(control=None, progress_callback=None):
    result=music.install_optional(control or DownloadControl(), progress_callback)
    data=music.catalog()
    hashes={}
    for item in data["optional"]:
        rel=Path(*PurePosixPath(item["relative"]).parts)
        path=music.LOCAL_ROOT/rel
        if path.is_file():
            hashes[norm(str(PurePosixPath(item["relative"])))] = sha256_file(path)
    state=load_state()
    state["music_playlist"]=True
    state["music_hashes"]=hashes
    save_state(state)
    return result


def uninstall_music():
    state=load_state()
    known={norm(k).casefold():v.lower() for k,v in state.get("music_hashes",{}).items()}
    data=music.catalog()
    removed=modified=missing=0
    for item in data["optional"]:
        rel=norm(item["relative"])
        path=music.LOCAL_ROOT/Path(*PurePosixPath(rel).parts)
        if not path.exists():
            missing+=1; continue
        wanted=known.get(rel.casefold())
        # Delete only files whose SHA was recorded by our installer.
        if wanted and path.is_file() and sha256_file(path).lower()==wanted:
            path.unlink(); removed+=1; print(f"[removed] {rel}")
        else:
            modified+=1; print(f"[preserved unverified/modified] {rel}")
    state["music_playlist"]=False
    state["music_hashes"]={}
    save_state(state)
    print(f"[OK] Music removal: removed={removed}, preserved={modified}, missing={missing}")


def set_autoexec_update(enabled):
    state=load_state(); state["autoexec_update"]=bool(enabled); save_state(state)
    print(f"[OK] Autoexec Update: {'ON' if enabled else 'OFF'}")


def show_info():
    c=catalog(); s=load_state()
    total=c["basic_bytes"]
    print("="*72)
    print(" Q3Elite installation components — Step 18B")
    print("="*72)
    print(f"Quake 3 Elite Basic      {human(c['basic_bytes'])}  [required]")
    print(f"[ ] External Maps       +{human(c['maps_bytes'])}")
    print(f"[ ] Music Playlist      +{human(c['music_bytes'])} ({c['music_tracks']} tracks)")
    print(f"[ ] Autoexec Update      policy toggle ({human(c['autoexec_bytes'])} file)")
    print()
    print("Current component state:")
    print(json.dumps(s, indent=2))
    print(f"\nBasic-only download size (remote payload): {human(total)}")


def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    sub.add_parser("info")
    ins=sub.add_parser("install")
    ins.add_argument("--maps",action="store_true")
    ins.add_argument("--music",action="store_true")
    ins.add_argument("--autoexec-update",action="store_true")
    sub.add_parser("install-maps")
    sub.add_parser("remove-maps")
    sub.add_parser("install-music")
    sub.add_parser("remove-music")
    ae=sub.add_parser("autoexec-update")
    ae.add_argument("value",choices=("on","off"))
    a=ap.parse_args()
    ctl=DownloadControl()
    if a.cmd=="info": show_info()
    elif a.cmd=="install": install_basic(a.maps,a.music,a.autoexec_update,ctl)
    elif a.cmd=="install-maps": install_maps(ctl)
    elif a.cmd=="remove-maps": uninstall_maps()
    elif a.cmd=="install-music": install_music(ctl)
    elif a.cmd=="remove-music": uninstall_music()
    elif a.cmd=="autoexec-update": set_autoexec_update(a.value=="on")

if __name__=="__main__":
    main()
