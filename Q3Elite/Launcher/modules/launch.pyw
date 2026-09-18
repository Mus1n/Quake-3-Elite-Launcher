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
import q3elite_components as q3components

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
    """
    First-install worker — Step 18B.2.

    IMPORTANT:
    This no longer uses the legacy monolithic "Quake 3 Elite.zip".
    Fresh installation is delegated to q3elite_components.install_basic(),
    which uses the pCloud bulk ZIP accelerator and then performs the
    manifest/SHA-256 repair pass.
    """

    result_ready = pyqtSignal(bool)

    def run(self):
        try:
            print()
            print("========================================")
            print(" Installing Quake 3 Elite Basic")
            print("========================================")
            print()

            # Step 18B defaults:
            #   Basic            ON
            #   External Maps    OFF
            #   Music Playlist   OFF
            #   Autoexec Update  OFF
            #
            # Step 19 GUI will pass the user's checkbox selections here.
            q3components.install_basic(
                external_maps=False,
                music_playlist=False,
                autoexec_update=False,
                control=download_control,
                progress_callback=download_progress_callback,
            )

            # Strong postcondition for the first-install worker.
            if not q3elite_is_installed():
                raise RuntimeError(
                    "Basic installation finished, but Q3Elite/Engines "
                    "was not created."
                )

            self.result_ready.emit(True)

        except Exception as error:
            print()
            print(
                f"[error] Quake 3 Elite "
                f"installation failed: {error}"
            )
            print()
            self.result_ready.emit(False)


# ============================================================================
# COMPONENT MANAGER — STEP 18B.12
# ============================================================================

class ComponentWorker(QtCore.QThread):
    result_ready = pyqtSignal(bool, str)

    def __init__(self, action):
        super().__init__()
        self.action = action

    def run(self):
        try:
            download_control.reset()
            if self.action == "install-maps":
                q3components.install_maps(download_control, download_progress_callback)
            elif self.action == "remove-maps":
                q3components.uninstall_maps()
            elif self.action == "install-music":
                q3components.install_music(download_control, download_progress_callback)
            elif self.action == "remove-music":
                q3components.uninstall_music()
            elif self.action == "autoexec-on":
                q3components.set_autoexec_update(True)
            elif self.action == "autoexec-off":
                q3components.set_autoexec_update(False)
            else:
                raise RuntimeError(f"Unknown component action: {self.action}")
            self.result_ready.emit(True, self.action)
        except Exception as error:
            print(f"[component error] {error}")
            self.result_ready.emit(False, str(error))


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
            if not launcher_settings.get("auto_update_osp", True):
                print("[settings] Automatic OSP2-BE update is disabled.")
                self.result_ready.emit(True)
                return

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
            if (
                not launcher_settings.get("check_updates_on_startup", True)
                or not launcher_settings.get("auto_update_launcher", True)
            ):
                print("[settings] Automatic Launcher update check is disabled.")
                self.result_ready.emit("continue")
                return

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
# STEP 19 — LAUNCHER SETTINGS
# ============================================================================

SETTINGS_FILE = Path(os.environ.get("APPDATA", Path.home())) / "Quake 3 Elite" / "Launcher" / "settings.json"

DEFAULT_SETTINGS = {
    "auto_update_q3elite": True,
    "auto_update_launcher": True,
    "auto_update_osp": True,
    "check_updates_on_startup": True,
}


def load_launcher_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.is_file():
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULT_SETTINGS:
                    if key in raw:
                        data[key] = bool(raw[key])
    except Exception as error:
        print(f"[settings] Could not read settings: {error}")
    return data


def save_launcher_settings(data):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


launcher_settings = load_launcher_settings()


def read_local_q3elite_version():
    candidates = [
        GAME_ROOT / "Q3Elite" / "Version.json",
        GAME_ROOT / "Q3Elite" / "version.json",
    ]
    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return str(data.get("version", data.get("Version", "?")))
        except Exception:
            pass
    return "—"

