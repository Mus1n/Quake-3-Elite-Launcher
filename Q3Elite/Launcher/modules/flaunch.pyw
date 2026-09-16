import os
import sys
import shutil
import json
import urllib.parse
import urllib.request
import zipfile

from pathlib import Path


# ============================================================================
# PATHS
# ============================================================================

LAUNCHER_DIR = Path(__file__).resolve().parent.parent
GAME_ROOT = LAUNCHER_DIR.parent.parent

os.chdir(LAUNCHER_DIR)


# ============================================================================
# Q3ELITE MODULES
# ============================================================================

import download_tools as dt

from upd_tools import get_updates, update
from pak_verifier import verify_paks
from bmods_tools import *
from base_methods import *
from gui_tools import *


# ============================================================================
# Q3ELITE VERSION
# ============================================================================

def q3elite_is_current():
    """
    Temporary fake version checker.

    True:
        Current Quake 3 Elite version is already downloaded.
        Skip pCloud download and use the existing archive.

    TODO:
        Replace this with real local/remote version comparison.
    """

    return True


# ============================================================================
# PCLOUD
# ============================================================================

def resolve_pcloud_file(public_link, wanted_name):
    """
    Resolve a pCloud public share link to the actual file download URL.

    Example public link:
        https://e.pcloud.link/publink/show?code=...

    Process:
        1. Extract public link code.
        2. Call showpublink.
        3. Find wanted file.
        4. Get fileid and expected size.
        5. Call getpublinkdownload.
        6. Return actual temporary download URL.
    """

    parsed = urllib.parse.urlparse(public_link)
    params = urllib.parse.parse_qs(parsed.query)

    code = params.get("code", [None])[0]

    if not code:
        raise RuntimeError(
            "Invalid pCloud public link: missing code."
        )

    api = "https://eapi.pcloud.com"

    # ------------------------------------------------------------------------
    # READ PUBLIC SHARE
    # ------------------------------------------------------------------------

    show_url = (
        f"{api}/showpublink?"
        + urllib.parse.urlencode({
            "code": code
        })
    )

    with urllib.request.urlopen(
        show_url,
        timeout=30
    ) as response:

        data = json.load(response)

    if data.get("result") != 0:
        raise RuntimeError(
            f"pCloud API error: "
            f"{data.get('error', data.get('result'))}"
        )

    contents = (
        data
        .get("metadata", {})
        .get("contents", [])
    )

    file_info = next(
        (
            item
            for item in contents
            if item.get("name") == wanted_name
            and not item.get("isfolder", False)
        ),
        None
    )

    if file_info is None:
        raise FileNotFoundError(
            f"{wanted_name} was not found in pCloud."
        )

    file_id = file_info["fileid"]
    expected_size = file_info["size"]

    # ------------------------------------------------------------------------
    # GET ACTUAL DOWNLOAD URL
    # ------------------------------------------------------------------------

    download_api_url = (
        f"{api}/getpublinkdownload?"
        + urllib.parse.urlencode({
            "code": code,
            "fileid": file_id
        })
    )

    with urllib.request.urlopen(
        download_api_url,
        timeout=30
    ) as response:

        download = json.load(response)

    if download.get("result") != 0:
        raise RuntimeError(
            f"pCloud download URL error: "
            f"{download.get('error', download.get('result'))}"
        )

    hosts = download.get("hosts", [])
    path = download.get("path")

    if not hosts or not path:
        raise RuntimeError(
            "pCloud returned no download server."
        )

    download_url = f"https://{hosts[0]}{path}"

    return download_url, expected_size


# ============================================================================
# Q3ELITE ARCHIVE INSTALLATION
# ============================================================================

