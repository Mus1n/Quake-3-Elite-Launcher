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

from osp_updater import check_and_update as check_and_update_osp
from q3elite_updater import check_and_update as check_and_update_q3elite
from pak_verifier import verify_paks
from launcher_updater import check_for_update as check_launcher_update, stage_update as stage_launcher_update, launch_apply_helper
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
# INSTALLATION STATE
# ============================================================================

def q3elite_is_installed():
    """Q3Elite is installed once its Engines directory exists."""
    engines_dir = GAME_ROOT / "Q3Elite" / "Engines"
    print(f"Q3Elite install marker: {engines_dir}")
    return engines_dir.is_dir()


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
                    skip=True,
                    control=download_control,
                    progress_callback=download_progress_callback,
                    expected_size=expected_size,
                    use_part_file=True
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
# INSTALLED Q3ELITE UPDATE — STEP 17
# ============================================================================

class Q3EliteUpdate(QtCore.QThread):

    result_ready = pyqtSignal(bool)
    offline = pyqtSignal(str)

    def run(self):
        """
        Run the Step 16 Q3Elite updater before PAK/OSP checks.

        Same version:
            remote Version.json only -> local manifest existence check.

        New version:
            remote manifest diff -> incremental pCloud update.

        Network failure:
            non-blocking for an already installed game. PLAY can continue
            after the independent PAK/OSP checks.

        Update/verification failure after metadata was reached:
            blocking, because a partially applied Q3Elite release should be
            retried instead of being silently accepted as READY.
        """
        try:
            print()
            print("========================================")
            print(" Checking Q3Elite update")
            print("========================================")
            print()

            result = check_and_update_q3elite(
                profile="full",
                control=download_control,
                progress_callback=download_progress_callback,
            )

            print()
            print(f"Q3Elite status: {result.get('status', 'unknown')}")
            print()
            print("========================================")
            print(" Q3Elite update check complete")
            print("========================================")
            print()

            self.result_ready.emit(True)

        except Exception as error:
            # Step 17 keeps startup usable when the remote version check itself
            # is unavailable. Detect the common network-layer exceptions without
            # coupling q3elite_updater.py to Qt.
            import socket
            import urllib.error

            network_error = isinstance(
                error,
                (
                    urllib.error.URLError,
                    TimeoutError,
                    socket.timeout,
                    ConnectionError,
                ),
            )

            if network_error:
                print()
                print(f"[offline] Q3Elite update check unavailable: {error}")
                print("Using the installed Q3Elite release.")
                print()

                self.offline.emit(str(error))
                self.result_ready.emit(True)
                return

            print()
            print(f"[error] Q3Elite update failed: {error}")
            print()
            self.result_ready.emit(False)


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

            # ============================================================
            # VERIFY OFFICIAL QUAKE 3 PAKS
            # ============================================================

            paks_ok = verify_paks(
                control=download_control,
                progress_callback=download_progress_callback
            )

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
    offline = pyqtSignal(str)

    def run(self):

        osp_file = (
            BASEQ3_DIR
            / "mods"
            / "osp"
            / "zz-osp-pak8be.pk3"
        )

        try:
            print()
            print("========================================")
            print(" Checking OSP2-BE")
            print("========================================")
            print()

            if osp_file.is_file():
                print(f"Installed file: {osp_file}")
            else:
                print(f"OSP2-BE file is missing: {osp_file}")

            result = check_and_update_osp()

            print()
            print(
                "OSP2-BE status: "
                f"{result['action']}"
            )

            if osp_file.is_file():
                print(f"OSP2-BE verified: {osp_file}")
            else:
                print("[warning] OSP2-BE is still missing.")
                print(
                    "PLAY remains available; repair will be "
                    "retried when online."
                )

            print()
            print("========================================")
            print(" OSP2-BE check complete")
            print("========================================")
            print()

            self.result_ready.emit(True)

        except Exception as error:
            # OSP2-BE is optional for launching the engine. A network/update
            # failure must therefore not brick an otherwise valid installation.
            print()
            print(
                f"[offline] OSP2-BE update check unavailable: "
                f"{error}"
            )
            print("Continuing without blocking PLAY.")
            print()

            self.offline.emit(str(error))
            self.result_ready.emit(True)


