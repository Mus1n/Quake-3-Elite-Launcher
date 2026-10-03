import json
import urllib.request
from pathlib import Path

import download_tools as dt
from base_methods import BASEQ3_DIR, Q3ELITE_LAUNCHER_DATA_DIR


# ============================================================================
# OSP2-BE UPDATER
# ============================================================================
#
# Remote source of truth:
#   official scoqx/osp2-be version.txt
#
# Local source of truth:
#   %APPDATA%\Quake 3 Elite\Launcher\update_state.json
#
# Installed file:
#   <game root>\baseq3\mods\osp\zz-osp-pak8be.pk3
#
# The PK3 is downloaded from releases/download/latest.
# The state file is updated ONLY after a successful download/install.
# ============================================================================

GITHUB_REPO = "scoqx/osp2-be"

# OSP2-BE deliberately uses a release named "latest", so the release tag
# itself is NOT the real mod version. The real version is maintained in
# version.txt and currently has values such as "1.08a".
OSP_VERSION_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_REPO}/"
    "refs/heads/main/version.txt"
)

OSP_DOWNLOAD_URL = (
    f"https://github.com/{GITHUB_REPO}/releases/download/latest/"
    "zz-osp-pak8be.pk3"
)

OSP_ASSET_NAME = "zz-osp-pak8be.pk3"
OSP_FILE = BASEQ3_DIR / "mods" / "osp" / OSP_ASSET_NAME

UPDATE_STATE_FILE = (
    Q3ELITE_LAUNCHER_DATA_DIR / "update_state.json"
)
STATE_KEY = "osp2-be"


def _download_text(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Q3Elite-Launcher",
            "Accept": "text/plain",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=timeout,
    ) as response:
        return response.read().decode(
            "utf-8",
            errors="replace",
        ).strip()


# ============================================================================
# REMOTE VERSION
# ============================================================================

def get_remote_info():
    """
    Read the real OSP2-BE version from the author's version.txt.

    The GitHub release is intentionally called "latest", therefore its
    release tag must not be used as the installed version.
    """
    version_raw = _download_text(
        OSP_VERSION_URL
    )
    version = _normalize_version(version_raw)

    if not version:
        raise RuntimeError(
            "OSP2-BE version.txt is empty."
        )

    # Defensive validation: version.txt should contain a single short value,
    # e.g. "1.08a", not HTML or another accidental response.
    if (
        "\n" in version
        or "\r" in version
        or len(version) > 64
    ):
        raise RuntimeError(
            f"Invalid OSP2-BE version.txt response: {version!r}"
        )

    return {
        "version": version,
        "download_url": OSP_DOWNLOAD_URL,
    }


def _normalize_version(version):
    """
    Store/compare the human version only.

    OSP2-BE version.txt currently uses values such as "1.08a".
    update_state.json uses "1.08a".
    """
    value = str(version or "").strip()

    if value.lower().startswith("be-"):
        value = value[3:].strip()

    return value


# ============================================================================
# LOCAL STATE
# ============================================================================

def _read_update_state():
    """
    Read update_state.json without modifying it.

    Missing, empty or invalid JSON is treated as an empty state.
    """
    if not UPDATE_STATE_FILE.is_file():
        return {}

    try:
        text = UPDATE_STATE_FILE.read_text(
            encoding="utf-8"
        ).strip()

        if not text:
            return {}

        data = json.loads(text)

        if isinstance(data, dict):
            return data

    except (OSError, json.JSONDecodeError) as error:
        print(
            f"[warning] Could not read update_state.json: "
            f"{error}"
        )

    return {}


def get_local_version():
    """
    Read the version previously installed successfully by this launcher.

    Expected format:

        {
            "osp2-be": {
                "version": "1.08a"
            }
        }
    """
    state = _read_update_state()

    osp_state = state.get(STATE_KEY)

    if not isinstance(osp_state, dict):
        return None

    version = osp_state.get("version")

    if version is None:
        return None

    version = _normalize_version(version)

    return version or None


def _save_local_version(version):
    """
    Save ONLY after OSP2-BE was successfully installed.

    Existing state for future components is preserved.
    """
    version = _normalize_version(version)

    if not version:
        raise ValueError(
            "Refusing to save an empty OSP2-BE version."
        )

    UPDATE_STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    state = _read_update_state()

    osp_state = state.get(STATE_KEY)

    if not isinstance(osp_state, dict):
        osp_state = {}

    osp_state["version"] = version
    state[STATE_KEY] = osp_state

    temp_file = UPDATE_STATE_FILE.with_name(
        UPDATE_STATE_FILE.name + ".tmp"
    )

    with temp_file.open(
        "w",
        encoding="utf-8",
    ) as state_file:
        json.dump(
            state,
            state_file,
            indent=4,
            ensure_ascii=False,
        )
        state_file.write("\n")

    temp_file.replace(
        UPDATE_STATE_FILE
    )

    print(
        f"Saved OSP2-BE state: {version}"
    )