def install_q3elite_archive(zip_path):
    """
    Extract Quake 3 Elite to a temporary staging directory first,
    then install it into the actual Q3Elite directory.
    """

    zip_path = Path(zip_path)

    install_root = Path(__file__).resolve().parents[3]

    archive_prefix = "Quake 3 Arena/Quake 3 Elite/"

    staging_root = (
        Q3ELITE_TEMP_DIR
        / "Q3Elite_Extracted"
    )

    print()
    print("========================================")
    print(" Installing Quake 3 Elite")
    print("========================================")
    print()

    print(f"Archive:     {zip_path}")
    print(f"Staging:     {staging_root}")
    print(f"Destination: {install_root}")
    print()

    # ========================================================================
    # VERIFY ARCHIVE
    # ========================================================================

    if not zip_path.is_file():
        raise FileNotFoundError(
            f"Quake 3 Elite.zip was not found:\n{zip_path}"
        )

    print("Checking archive...")

    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError(
            "Quake 3 Elite.zip is not a valid ZIP archive."
        )

    print("Archive is valid.")

    # ========================================================================
    # CLEAN OLD STAGING DIRECTORY
    # ========================================================================

    if staging_root.exists():

        print("Removing old temporary extraction...")

        shutil.rmtree(
            staging_root,
            ignore_errors=False
        )

    staging_root.mkdir(
        parents=True,
        exist_ok=True
    )

    # ========================================================================
    # EXTRACT TO STAGING
    # ========================================================================

    print()
    print("Extracting Quake 3 Elite...")
    print()

    extracted_files = 0

    with zipfile.ZipFile(zip_path, "r") as archive:

        for member in archive.infolist():

            member_name = member.filename.replace(
                "\\",
                "/"
            )

            if not member_name.startswith(
                archive_prefix
            ):
                continue

            relative_name = member_name[
                len(archive_prefix):
            ]

            if not relative_name:
                continue

            relative_path = Path(relative_name)

            # Prevent paths such as ../../something
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
            ):
                raise RuntimeError(
                    f"Unsafe path inside ZIP: {member_name}"
                )

            destination = (
                staging_root
                / relative_path
            )

            if member.is_dir():

                destination.mkdir(
                    parents=True,
                    exist_ok=True
                )

                continue

            destination.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            with archive.open(member, "r") as source:

                with destination.open("wb") as target:

                    shutil.copyfileobj(
                        source,
                        target,
                        length=1024 * 1024
                    )

            extracted_files += 1

            if extracted_files % 100 == 0:

                print(
                    f"Extracted "
                    f"{extracted_files} files..."
                )

    # At this point the ZIP has been CLOSED.

    if extracted_files == 0:

        raise RuntimeError(
            "No Quake 3 Elite files were found "
            "inside the archive.\n"
            f"Expected prefix: {archive_prefix}"
        )

    print()
    print(
        f"Archive extraction complete: "
        f"{extracted_files} files."
    )

    # ========================================================================
    # VERIFY EXPECTED DIRECTORIES
    # ========================================================================

    staged_baseq3 = (
        staging_root
        / "baseq3"
    )

    staged_q3elite = (
        staging_root
        / "Q3Elite"
    )

    if not staged_baseq3.is_dir():

        raise RuntimeError(
            "Extracted archive does not contain "
            "the expected baseq3 directory."
        )

    if not staged_q3elite.is_dir():

        raise RuntimeError(
            "Extracted archive does not contain "
            "the expected Q3Elite directory."
        )

    # ========================================================================
    # INSTALL
    # ========================================================================

    print()
    print("Installing extracted files...")
    print()

    install_root.mkdir(
        parents=True,
        exist_ok=True
    )

    installed_files = 0

    for source in staging_root.rglob("*"):

        relative_path = source.relative_to(
            staging_root
        )

        destination = (
            install_root
            / relative_path
        )

        if source.is_dir():

            destination.mkdir(
                parents=True,
                exist_ok=True
            )

            continue

        destination.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        shutil.copy2(
            source,
            destination
        )

        installed_files += 1

        if installed_files % 100 == 0:

            print(
                f"Installed "
                f"{installed_files} files..."
            )

    # ========================================================================
    # VERIFY INSTALLATION
    # ========================================================================

    print()
    print("Verifying installation...")

    if not (install_root / "baseq3").is_dir():

        raise RuntimeError(
            "baseq3 was not installed."
        )

    if not (install_root / "Q3Elite").is_dir():

        raise RuntimeError(
            "Q3Elite directory was not installed."
        )

    installed_count = sum(
        1
        for path in install_root.rglob("*")
        if path.is_file()
    )

    print(
        f"Installed files found: {installed_count}"
    )

    # ========================================================================
    # CLEAN STAGING
    # ========================================================================

    print("Cleaning temporary extraction...")

    shutil.rmtree(
        staging_root,
        ignore_errors=True
    )

    # ========================================================================
    # DONE
    # ========================================================================

    print()
    print("========================================")
    print(" Quake 3 Elite installation complete")
    print("========================================")
    print()

    print(
        f"Extracted: {extracted_files} files"
    )

    print(
        f"Installed: {installed_files} files"
    )

    print()