# ============================================================================
# LAUNCHER SELF-UPDATE
# ============================================================================

class LauncherSelfUpdate(QtCore.QThread):

    result_ready = pyqtSignal(str)

    def run(self):
        """
        Check GitHub before PAK/OSP work.

        Results:
            "continue" -> no update / offline / failed check; continue startup
            "restart"  -> update staged and helper started; close this launcher
        """
        try:
            print()
            print("========================================")
            print(" Checking Launcher update")
            print("========================================")
            print()

            update_info = check_launcher_update()

            if not update_info:
                self.result_ready.emit("continue")
                return

            print(
                f"Launcher update available: "
                f"{update_info['local_version']} -> "
                f"{update_info['remote_version']}"
            )
            print(f"Changed files: {len(update_info['files'])}")
            print()

            stage_launcher_update(
                update_info,
                control=download_control,
                progress_callback=download_progress_callback,
            )

            launch_apply_helper(os.getpid())
            self.result_ready.emit("restart")

        except Exception as error:
            # Self-update must not brick an otherwise usable launcher.
            print()
            print(f"[warning] Launcher self-update unavailable: {error}")
            print("Continuing normal startup.")
            print()
            self.result_ready.emit("continue")


# ============================================================================
# SHARED DOWNLOAD CONTROL
# ============================================================================

download_control = dt.DownloadControl()

# All downloader calls use this shared controller unless a caller supplies
# another one explicitly.
dt.set_default_download_control(download_control)

_download_progress = {
    "name": "",
    "downloaded": 0,
    "total": None,
    "speed": 0.0,
}


def download_progress_callback(downloaded, total, speed, file_name):
    """Worker-thread callback. Store numbers only; Qt GUI reads them on its timer."""
    _download_progress["name"] = file_name
    _download_progress["downloaded"] = downloaded
    _download_progress["total"] = total
    _download_progress["speed"] = speed


dt.set_default_progress_callback(download_progress_callback)


def _format_bytes(value):
    value = float(value or 0)
    units = ("B", "KB", "MB", "GB")
    unit = units[0]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            break
        value /= 1024.0
    return f"{value:.1f} {unit}"


def toggle_download_pause():
    if download_control.paused:
        download_control.resume()
        pause_button.setText("PAUSE")
        print("\\n[download] Resumed.")
    else:
        download_control.pause()
        pause_button.setText("RESUME")
        print("\\n[download] Paused.")


def update_download_overlay():
    """Refresh the small download control without touching worker threads."""
    if not pause_button.isVisible():
        return

    name = _download_progress["name"]
    done = _download_progress["downloaded"]
    total = _download_progress["total"]
    speed = _download_progress["speed"]

    if name and done:
        if total:
            info = (
                f"{name}  |  {_format_bytes(done)} / {_format_bytes(total)}"
                f"  |  {_format_bytes(speed)}/s"
            )
        else:
            info = (
                f"{name}  |  {_format_bytes(done)}"
                f"  |  {_format_bytes(speed)}/s"
            )
        download_info.setText(info)


def show_download_controls():
    pause_button.setText("RESUME" if download_control.paused else "PAUSE")
    pause_button.show()
    download_info.show()
    position_download_controls()


def hide_download_controls():
    pause_button.hide()
    download_info.hide()