# ============================================================================
# SHARED DOWNLOAD CONTROL
# ============================================================================

download_control = dt.DownloadControl()
component_worker = None

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


def position_component_button():
    margin = 18
    w, h = 145, 34
    components_button.setGeometry(margin, margin, w, h)


# ============================================================================
# MODERN GUI — STEP 19.1
# ============================================================================

def _disconnect_main_button():
    try:
        window.playButton.clicked.disconnect()
    except (TypeError, RuntimeError):
        pass


def set_status(title, detail="", kind="normal"):
    window.statusTitle.setText(title)
    window.statusDetail.setText(detail)
    window.statusCard.setProperty("state", kind)
    window.statusCard.style().unpolish(window.statusCard)
    window.statusCard.style().polish(window.statusCard)


def set_gui_checking(text="Checking..."):
    try:
        _disconnect_main_button()
        window.playButton.setText(text.upper())
        window.playButton.setEnabled(False)
        window.progressBar.setRange(0, 0)
        window.progressBar.show()
        window.pauseButton.show()
        set_status(text, "Please wait while Q3Elite is being checked.", "working")
    except Exception:
        pass


def set_gui_ready(offline=False):
    try:
        _disconnect_main_button()
        window.playButton.setText("▶   PLAY")
        window.playButton.setEnabled(True)
        window.playButton.clicked.connect(window.launch)
        window.progressBar.setRange(0, 100)
        window.progressBar.setValue(100)
        window.pauseButton.hide()

        version = read_local_q3elite_version()
        window.q3VersionValue.setText(version)
        window.installedValue.setText("Installed")
        if offline:
            set_status("Ready to play — offline", "Update servers are currently unavailable.", "warning")
        else:
            set_status("Q3Elite is up to date", "Everything is installed and verified.", "ok")
        print("Launcher ready" + (" - OFFLINE MODE." if offline else "."))
    except Exception as error:
        print(f"[warning] Could not update GUI READY state: {error}")


def set_gui_error(text="RETRY"):
    try:
        _disconnect_main_button()
        window.playButton.setText(text.upper())
        window.playButton.setEnabled(True)
        window.playButton.clicked.connect(start_local_check)
        window.progressBar.setRange(0, 100)
        window.progressBar.setValue(0)
        window.pauseButton.hide()
        set_status("Update required", "Repair or update failed. Press RETRY.", "critical")
    except Exception:
        pass


def update_download_overlay():
    name = _download_progress["name"]
    done = _download_progress["downloaded"]
    total = _download_progress["total"]
    speed = _download_progress["speed"]

    if not name:
        return

    if total:
        pct = max(0, min(100, int(done * 100 / total))) if total else 0
        window.progressBar.setRange(0, 100)
        window.progressBar.setValue(pct)
        window.downloadInfo.setText(
            f"{name}   •   {_format_bytes(done)} / {_format_bytes(total)}   •   {_format_bytes(speed)}/s"
        )
    else:
        window.progressBar.setRange(0, 0)
        window.downloadInfo.setText(
            f"{name}   •   {_format_bytes(done)}   •   {_format_bytes(speed)}/s"
        )


def toggle_download_pause():
    if download_control.paused:
        download_control.resume()
        window.pauseButton.setText("PAUSE")
        print()
        print("[download] Resumed.")
    else:
        download_control.pause()
        window.pauseButton.setText("RESUME")
        print()
        print("[download] Paused.")


def refresh_component_gui():
    state = q3components.load_state()
    window.mapsBox.blockSignals(True)
    window.musicBox.blockSignals(True)
    window.autoexecBox.blockSignals(True)
    window.mapsBox.setChecked(bool(state.get("external_maps", False)))
    window.musicBox.setChecked(bool(state.get("music_playlist", False)))
    window.autoexecBox.setChecked(bool(state.get("autoexec_update", False)))
    window.mapsBox.blockSignals(False)
    window.musicBox.blockSignals(False)
    window.autoexecBox.blockSignals(False)
    window.capture_component_baseline()