# ============================================================================
# Q3ELITE DOWNLOAD / INSTALL THREAD
# ============================================================================

class Q3EliteDownload(QtCore.QThread):

    result_ready = pyqtSignal(bool)

    def run(self):

        try:

            Q3ELITE_TEMP_DIR.mkdir(
                parents=True,
                exist_ok=True
            )

            zip_path = (
                Q3ELITE_TEMP_DIR
                / "Quake 3 Elite.zip"
            )

            print()
            print("Checking Quake 3 Elite version...")

            # =================================================================
            # VERSION CHECK
            # =================================================================

            if q3elite_is_current():

                print(
                    "Quake 3 Elite is up to date."
                )

                print(
                    "Skipping Q3Elite download."
                )

            else:

                print(
                    "New Quake 3 Elite version available."
                )

                print(
                    "Downloading Quake 3 Elite.zip..."
                )

                # =============================================================
                # READ PCLOUD CONFIG
                # =============================================================

                conf_file = (
                    DOWNLOAD_CONFS_DIR
                    / "Quake 3 Elite pcloud.dconf"
                )

                if not conf_file.is_file():
                    raise FileNotFoundError(
                        f"pCloud configuration not found:\n"
                        f"{conf_file}"
                    )

                public_link = conf_file.read_text(
                    encoding="utf-8"
                ).strip()

                if not public_link:
                    raise RuntimeError(
                        "Quake 3 Elite pCloud URL is empty."
                    )

                # =============================================================
                # RESOLVE PCLOUD FILE
                # =============================================================

                download_url, expected_size = (
                    resolve_pcloud_file(
                        public_link,
                        "Quake 3 Elite.zip"
                    )
                )

                print(
                    f"Expected archive size: "
                    f"{expected_size} bytes"
                )

                # =============================================================
                # REMOVE OLD ARCHIVE
                # =============================================================

                if zip_path.exists():

                    print(
                        "Removing old Quake 3 Elite.zip..."
                    )

                    zip_path.unlink()

                # =============================================================
                # DOWNLOAD
                # =============================================================

                result = dt.downloader(
                    download_url,
                    str(Q3ELITE_TEMP_DIR),
                    "Quake 3 Elite.zip",
                    skip=True
                )

                if not result:
                    raise RuntimeError(
                        "Failed to download "
                        "Quake 3 Elite.zip."
                    )

                # =============================================================
                # VERIFY SIZE
                # =============================================================

                actual_size = zip_path.stat().st_size

                if actual_size != expected_size:
                    raise RuntimeError(
                        "Downloaded Q3Elite archive "
                        "has incorrect size.\n"
                        f"Expected: {expected_size}\n"
                        f"Actual:   {actual_size}"
                    )

                print(
                    "Quake 3 Elite.zip downloaded successfully."
                )

            # =================================================================
            # INSTALL EXISTING OR DOWNLOADED ARCHIVE
            # =================================================================

            install_q3elite_archive(
                zip_path
            )

            self.result_ready.emit(
                True
            )

        except Exception as error:

            print()
            print(
                f"[error] Quake 3 Elite "
                f"installation failed: {error}"
            )
            print()

            self.result_ready.emit(
                False
            )


