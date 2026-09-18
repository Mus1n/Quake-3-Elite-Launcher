import hashlib
import shutil
import re
from pathlib import Path

from base_methods import *
from download_tools import downloader


# ============================================================================
# OFFICIAL QUAKE 3 PAK FILES
# ============================================================================

PAK_FILES = {
    "pak0.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak0.pk3",
        "sha256": "7ce8b3910620cd50a09e4f1100f426e8c6180f68895d589f80e6bd95af54bcae",
    },
    "pak1.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak1.pk3",
        "sha256": "d4ffd60b4b414c3419499e321b6f5c2e933cf082df85823ad2d6ae2f803e1682",
    },
    "pak2.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak2.pk3",
        "sha256": "ccae938a2f13a03b24902d675181d516a431699701ed88023a307f34b5bcd58c",
    },
    "pak3.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak3.pk3",
        "sha256": "d03c0a0e06b99f9ecca2be7389f57faed406e85f7c09b9c56afdfa53ba25e312",
    },
    "pak4.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak4.pk3",
        "sha256": "af5f6d5c82fe4440ae0bb660f0648d1fa1731a9e8305a9eb652aa243428697f1",
    },
    "pak5.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak5.pk3",
        "sha256": "69f87070ca7719e252a3ba97e6483f6663939c987ede550d1268d4d9a07b45bc",
    },
    "pak6.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak6.pk3",
        "sha256": "bb4f0ae2bf603b050fb665436d3178ce7c1c20360e67bacf7c14d93daff38daf",
    },
    "pak7.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak7.pk3",
        "sha256": "de6283ce23e3486a2622c5dbf73d3721a59f24debd380e90f43a97d952fea283",
    },
    "pak8.pk3": {
        "url": "https://q3msk.net/files/original_paks/pak8.pk3",
        "sha256": "812c9e97f231e89cefede3848c6110b7bd34245093af6f22c2cacde3e6b15663",
    },
}


# ============================================================================
# SHA-256
# ============================================================================

def sha256_file(file_path, chunk_size=1024 * 1024):
    """
    Calculate SHA-256 without loading the whole file into RAM.
    """

    file_path = Path(file_path)

    if not file_path.is_file():
        return None

    sha256 = hashlib.sha256()

    with file_path.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)

            if not chunk:
                break

            sha256.update(chunk)

    return sha256.hexdigest().lower()


def verify_file(file_path, expected_hash):
    """
    Return True only when the file exists and its SHA-256 matches.
    """

    actual_hash = sha256_file(file_path)

    if actual_hash is None:
        return False

    return actual_hash == expected_hash.lower()


# ============================================================================
# Q3ELITE PATHS
# ============================================================================

def get_cached_pak(file_name):
    return CACHE_DIR / file_name


def get_installed_pak(file_name):
    return BASEQ3_DIR / file_name


# ============================================================================
# EXISTING QUAKE III INSTALLATIONS
# ============================================================================

_quake3_search_paths_cache = None