def position_download_controls():
    """Keep controls in the lower-right corner of the current launcher window."""
    margin = 18
    button_w = 125
    button_h = 36
    info_h = 24
    info_w = max(260, window.width() - button_w - margin * 3)

    y = max(margin, window.height() - button_h - margin)
    pause_button.setGeometry(
        max(margin, window.width() - button_w - margin),
        y,
        button_w,
        button_h
    )
    download_info.setGeometry(
        margin,
        y + (button_h - info_h) // 2,
        info_w,
        info_h
    )


# ============================================================================
# GUI STATE HELPERS
# ============================================================================

def _disconnect_main_button():
    """Remove old Launch/Update connections before assigning a new action."""
    try:
        window.pushButton.clicked.disconnect()
    except (TypeError, RuntimeError):
        pass


def set_gui_checking(text="Checking..."):
    """Keep the existing GUI, but make its main button reflect background work."""
    try:
        _disconnect_main_button()
        window.pushButton.setText(text)
        window.pushButton.setEnabled(False)
        show_download_controls()
    except Exception:
        pass


def set_gui_ready(offline=False):
    """Switch the main button directly to Launch without the legacy updater."""
    try:
        hide_download_controls()
        _disconnect_main_button()

        window.pushButton.setText("Launch")
        window.pushButton.setEnabled(True)
        window.pushButton.clicked.connect(window.launch)

        if offline:
            print("Launcher ready - OFFLINE MODE.")
        else:
            print("Launcher ready.")

        window.close_terminal()

    except Exception as error:
        print(f"[warning] Could not update GUI READY state: {error}")


def set_gui_error(text="RETRY"):
    try:
        hide_download_controls()
        _disconnect_main_button()
        window.pushButton.setText(text)
        window.pushButton.setEnabled(True)
        window.pushButton.clicked.connect(start_local_check)
    except Exception:
        pass


def start_local_check():
    """Retry local verification without restarting the launcher."""
    global fdownload, post_update

    if q3elite_update.isRunning() or fdownload.isRunning() or post_update.isRunning():
        return

    install_state["base_done"] = False
    install_state["base_ok"] = False
    install_state["post_update_started"] = False
    install_state["offline"] = False
    download_control.reset()

    if q3elite_is_installed():
        install_state["q3elite_done"] = False
        install_state["q3elite_ok"] = False
        install_state["q3elite_update_done"] = False
        install_state["q3elite_update_ok"] = False
        set_gui_checking("Checking Q3Elite...")
        q3elite_update.start()
    else:
        # First-install button handling will be expanded to DOWNLOAD /
        # PAUSE / RESUME in the next downloader pass. For now retry the
        # existing installation pipeline.
        install_state["q3elite_done"] = False
        install_state["q3elite_ok"] = False
        set_gui_checking("Installing...")
        q3elite_download.start()

def start_game_checks():
    """Start the existing Q3Elite/PAK/OSP pipeline after self-update resolves."""
    if q3elite_is_installed():
        print()
        print("Existing Q3Elite installation detected.")
        print("Verifying local PAK files and checking OSP2-BE...")
        print()

        # Step 17: Q3Elite update is serialized before PAK verification.
        # This avoids simultaneous writes into baseq3/Q3Elite.
        install_state["q3elite_done"] = False
        install_state["q3elite_ok"] = False
        install_state["q3elite_update_done"] = False
        install_state["q3elite_update_ok"] = False

        set_gui_checking("Checking Q3Elite...")
        q3elite_update.start()
    else:
        print()
        print("Q3Elite is not installed.")
        print("Starting first installation...")
        print()

        # Serialize first installation. The Q3Elite archive contains baseq3,
        # therefore PAK verification must not write there at the same time.
        install_state["q3elite_done"] = False
        install_state["q3elite_ok"] = False
        set_gui_checking("Installing...")
        q3elite_download.start()


def launcher_self_update_result(action):
    if action == "restart":
        print()
        print("Launcher update staged. Restarting...")
        print()
        app.quit()
        return

    start_game_checks()


# ============================================================================
# MAIN
# ============================================================================