# ============================================================================
# BASE DOWNLOAD / UPDATE
# ============================================================================

class FDownload(QtCore.QThread):

    result_ready = pyqtSignal(bool)

    def run(self):

        try:

            baseq3_dir = GAME_ROOT / "baseq3"

            (
                baseq3_dir
                / "mods"
                / "baseq3"
            ).mkdir(
                parents=True,
                exist_ok=True
            )

            (
                baseq3_dir
                / "mods"
                / "osp"
                / "demos"
            ).mkdir(
                parents=True,
                exist_ok=True
            )

            if not os.path.exists("./cache"):
                os.mkdir("./cache")

            bmod_conf.save()

            # ============================================================
            # VERIFY OFFICIAL QUAKE 3 PAKS
            # ============================================================

            paks_ok = verify_paks()

            if not paks_ok:
                raise RuntimeError(
                    "One or more official Quake 3 PAK files "
                    "could not be verified or repaired."
                )

            self.result_ready.emit(True)

        except Exception as error:

            print()
            print(
                f"[error] Base installation failed: {error}"
            )
            print()

            self.result_ready.emit(False)
            
            
# ============================================================================
# POST-INSTALL UPDATE
# ============================================================================

class PostInstallUpdate(QtCore.QThread):

    result_ready = pyqtSignal(bool)

    def run(self):

        try:

            print()
            print("========================================")
            print(" Checking OSP2-BE")
            print("========================================")
            print()

            osp_file = (
                BASEQ3_DIR
                / "mods"
                / "osp"
                / "zz-osp-pak8be.pk3"
            )

            # ================================================================
            # CHECK FILE
            # ================================================================

            osp_file_exists = osp_file.is_file()

            if osp_file_exists:

                print(
                    f"Installed file: {osp_file}"
                )

            else:

                print(
                    f"OSP2-BE file is missing: {osp_file}"
                )

            # ================================================================
            # CHECK VERSION
            # ================================================================

            updates = get_updates()

            osp_update_available = (
                "OSP2-BE" in updates
            )

            # ================================================================
            # UPDATE / REPAIR
            # ================================================================

            if osp_update_available:

                print()
                print(
                    "New OSP2-BE version available."
                )
                print(
                    "Updating OSP2-BE..."
                )
                print()

                update(
                    "OSP2-BE"
                )

            elif not osp_file_exists:

                print()
                print(
                    "OSP2-BE version is current, "
                    "but the PK3 file is missing."
                )
                print(
                    "Repairing OSP2-BE..."
                )
                print()

                osp_dconf = (
                    DOWNLOAD_CONFS_DIR
                    / "OSP2-BE.dconf"
                )

                if not osp_dconf.is_file():
                    raise FileNotFoundError(
                        f"OSP2-BE.dconf not found: {osp_dconf}"
                    )

                dt.download(
                    str(osp_dconf),
                    skip=True
                )

            else:

                print()
                print(
                    "OSP2-BE is up to date."
                )

            # ================================================================
            # FINAL FILE CHECK
            # ================================================================

            if not osp_file.is_file():

                raise RuntimeError(
                    "OSP2-BE installation finished, but "
                    "zz-osp-pak8be.pk3 is still missing."
                )

            print()
            print(
                f"OSP2-BE verified: {osp_file}"
            )

            print()
            print("========================================")
            print(" OSP2-BE check complete")
            print("========================================")
            print()

            self.result_ready.emit(
                True
            )

        except Exception as error:

            print()
            print(
                f"[error] OSP2-BE update failed: {error}"
            )
            print()

            self.result_ready.emit(
                False
            )