def get_quake3_search_paths():
    """
    Find possible existing Quake III baseq3 directories.

    Searches:
        - common standalone locations
        - default Steam locations
        - additional Steam libraries from libraryfolders.vdf

    Results are cached for the current launcher session.
    """

    global _quake3_search_paths_cache

    if _quake3_search_paths_cache is not None:
        return _quake3_search_paths_cache

    candidates = []

    # ------------------------------------------------------------------------
    # Common standalone locations
    # ------------------------------------------------------------------------

    common_paths = [
        Path(r"C:\Games\Quake 3 Arena\baseq3"),
        Path(r"C:\Games\Quake III Arena\baseq3"),
        Path(r"C:\Games\Quake3\baseq3"),

        Path(r"C:\Quake3\baseq3"),
        Path(r"C:\Quake 3\baseq3"),
        Path(r"C:\Quake 3 Arena\baseq3"),
        Path(r"C:\Quake III Arena\baseq3"),

        Path(r"C:\Program Files\Quake III Arena\baseq3"),
        Path(r"C:\Program Files (x86)\Quake III Arena\baseq3"),
    ]

    candidates.extend(common_paths)

    # ------------------------------------------------------------------------
    # Steam roots
    # ------------------------------------------------------------------------

    steam_roots = [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
    ]

    # Environment-based Program Files paths
    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")

    if program_files:
        steam_roots.append(
            Path(program_files) / "Steam"
        )

    if program_files_x86:
        steam_roots.append(
            Path(program_files_x86) / "Steam"
        )

    # ------------------------------------------------------------------------
    # Read Steam libraries
    # ------------------------------------------------------------------------

    steam_libraries = []

    for steam_root in steam_roots:

        if not steam_root.exists():
            continue

        steam_libraries.append(steam_root)

        library_file = (
            steam_root
            / "steamapps"
            / "libraryfolders.vdf"
        )

        if not library_file.is_file():
            continue

        try:
            text = library_file.read_text(
                encoding="utf-8",
                errors="ignore"
            )

            library_paths = re.findall(
                r'"path"\s+"([^"]+)"',
                text
            )

            for library_path in library_paths:

                library_path = library_path.replace(
                    "\\\\",
                    "\\"
                )

                steam_libraries.append(
                    Path(library_path)
                )

        except Exception as error:
            print(
                f"[warning] Could not read Steam libraries: {error}"
            )

    # ------------------------------------------------------------------------
    # Possible Steam Quake III directory names
    # ------------------------------------------------------------------------

    quake3_steam_names = [
        "Quake 3 Arena",
        "Quake III Arena",
        "Quake III",
        "Quake 3",
    ]

    for library in steam_libraries:

        for game_name in quake3_steam_names:

            candidates.append(
                library
                / "steamapps"
                / "common"
                / game_name
                / "baseq3"
            )

    # ------------------------------------------------------------------------
    # Remove duplicates and Q3Elite's own baseq3
    # ------------------------------------------------------------------------

    unique = []

    q3elite_baseq3 = BASEQ3_DIR.resolve()

    for path in candidates:

        try:
            normalized = path.resolve()
        except OSError:
            normalized = path

        # Do not "discover" our own Q3Elite installation.
        if normalized == q3elite_baseq3:
            continue

        if normalized not in unique:
            unique.append(normalized)

    _quake3_search_paths_cache = unique

    return unique


def find_existing_pak(file_name, expected_hash):
    """
    Search other Quake III installations for a valid PAK.

    A candidate is accepted ONLY when SHA-256 matches.
    """

    for baseq3 in get_quake3_search_paths():

        candidate = baseq3 / file_name

        if not candidate.is_file():
            continue

        print(f"[local] Found candidate: {candidate}")
        print(f"[hash] Verifying local {file_name}...")

        if verify_file(candidate, expected_hash):

            print(
                f"[local] Valid {file_name} found."
            )

            return candidate

        print(
            f"[local] Hash mismatch for {candidate}. Ignoring."
        )

    return None


def import_existing_pak(
    file_name,
    source_file,
    expected_hash
):
    """
    Import a verified PAK from another Quake III installation.

    Copies to:
        1. AppData persistent cache
        2. Q3Elite baseq3

    Original source file is never modified.
    """

    source_file = Path(source_file)

    cached_file = get_cached_pak(file_name)
    installed_file = get_installed_pak(file_name)

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    BASEQ3_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # Verify source again before copying.
    if not verify_file(
        source_file,
        expected_hash
    ):
        print(
            f"[error] Source {file_name} failed verification."
        )
        return False

    print(
        f"[import] Copying {file_name} to Q3Elite cache..."
    )

    shutil.copy2(
        source_file,
        cached_file
    )

    # Verify cached copy.
    if not verify_file(
        cached_file,
        expected_hash
    ):
        print(
            f"[error] Cached copy of {file_name} failed verification."
        )

        try:
            cached_file.unlink()
        except OSError:
            pass

        return False

    print(
        f"[import] Installing {file_name} into Q3Elite..."
    )

    shutil.copy2(
        cached_file,
        installed_file
    )

    # Verify installed copy.
    if not verify_file(
        installed_file,
        expected_hash
    ):
        print(
            f"[error] Installed copy of {file_name} failed verification."
        )
        return False

    return True