# ============================================================================
# INSTALL / UPDATE
# ============================================================================

def install_or_update(remote_info, repair=False):
    """
    Download a NEW OSP2-BE file beside the currently installed PK3 first.

    This intentionally does not download directly over OSP_FILE.  The old
    working PK3 remains untouched until the new download has completed.
    Afterwards os.replace() swaps it atomically.

    This also avoids any "existing final file is already complete -> skip"
    behaviour in the generic downloader.
    """
    remote_version = _normalize_version(
        remote_info["version"]
    )
    download_url = str(
        remote_info["download_url"]
    ).strip()

    if not remote_version:
        raise RuntimeError(
            "OSP2-BE remote version is empty."
        )

    if not download_url:
        raise RuntimeError(
            "OSP2-BE download URL is empty."
        )

    osp_dir = OSP_FILE.parent
    osp_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    update_name = OSP_FILE.name + ".update"
    update_file = osp_dir / update_name
    update_part = Path(str(update_file) + ".part")

    # A version mismatch means a fresh release is required. Do not let an old
    # completed .update or stale .part make the generic downloader skip it.
    for stale in (update_file, update_part):
        if stale.exists():
            try:
                stale.unlink()
            except OSError as error:
                raise RuntimeError(
                    f"Could not remove stale OSP2-BE update file: {stale}"
                ) from error

    print(
        f"Downloading OSP2-BE {remote_version}..."
    )
    print(
        f"Temporary destination: {update_file}"
    )

    result = dt.downloader(
        download_url,
        str(osp_dir),
        update_name,
        skip=False,
        use_part_file=True,
    )

    if not result:
        raise RuntimeError(
            "OSP2-BE downloader reported failure."
        )

    if not update_file.is_file():
        raise RuntimeError(
            "OSP2-BE download finished, but the new PK3 is missing."
        )

    if update_file.stat().st_size <= 0:
        raise RuntimeError(
            "Downloaded OSP2-BE PK3 is empty."
        )

    print(
        f"Replacing installed OSP2-BE: {OSP_FILE}"
    )

    # Atomic on the same filesystem. Existing working OSP remains available
    # until this exact point.
    update_file.replace(OSP_FILE)

    if not OSP_FILE.is_file() or OSP_FILE.stat().st_size <= 0:
        raise RuntimeError(
            "OSP2-BE replacement failed."
        )

    # State changes only AFTER the new PK3 became the installed file.
    _save_local_version(
        remote_version
    )

    print(
        f"OSP2-BE {remote_version} installed successfully."
    )

    return True


# ============================================================================
# CHECK
# ============================================================================

def check_and_update():
    """
    Check OSP2-BE against the author's official version.txt.

    Rules:
      - missing PK3 -> download regardless of saved version
      - missing local state -> refresh/download current release
      - local != remote -> update
      - local == remote and PK3 exists -> do nothing

    Network exceptions intentionally propagate to launch.pyw, which already
    decides whether the launcher should continue in offline mode.
    """
    installed = OSP_FILE.is_file()

    # Read this BEFORE any remote request or write.
    local_version = get_local_version()

    print()
    print("========================================")
    print(" Checking OSP2-BE")
    print("========================================")
    print()
    print(
        f"OSP2-BE file: "
        f"{'installed' if installed else 'missing'}"
    )
    print(f"State file: {UPDATE_STATE_FILE}")
    print(
        f"Saved local version: "
        f"{local_version or 'unknown'}"
    )

    remote_info = get_remote_info()
    remote_version = _normalize_version(
        remote_info["version"]
    )

    print(
        f"Remote OSP2-BE version: {remote_version}"
    )

    if not installed:
        print(
            "OSP2-BE file is missing. "
            "Downloading current release..."
        )

        install_or_update(
            remote_info,
            repair=True,
        )

        action = "repaired"

    elif not local_version:
        print(
            "OSP2-BE file exists, but no installed version "
            "is recorded. Refreshing current release..."
        )

        install_or_update(
            remote_info,
            repair=False,
        )

        action = "refreshed"

    elif local_version != remote_version:
        print(
            f"OSP2-BE update required: "
            f"{local_version} -> {remote_version}"
        )

        install_or_update(
            remote_info,
            repair=False,
        )

        action = "updated"

    else:
        print(
            "OSP2-BE is up to date."
        )

        action = "current"

    # Re-read the file instead of pretending local == remote.
    final_local_version = get_local_version()

    return {
        "action": action,
        "local_version": final_local_version,
        "remote_version": remote_version,
        "file": str(OSP_FILE),
    }