# ============================================================================
# MOD LIST DOWNLOAD
# ============================================================================

class MDownload(QtCore.QThread):

    result_ready = pyqtSignal(bool)

    def run(self):

        get_modlist()


# ============================================================================
# MOD LIST CHECK
# ============================================================================

def mdlist_check():

    window.upd_status(
        False
    )

    if not os.path.exists(
        "./temp_files/modlist.json"
    ):

        window.qerror(
            "Internet connection error."
        )


# ============================================================================
# MAIN
# ============================================================================

install_state = {
    "base_done": False,
    "base_ok": False,

    "q3elite_done": False,
    "q3elite_ok": False,

    "post_update_started": False,
}


def base_install_result(success):
    install_state["base_done"] = True
    install_state["base_ok"] = success

    check_install_finished()


def q3elite_install_result(success):
    install_state["q3elite_done"] = True
    install_state["q3elite_ok"] = success

    check_install_finished()


def check_install_finished():

    # Wait for PAK verification AND Q3Elite extraction.
    if not (
        install_state["base_done"]
        and install_state["q3elite_done"]
    ):
        return

    # One of the two installation stages failed.
    if not (
        install_state["base_ok"]
        and install_state["q3elite_ok"]
    ):

        print()
        print("========================================")
        print(" Q3Elite installation FAILED")
        print("========================================")
        print()

        window.qerror(
            "Q3Elite installation failed.\n"
            "Check the installation log."
        )

        return

    # Prevent starting the post-update thread twice.
    if install_state["post_update_started"]:
        return

    install_state["post_update_started"] = True

    print()
    print("========================================")
    print(" Base installation complete")
    print(" Starting post-install updates...")
    print("========================================")
    print()

    post_update.start()


def post_update_result(success):

    if not success:

        print()
        print("========================================")
        print(" Post-install update FAILED")
        print("========================================")
        print()

        window.qerror(
            "Q3Elite update failed.\n"
            "Check the installation log."
        )

        return

    print()
    print("========================================")
    print(" Q3Elite installation complete")
    print("========================================")
    print()

    window.close_terminal()


if __name__ == "__main__":

    try:

        app = QApplication(
            sys.argv
        )

        # --------------------------------------------------------------------
        # FONT
        # --------------------------------------------------------------------

        font_id = (
            QFontDatabase
            .addApplicationFont(
                "./ui/FiraCode-Regular.ttf"
            )
        )

        if font_id == -1:

            print(
                "Error: could not load "
                "./ui/FiraCode-Regular.ttf"
            )

            family = "Arial"

        else:

            family = (
                QFontDatabase
                .applicationFontFamilies(
                    font_id
                )[0]
            )

        # --------------------------------------------------------------------
        # GUI
        # --------------------------------------------------------------------

        app.setStyleSheet(
            DARK_STYLE
        )

        window = MainWindow()

        window.terminal.set_font(
            family
        )

        window.show()
        window.open_terminal()

        # --------------------------------------------------------------------
        # WORKERS
        # --------------------------------------------------------------------

        fdownload = FDownload()
        mdownload = MDownload()
        q3elite_download = Q3EliteDownload()
        post_update = PostInstallUpdate()

        mdownload.finished.connect(
            mdlist_check
        )

        fdownload.result_ready.connect(
            base_install_result
        )

        q3elite_download.result_ready.connect(
            q3elite_install_result
        )

        post_update.result_ready.connect(
            post_update_result
        )

        # --------------------------------------------------------------------
        # START
        # --------------------------------------------------------------------

        mdownload.start()
        fdownload.start()
        q3elite_download.start()

        sys.exit(
            app.exec()
        )

    except Exception as error:

        message = (
            f"{type(error).__name__}: {error}\n"
            "If after restart you see this message, "
            "write me (t.me/konstalker)"
        )

        show_error(
            message,
            lambda: update("scripts")
        )