# ============================================================================
# CACHE RESTORE
# ============================================================================

def restore_from_cache(
    file_name,
    expected_hash
):
    """
    Restore a PAK from AppData cache if the cached copy is valid.
    """

    cached_file = get_cached_pak(file_name)
    installed_file = get_installed_pak(file_name)

    if not verify_file(
        cached_file,
        expected_hash
    ):
        return False

    BASEQ3_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    print(
        f"[repair] Restoring {file_name} from cache..."
    )

    shutil.copy2(
        cached_file,
        installed_file
    )

    return verify_file(
        installed_file,
        expected_hash
    )


# ============================================================================
# DOWNLOAD / REPAIR
# ============================================================================

def download_and_install_pak(
    file_name,
    file_url,
    expected_hash,
    control=None,
    progress_callback=None
):
    """
    Download PAK to AppData cache, verify SHA-256,
    then copy it to Q3Elite baseq3.
    """

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    BASEQ3_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    cached_file = get_cached_pak(file_name)
    installed_file = get_installed_pak(file_name)

    # We only reach this function when the final cache is missing or invalid.
    # Preserve an interrupted legacy download as .part so downloader() can
    # validate its size against the server and resume it with HTTP Range.
    part_file = Path(str(cached_file) + ".part")

    if cached_file.exists():
        try:
            if part_file.exists():
                part_file.unlink()
            cached_file.replace(part_file)
            print(
                f"[cache] Preserving incomplete {file_name} for resume: "
                f"{part_file}"
            )
        except OSError as error:
            print(
                f"[error] Could not preserve partial cache "
                f"{cached_file}: {error}"
            )
            return False

    print(
        f"[download] Downloading clean {file_name}..."
    )

    downloaded = downloader(
        file_url,
        str(CACHE_DIR),
        file_name,
        skip=True,
        control=control,
        progress_callback=progress_callback,
        use_part_file=True
    )

    if not downloaded:
        print(
            f"[error] Failed to download {file_name}"
        )
        return False

    # ------------------------------------------------------------------------
    # Verify downloaded cache
    # ------------------------------------------------------------------------

    print(
        f"[hash] Verifying downloaded {file_name}..."
    )

    if not verify_file(
        cached_file,
        expected_hash
    ):

        print(
            f"[error] SHA-256 mismatch after downloading {file_name}"
        )

        try:
            cached_file.unlink()
        except OSError:
            pass

        return False

    print(
        f"[hash] {file_name}: downloaded file verified."
    )

    # ------------------------------------------------------------------------
    # Install from verified cache
    # ------------------------------------------------------------------------

    shutil.copy2(
        cached_file,
        installed_file
    )

    # ------------------------------------------------------------------------
    # Verify installed copy
    # ------------------------------------------------------------------------

    if not verify_file(
        installed_file,
        expected_hash
    ):
        print(
            f"[error] Installed copy of {file_name} failed verification."
        )
        return False

    return True


# ============================================================================
# VERIFY ALL PAKS
# ============================================================================