def start_component_action(action):
    global component_worker
    if component_worker is not None and component_worker.isRunning():
        return
    download_control.reset()
    window.set_navigation_enabled(False)
    set_gui_checking("Updating addons...")
    component_worker = ComponentWorker(action)
    component_worker.result_ready.connect(component_action_result)
    component_worker.start()


def _next_component_action():
    action = window.take_next_component_action()
    if action:
        start_component_action(action)
        return
    window.set_navigation_enabled(True)
    refresh_component_gui()
    set_gui_ready(offline=install_state["offline"])
    window.show_page("addons")
    window.set_addon_message("Changes applied successfully.")


def component_action_result(success, detail):
    if not success:
        window.set_navigation_enabled(True)
        set_gui_ready(offline=install_state["offline"])
        window.show_page("addons")
        window.set_addon_message("Operation failed: " + detail, error=True)
        return
    _next_component_action()


def apply_component_changes():
    window.prepare_component_actions()
    if not window.pending_component_actions:
        window.set_addon_message("No changes to apply.")
        return
    window.set_addon_message("")
    _next_component_action()


def apply_settings():
    global launcher_settings
    launcher_settings = {
        "auto_update_q3elite": window.autoQ3Box.isChecked(),
        "auto_update_launcher": window.autoLauncherBox.isChecked(),
        "auto_update_osp": window.autoOspBox.isChecked(),
        "check_updates_on_startup": window.checkStartupBox.isChecked(),
    }
    save_launcher_settings(launcher_settings)
    window.settingsMessage.setText("Settings saved.")


def refresh_updates():
    if (
        q3elite_update.isRunning()
        or fdownload.isRunning()
        or post_update.isRunning()
        or launcher_self_update.isRunning()
    ):
        return
    window.downloadInfo.setText("Checking for updates...")
    start_local_check()


def start_local_check():
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
        install_state["q3elite_done"] = False
        install_state["q3elite_ok"] = False
        set_gui_checking("Installing...")
        q3elite_download.start()


def start_game_checks():
    """Start Q3Elite/PAK/OSP pipeline after launcher self-update resolves."""
    if q3elite_is_installed() and not launcher_settings.get("auto_update_q3elite", True):
        print("[settings] Automatic Q3Elite update is disabled.")
        install_state["q3elite_done"] = True
        install_state["q3elite_ok"] = True
        install_state["q3elite_update_done"] = True
        install_state["q3elite_update_ok"] = True
        set_gui_checking("Checking PAKs...")
        fdownload.start()
        return

    if q3elite_is_installed():
        print()
        print("Existing Q3Elite installation detected.")
        print("Verifying local PAK files and checking OSP2-BE...")
        print()

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