install_state = {
    "base_done": False,
    "base_ok": False,

    "q3elite_done": False,
    "q3elite_ok": False,

    "q3elite_update_done": False,
    "q3elite_update_ok": False,

    "post_update_started": False,
    "offline": False,
}


def q3elite_update_offline(reason):
    install_state["offline"] = True
    print(f"[offline] {reason}")


def q3elite_update_result(success):
    install_state["q3elite_update_done"] = True
    install_state["q3elite_update_ok"] = success

    if not success:
        print()
        print("========================================")
        print(" Q3Elite update FAILED")
        print("========================================")
        print()

        set_gui_error("RETRY")
        window.qerror(
            "Q3Elite update failed.\n"
            "The previous Version.json was kept, so the update can be retried."
        )
        return

    # Existing installation is now accepted for this startup.
    install_state["q3elite_done"] = True
    install_state["q3elite_ok"] = True

    # Continue serially with official PAK verification.
    set_gui_checking("Checking PAKs...")
    fdownload.start()


def base_install_result(success):
    install_state["base_done"] = True
    install_state["base_ok"] = success

    check_install_finished()


def q3elite_install_result(success):
    install_state["q3elite_done"] = True
    install_state["q3elite_ok"] = success
    install_state["q3elite_update_done"] = True
    install_state["q3elite_update_ok"] = success

    if not success:
        check_install_finished()
        return

    # On first install PAK verification starts only AFTER the Q3Elite archive
    # has finished writing baseq3.
    if not install_state["base_done"] and not fdownload.isRunning():
        set_gui_checking("Checking...")
        fdownload.start()
        return

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

        set_gui_error("RETRY")
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


def post_update_offline(reason):
    install_state["offline"] = True
    print(f"[offline] {reason}")


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

    # Keep the merged launcher alive. This is now the persistent launcher.
    set_gui_ready(offline=install_state["offline"])


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

        # Minimal Step-3 overlay.  It intentionally sits above the existing
        # terminal view so Pause/Resume is testable before the GUI redesign.
        download_info = QtWidgets.QLabel(window)
        download_info.setText("No active network transfer")
        download_info.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignVCenter
            | QtCore.Qt.AlignmentFlag.AlignLeft
        )

        pause_button = QtWidgets.QPushButton("PAUSE", window)
        pause_button.clicked.connect(toggle_download_pause)

        download_timer = QtCore.QTimer(window)
        download_timer.timeout.connect(update_download_overlay)
        download_timer.start(250)

        # Reposition on resize without modifying gui_tools.py.
        _old_resize_event = window.resizeEvent
        def _launcher_resize_event(event):
            try:
                _old_resize_event(event)
            finally:
                position_download_controls()
        window.resizeEvent = _launcher_resize_event

        set_gui_checking("Checking...")

        # --------------------------------------------------------------------
        # WORKERS
        # --------------------------------------------------------------------

        launcher_self_update = LauncherSelfUpdate()
        q3elite_update = Q3EliteUpdate()
        fdownload = FDownload()
        q3elite_download = Q3EliteDownload()
        post_update = PostInstallUpdate()

        launcher_self_update.result_ready.connect(
            launcher_self_update_result
        )

        q3elite_update.result_ready.connect(
            q3elite_update_result
        )

        q3elite_update.offline.connect(
            q3elite_update_offline
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

        post_update.offline.connect(
            post_update_offline
        )

        # --------------------------------------------------------------------
        # START
        # --------------------------------------------------------------------

        # Self-update always resolves before any PAK/OSP/game update workers.
        # GitHub/network failure is non-fatal and falls through to normal startup.
        set_gui_checking("Checking Launcher...")
        launcher_self_update.start()

        sys.exit(
            app.exec()
        )

    except Exception as error:

        message = (
            f"{type(error).__name__}: {error}\n"
            "If the problem remains after restart, "
            "check the launcher log."
        )

        show_error(
            message,
            lambda: None
        )