def verify_paks(control=None, progress_callback=None):
    """
    Verify pak0.pk3 - pak8.pk3.

    Repair priority:

        1. Q3Elite baseq3
           Valid -> keep it.

        2. AppData cache
           Valid -> restore Q3Elite copy.

        3. Existing Quake III installation
           Valid -> copy to cache + Q3Elite.

        4. Internet
           Download to cache -> verify -> install -> verify.

    Returns:
        True  - all PAKs are valid
        False - one or more PAKs could not be repaired
    """

    print()
    print("========================================")
    print(" Q3Elite - Verifying Quake 3 PAK files")
    print("========================================")
    print()

    all_valid = True

    for file_name, info in PAK_FILES.items():

        expected_hash = info["sha256"].lower()
        file_url = info["url"]

        installed_file = get_installed_pak(
            file_name
        )

        cached_file = get_cached_pak(
            file_name
        )

        print()
        print(f"[check] {file_name}")

        # ====================================================================
        # 1. CHECK Q3ELITE INSTALLATION
        # ====================================================================

        if verify_file(
            installed_file,
            expected_hash
        ):

            print(
                f"[OK] {file_name}"
            )

            # ---------------------------------------------------------------
            # Optional:
            # If installed file is valid but cache is missing/corrupt,
            # create a clean recovery cache from our verified installed copy.
            # ---------------------------------------------------------------

            if not verify_file(
                cached_file,
                expected_hash
            ):

                print(
                    f"[cache] Creating recovery cache for {file_name}..."
                )

                CACHE_DIR.mkdir(
                    parents=True,
                    exist_ok=True
                )

                try:
                    shutil.copy2(
                        installed_file,
                        cached_file
                    )

                    if verify_file(
                        cached_file,
                        expected_hash
                    ):
                        print(
                            f"[cache] {file_name} cached."
                        )
                    else:
                        print(
                            f"[warning] Failed to verify cached {file_name}."
                        )

                except OSError as error:
                    print(
                        f"[warning] Could not cache {file_name}: {error}"
                    )

            continue

        # ====================================================================
        # INSTALLED FILE IS MISSING / CORRUPTED
        # ====================================================================

        if installed_file.exists():

            print(
                f"[warning] {file_name} is modified or corrupted."
            )

        else:

            print(
                f"[warning] {file_name} is missing."
            )

        # ====================================================================
        # 2. TRY APPDATA CACHE
        # ====================================================================

        if verify_file(
            cached_file,
            expected_hash
        ):

            print(
                f"[cache] Valid cached copy found."
            )

            if restore_from_cache(
                file_name,
                expected_hash
            ):

                print(
                    f"[OK] {file_name} restored from cache."
                )

                continue

            print(
                f"[warning] Failed to restore {file_name} from cache."
            )

        elif cached_file.exists():

            print(
                f"[warning] Cached {file_name} is incomplete or corrupted."
            )
            print(
                f"[cache] Keeping it temporarily so the downloader can "
                f"attempt HTTP resume."
            )

        # ====================================================================
        # 3. SEARCH EXISTING QUAKE III INSTALLATIONS
        # ====================================================================

        existing_pak = find_existing_pak(
            file_name,
            expected_hash
        )

        if existing_pak is not None:

            print(
                f"[local] Using existing Quake III file:"
            )

            print(
                f"        {existing_pak}"
            )

            if import_existing_pak(
                file_name,
                existing_pak,
                expected_hash
            ):

                print(
                    f"[OK] {file_name} imported from existing Quake III."
                )

                continue

            print(
                f"[warning] Failed to import existing {file_name}."
            )

        # ====================================================================
        # 4. DOWNLOAD FROM INTERNET
        # ====================================================================

        print(
            f"[local] No usable local copy of {file_name} found."
        )

        if download_and_install_pak(
            file_name,
            file_url,
            expected_hash,
            control=control,
            progress_callback=progress_callback
        ):

            print(
                f"[OK] {file_name} downloaded and repaired."
            )

        else:

            print(
                f"[FAILED] Could not repair {file_name}."
            )

            all_valid = False

    # ========================================================================
    # RESULT
    # ========================================================================

    print()
    print("========================================")

    if all_valid:

        print(
            "[OK] All Quake 3 PAK files verified."
        )

    else:

        print(
            "[ERROR] One or more PAK files failed verification."
        )

    print("========================================")
    print()

    return all_valid


# ============================================================================
# STANDALONE TEST
# ============================================================================

if __name__ == "__main__":
    verify_paks()