class ModernLauncherWindow(QtWidgets.QMainWindow):
    """1368x768 frameless Q3Elite launcher. Backend stays in launch.pyw."""

    def __init__(self):
        QtWidgets.QMainWindow.__init__(self)

        self.setObjectName("launcherWindow")
        self.setWindowTitle("Quake 3 Elite Launcher")
        self.setFixedSize(1368, 768)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.Window
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._drag_pos = None
        self.pending_component_actions = []
        self._component_baseline = {}

        self.shell = QtWidgets.QFrame(self)
        self.shell.setObjectName("shell")
        self.shell.setGeometry(8, 8, 1352, 752)

        root = QtWidgets.QHBoxLayout(self.shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # LEFT RAIL ---------------------------------------------------------
        self.sidebar = QtWidgets.QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(224)
        side = QtWidgets.QVBoxLayout(self.sidebar)
        side.setContentsMargins(22, 24, 22, 22)
        side.setSpacing(10)

        brand = QtWidgets.QLabel("Q3<span style='color:#35d9ff'>ELITE</span>")
        brand.setObjectName("brand")
        brand.setTextFormat(QtCore.Qt.TextFormat.RichText)
        side.addWidget(brand)

        sub = QtWidgets.QLabel("QUAKE 3 ARENA COMPILATION")
        sub.setObjectName("brandSub")
        side.addWidget(sub)
        side.addSpacing(34)

        self.homeNav = self._nav_button("⌂   HOME", "home")
        self.addonsNav = self._nav_button("◇   INSTALL ADDONS", "addons")
        self.settingsNav = self._nav_button("⚙   SETTINGS", "settings")
        self.changelogNav = self._nav_button("≡   CHANGELOG", "changelog")
        for button in (self.homeNav, self.addonsNav, self.settingsNav, self.changelogNav):
            side.addWidget(button)

        side.addStretch(1)
        quote = QtWidgets.QLabel("Q3ELITE\n\nMORE THAN A GAME.\nA TIMELESS ARENA.")
        quote.setObjectName("sideQuote")
        side.addWidget(quote)
        root.addWidget(self.sidebar)

        # MAIN AREA ---------------------------------------------------------
        body = QtWidgets.QFrame()
        body.setObjectName("body")
        body_layout = QtWidgets.QVBoxLayout(body)
        body_layout.setContentsMargins(26, 18, 22, 20)
        body_layout.setSpacing(14)

        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("PLAY  /  IMPROVE  /  FRAG")
        title.setObjectName("topMotto")
        top.addWidget(title)
        top.addStretch(1)

        self.launcherVersion = QtWidgets.QLabel("Launcher v0.04")
        self.launcherVersion.setObjectName("versionLabel")
        top.addWidget(self.launcherVersion)

        self.minButton = QtWidgets.QPushButton("—")
        self.minButton.setObjectName("windowButton")
        self.minButton.setFixedSize(42, 36)
        self.minButton.clicked.connect(self.showMinimized)
        top.addWidget(self.minButton)

        self.closeButton = QtWidgets.QPushButton("×")
        self.closeButton.setObjectName("closeButton")
        self.closeButton.setFixedSize(42, 36)
        self.closeButton.clicked.connect(self.close)
        top.addWidget(self.closeButton)
        body_layout.addLayout(top)

        self.pages = QtWidgets.QStackedWidget()
        self.pages.setObjectName("pages")
        self.homePage = self._build_home()
        self.addonsPage = self._build_addons()
        self.settingsPage = self._build_settings()
        self.changelogPage = self._build_changelog()
        for page in (self.homePage, self.addonsPage, self.settingsPage, self.changelogPage):
            self.pages.addWidget(page)
        body_layout.addWidget(self.pages, 1)
        root.addWidget(body, 1)

        self.show_page("home")
        self._load_component_state_initial()
        self.load_settings_ui()

    def _nav_button(self, text, page):
        b = QtWidgets.QPushButton(text)
        b.setObjectName("navButton")
        b.setCheckable(True)
        b.setProperty("page", page)
        b.clicked.connect(lambda checked=False, p=page: self.show_page(p))
        return b

    def _card(self, name="card"):
        f = QtWidgets.QFrame()
        f.setObjectName(name)
        return f

    def _build_home(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        upper = QtWidgets.QHBoxLayout()
        upper.setSpacing(14)

        hero = self._card("heroCard")
        hero_l = QtWidgets.QVBoxLayout(hero)
        hero_l.setContentsMargins(30, 28, 30, 28)
        hero_l.addStretch(1)
        kicker = QtWidgets.QLabel("QUAKE 3 ARENA")
        kicker.setObjectName("heroKicker")
        hero_l.addWidget(kicker)
        hero_title = QtWidgets.QLabel("RELOADED FOR A NEW ERA")
        hero_title.setObjectName("heroTitle")
        hero_l.addWidget(hero_title)
        hero_desc = QtWidgets.QLabel(
            "Vulkan Engine   •   OSP2-BE   •   Custom HUD\\n"
            "Enhanced Graphics   •   Community Maps"
        )
        hero_desc.setObjectName("heroDescription")
        hero_l.addWidget(hero_desc)
        upper.addWidget(hero, 2)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(12)

        self.statusCard = self._card("statusCard")
        status_l = QtWidgets.QVBoxLayout(self.statusCard)
        status_l.setContentsMargins(20, 18, 20, 18)
        label = QtWidgets.QLabel("SYSTEM STATUS")
        label.setObjectName("sectionTitle")
        status_l.addWidget(label)
        self.statusTitle = QtWidgets.QLabel("Checking...")
        self.statusTitle.setObjectName("statusTitle")
        status_l.addWidget(self.statusTitle)
        self.statusDetail = QtWidgets.QLabel("Connecting to update services.")
        self.statusDetail.setWordWrap(True)
        self.statusDetail.setObjectName("muted")
        status_l.addWidget(self.statusDetail)

        versions = QtWidgets.QGridLayout()
        versions.addWidget(QtWidgets.QLabel("Q3Elite"), 0, 0)
        self.q3VersionValue = QtWidgets.QLabel(read_local_q3elite_version())
        versions.addWidget(self.q3VersionValue, 0, 1)
        versions.addWidget(QtWidgets.QLabel("Installation"), 1, 0)
        self.installedValue = QtWidgets.QLabel("Checking")
        versions.addWidget(self.installedValue, 1, 1)
        status_l.addLayout(versions)

        self.refreshButton = QtWidgets.QPushButton("↻  REFRESH")
        self.refreshButton.setObjectName("secondaryButton")
        self.refreshButton.clicked.connect(refresh_updates)
        status_l.addWidget(self.refreshButton)
        right.addWidget(self.statusCard)

        alert = self._card("updateAlert")
        alert_l = QtWidgets.QVBoxLayout(alert)
        alert_l.setContentsMargins(18, 14, 18, 14)
        self.alertTitle = QtWidgets.QLabel("UPDATE ALERTS")
        self.alertTitle.setObjectName("alertTitle")
        self.alertText = QtWidgets.QLabel("No update requires your attention.")
        self.alertText.setObjectName("muted")
        self.alertText.setWordWrap(True)
        alert_l.addWidget(self.alertTitle)
        alert_l.addWidget(self.alertText)
        right.addWidget(alert)
        upper.addLayout(right, 1)
        layout.addLayout(upper, 3)

        action = QtWidgets.QHBoxLayout()
        self.playButton = QtWidgets.QPushButton("CHECKING...")
        self.playButton.setObjectName("playButton")
        self.playButton.setMinimumHeight(74)
        action.addWidget(self.playButton, 2)

        progress_card = self._card("progressCard")
        p = QtWidgets.QVBoxLayout(progress_card)
        p.setContentsMargins(20, 13, 20, 13)
        row = QtWidgets.QHBoxLayout()
        self.downloadInfo = QtWidgets.QLabel("Preparing launcher...")
        self.downloadInfo.setObjectName("downloadInfo")
        row.addWidget(self.downloadInfo, 1)
        self.pauseButton = QtWidgets.QPushButton("PAUSE")
        self.pauseButton.setObjectName("smallButton")
        self.pauseButton.clicked.connect(toggle_download_pause)
        row.addWidget(self.pauseButton)
        p.addLayout(row)
        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setTextVisible(False)
        self.progressBar.setRange(0, 0)
        p.addWidget(self.progressBar)
        action.addWidget(progress_card, 3)
        layout.addLayout(action)

        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(12)
        for title, text, page_name in (
            ("EXTERNAL MAPS", "Explore more arenas", "addons"),
            ("MUSIC PLAYLIST", "Extended soundtrack", "addons"),
            ("SETTINGS", "Control updates", "settings"),
        ):
            card = self._card("featureCard")
            cl = QtWidgets.QVBoxLayout(card)
            t = QtWidgets.QLabel(title)
            t.setObjectName("featureTitle")
            d = QtWidgets.QLabel(text)
            d.setObjectName("muted")
            cl.addStretch(1)
            cl.addWidget(t)
            cl.addWidget(d)
            card.mousePressEvent = lambda event, p=page_name: self.show_page(p)
            tiles.addWidget(card)
        layout.addLayout(tiles, 1)
        return page

    def _addon_row(self, title, description, checkbox):
        card = self._card("addonCard")
        lay = QtWidgets.QHBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 16)
        text = QtWidgets.QVBoxLayout()
        t = QtWidgets.QLabel(title)
        t.setObjectName("addonTitle")
        d = QtWidgets.QLabel(description)
        d.setObjectName("muted")
        text.addWidget(t)
        text.addWidget(d)
        lay.addLayout(text, 1)
        lay.addWidget(checkbox)
        return card

    def _build_addons(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 4, 4, 4)
        title = QtWidgets.QLabel("INSTALL ADDONS")
        title.setObjectName("pageTitle")
        lay.addWidget(title)
        sub = QtWidgets.QLabel("Choose optional Q3Elite components. Nothing changes until APPLY CHANGES is pressed.")
        sub.setObjectName("muted")
        lay.addWidget(sub)
        lay.addSpacing(14)

        basic = self._card("addonCard")
        bl = QtWidgets.QHBoxLayout(basic)
        bt = QtWidgets.QVBoxLayout()
        x = QtWidgets.QLabel("Quake 3 Elite Basic")
        x.setObjectName("addonTitle")
        bt.addWidget(x)
        bt.addWidget(QtWidgets.QLabel("Required core installation"))
        bl.addLayout(bt, 1)
        installed = QtWidgets.QLabel("REQUIRED")
        installed.setObjectName("installedBadge")
        bl.addWidget(installed)
        lay.addWidget(basic)

        self.mapsBox = QtWidgets.QCheckBox()
        self.musicBox = QtWidgets.QCheckBox()
        self.autoexecBox = QtWidgets.QCheckBox()
        lay.addWidget(self._addon_row("External Maps", "Community map collection • cached ZIP retained", self.mapsBox))
        lay.addWidget(self._addon_row("Music Playlist", "Extended Q3Elite music collection • cached ZIP retained", self.musicBox))
        lay.addWidget(self._addon_row("Autoexec Update", "Keep distributed autoexec.cfg synchronized", self.autoexecBox))
        lay.addStretch(1)

        self.addonMessage = QtWidgets.QLabel("")
        self.addonMessage.setObjectName("message")
        lay.addWidget(self.addonMessage)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("CANCEL")
        cancel.setObjectName("secondaryButton")
        cancel.clicked.connect(refresh_component_gui)
        buttons.addWidget(cancel)
        apply = QtWidgets.QPushButton("APPLY CHANGES")
        apply.setObjectName("applyButton")
        apply.clicked.connect(apply_component_changes)
        buttons.addWidget(apply)
        lay.addLayout(buttons)
        return page

    def _build_settings(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 4, 4, 4)
        title = QtWidgets.QLabel("SETTINGS")
        title.setObjectName("pageTitle")
        lay.addWidget(title)
        lay.addWidget(QtWidgets.QLabel("Control automatic update behavior."))
        lay.addSpacing(18)

        card = self._card("settingsCard")
        c = QtWidgets.QVBoxLayout(card)
        c.setContentsMargins(22, 20, 22, 20)
        self.autoQ3Box = QtWidgets.QCheckBox("Automatically update Q3Elite")
        self.autoLauncherBox = QtWidgets.QCheckBox("Automatically update Launcher")
        self.autoOspBox = QtWidgets.QCheckBox("Automatically update OSP2-BE")
        self.checkStartupBox = QtWidgets.QCheckBox("Check for updates on startup")
        for box in (self.autoQ3Box, self.autoLauncherBox, self.autoOspBox, self.checkStartupBox):
            c.addWidget(box)
        lay.addWidget(card)

        cache = self._card("settingsCard")
        cc = QtWidgets.QVBoxLayout(cache)
        cache_title = QtWidgets.QLabel("DOWNLOAD CACHE")
        cache_title.setObjectName("sectionTitle")
        cc.addWidget(cache_title)
        cache_path = Path(os.environ.get("APPDATA", Path.home())) / "Quake 3 Elite" / "Launcher" / "cache"
        cp = QtWidgets.QLabel(str(cache_path))
        cp.setObjectName("muted")
        cp.setWordWrap(True)
        cc.addWidget(cp)
        lay.addWidget(cache)
        lay.addStretch(1)

        self.settingsMessage = QtWidgets.QLabel("")
        self.settingsMessage.setObjectName("message")
        lay.addWidget(self.settingsMessage)
        apply = QtWidgets.QPushButton("SAVE SETTINGS")
        apply.setObjectName("applyButton")
        apply.clicked.connect(apply_settings)
        lay.addWidget(apply, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        return page

    def _build_changelog(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 4, 4, 4)
        title = QtWidgets.QLabel("CHANGELOG")
        title.setObjectName("pageTitle")
        lay.addWidget(title)

        browser = QtWidgets.QTextBrowser()
        browser.setObjectName("changelogBrowser")
        browser.setHtml(
            "<h2>Q3Elite 1.2</h2>"
            "<p>Current installed release.</p>"
            "<ul>"
            "<li>Updated XQ3E Vulkan integration</li>"
            "<li>HUD and spectator improvements</li>"
            "<li>OSP2-BE integration</li>"
            "<li>Launcher component system</li>"
            "</ul>"
            "<p><i>Remote changelog support will use Version.json metadata.</i></p>"
        )
        lay.addWidget(browser, 1)
        return page

    def show_page(self, page):
        mapping = {
            "home": (self.homePage, self.homeNav),
            "addons": (self.addonsPage, self.addonsNav),
            "settings": (self.settingsPage, self.settingsNav),
            "changelog": (self.changelogPage, self.changelogNav),
        }
        widget, active = mapping[page]
        self.pages.setCurrentWidget(widget)
        for b in (self.homeNav, self.addonsNav, self.settingsNav, self.changelogNav):
            b.setChecked(b is active)
        if page == "addons":
            refresh_component_gui()

    def set_navigation_enabled(self, enabled):
        for b in (self.homeNav, self.addonsNav, self.settingsNav, self.changelogNav, self.refreshButton):
            b.setEnabled(enabled)

    def _load_component_state_initial(self):
        """Load component state during __init__ without touching global `window`."""
        state = q3components.load_state()
        self.mapsBox.setChecked(bool(state.get("external_maps", False)))
        self.musicBox.setChecked(bool(state.get("music_playlist", False)))
        self.autoexecBox.setChecked(bool(state.get("autoexec_update", False)))
        self.capture_component_baseline()

    def capture_component_baseline(self):
        self._component_baseline = {
            "maps": self.mapsBox.isChecked(),
            "music": self.musicBox.isChecked(),
            "autoexec": self.autoexecBox.isChecked(),
        }

    def prepare_component_actions(self):
        old = self._component_baseline
        new = {
            "maps": self.mapsBox.isChecked(),
            "music": self.musicBox.isChecked(),
            "autoexec": self.autoexecBox.isChecked(),
        }
        actions = []
        if old.get("maps") != new["maps"]:
            actions.append("install-maps" if new["maps"] else "remove-maps")
        if old.get("music") != new["music"]:
            actions.append("install-music" if new["music"] else "remove-music")
        if old.get("autoexec") != new["autoexec"]:
            actions.append("autoexec-on" if new["autoexec"] else "autoexec-off")
        self.pending_component_actions = actions

    def take_next_component_action(self):
        if not self.pending_component_actions:
            return None
        return self.pending_component_actions.pop(0)

    def set_addon_message(self, text, error=False):
        self.addonMessage.setText(text)
        self.addonMessage.setProperty("error", bool(error))
        self.addonMessage.style().unpolish(self.addonMessage)
        self.addonMessage.style().polish(self.addonMessage)

    def load_settings_ui(self):
        self.autoQ3Box.setChecked(launcher_settings.get("auto_update_q3elite", True))
        self.autoLauncherBox.setChecked(launcher_settings.get("auto_update_launcher", True))
        self.autoOspBox.setChecked(launcher_settings.get("auto_update_osp", True))
        self.checkStartupBox.setChecked(launcher_settings.get("check_updates_on_startup", True))

    def launch(self):
        """Launch Q3Elite using the existing launch.bat entry point."""
        launch_bat = LAUNCHER_DIR / "launch.bat"
        if not launch_bat.is_file():
            self.qerror(f"Could not find launcher entry point:\n{launch_bat}")
            return
        try:
            os.startfile(str(launch_bat))
        except Exception as error:
            self.qerror(f"Could not start Q3Elite:\n{error}")

    def qerror(self, text):
        QtWidgets.QMessageBox.critical(self, "Q3Elite Launcher", str(text))

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and event.position().y() < 70:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


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

    # On first install PAK verification starts only AFTER Basic has
    # finished writing Q3Elite/baseq3.
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


def main():
    global app, window, download_timer
    global launcher_self_update, q3elite_update, fdownload, q3elite_download, post_update

    try:
        app = QApplication(sys.argv)

        # External stylesheet is now the single source of visual styling.
        style_path = LAUNCHER_DIR / "ui" / "style.css"
        if style_path.is_file():
            app.setStyleSheet(style_path.read_text(encoding="utf-8"))
        else:
            print(f"[warning] UI stylesheet not found: {style_path}")

        # Keep existing font support for the backend terminal.
        font_path = LAUNCHER_DIR / "ui" / "FiraCode-Regular.ttf"
        family = "Arial"
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id != -1:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    family = families[0]

        window = ModernLauncherWindow()

        window.show()

        download_timer = QtCore.QTimer(window)
        download_timer.timeout.connect(update_download_overlay)
        download_timer.start(200)

        # Create every worker BEFORE starting the first one.
        # A very fast self-update check can emit "continue" immediately, and
        # start_game_checks() expects q3elite_download/q3elite_update/fdownload
        # to already exist.
        q3elite_update = Q3EliteUpdate()
        fdownload = FDownload()
        q3elite_download = Q3EliteDownload()
        post_update = PostInstallUpdate()
        launcher_self_update = LauncherSelfUpdate()

        launcher_self_update.result_ready.connect(launcher_self_update_result)
        q3elite_update.result_ready.connect(q3elite_update_result)
        q3elite_update.offline.connect(q3elite_update_offline)
        fdownload.result_ready.connect(base_install_result)
        q3elite_download.result_ready.connect(q3elite_install_result)
        post_update.result_ready.connect(post_update_result)
        post_update.offline.connect(post_update_offline)

        set_gui_checking("Checking Launcher...")

        # Start only after QApplication enters its event loop. This removes the
        # initialization race between the self-update signal and main().
        QtCore.QTimer.singleShot(0, launcher_self_update.start)

        sys.exit(app.exec())

    except Exception as error:
        message = (
            f"{type(error).__name__}: {error}\n"
            "If the problem remains after restart, check the launcher log."
        )
        try:
            show_error(message, lambda: None)
        except Exception:
            print(message)
            raise


# Explicit entry point for the standalone .pyw launcher.
if __name__ == "__main__":
    main()
