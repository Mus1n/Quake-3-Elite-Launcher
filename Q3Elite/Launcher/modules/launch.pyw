import os
import sys
import shutil
import json
import re
import urllib.parse
import urllib.request
import zipfile

from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets, QtNetwork

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    TELEGRAM_WEBENGINE_AVAILABLE = True
except Exception:
    QWebEngineView = None
    TELEGRAM_WEBENGINE_AVAILABLE = False
from PyQt6.QtCore import QLockFile, pyqtSignal
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFontDatabase

try:
    import qtawesome as qta
except Exception:
    qta = None


# ============================================================================
# PATHS
# ============================================================================

LAUNCHER_DIR = Path(__file__).resolve().parent.parent
GAME_ROOT = LAUNCHER_DIR.parent.parent
ASSETS_DIR = LAUNCHER_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
IMAGES_DIR = ASSETS_DIR / "images"
CACHE_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Quake 3 Elite" / "Cache"
BACKGROUND_IMAGE = IMAGES_DIR / "background.png"
APP_ICON_ICO = ICONS_DIR / "favicon.ico"
APP_ICON_PNG = ICONS_DIR / "favicon.png"
VULKAN_EXE = GAME_ROOT / "Q3Elite" / "Engines" / "XQ3E_Vulkan.x64.exe"
RESHADE_SOURCE = GAME_ROOT / "Q3Elite" / "ReShade" / "Program"
RESHADE_DEST = Path(os.environ.get("ProgramData", r"C:\\ProgramData")) / "ReShade"

os.chdir(LAUNCHER_DIR)


# ============================================================================
# Q3ELITE MODULES
# ============================================================================

import download_tools as dt
import q3elite_components as q3components
import server_monitor

from osp_updater import check_and_update as check_and_update_osp
from q3elite_updater import check_and_update as check_and_update_q3elite
from pak_verifier import verify_paks
from launcher_updater import check_for_update as check_launcher_update, stage_update as stage_launcher_update, launch_apply_helper
from base_methods import *


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
    """A usable installation requires both the Vulkan engine and the external OSP marker."""
    engine = GAME_ROOT / "Q3Elite" / "Engines" / "XQ3E_Vulkan.x64.exe"
    osp_marker = GAME_ROOT / "baseq3" / "mods" / "osp" / "zzzz-Mus1n-REMASTERED.pk3dir"
    print(f"Q3Elite engine marker: {engine}")
    print(f"Q3Elite OSP marker:    {osp_marker}")
    return engine.is_file() and osp_marker.exists()


def config_editor_available():
    """Only expose the editor after Basic has actually installed both configs."""
    osp = GAME_ROOT / "baseq3" / "mods" / "osp"
    return (
        q3elite_is_installed()
        and (osp / "autoexec.cfg").is_file()
        and (osp / "UserConfig.cfg").is_file()
    )


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

    def __init__(self):
        super().__init__()
        self.external_maps = False
        self.music_playlist = False

    def set_options(self, external_maps=False, music_playlist=False):
        self.external_maps = bool(external_maps)
        self.music_playlist = bool(music_playlist)

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
            # Always install/extract BASIC first. Optional components must not
            # influence Basic's manifest repair pass.
            q3components.install_basic(
                external_maps=False,
                music_playlist=False,
                autoexec_update=False,
                control=download_control,
                progress_callback=download_progress_callback,
            )

            # Only after Basic is physically installed may optional components run.
            # Maps use their dedicated pCloud ZIP installer instead of falling back
            # to hundreds of individual manifest downloads.
            if self.external_maps:
                print("\nBasic installed. Installing External Maps...")
                q3components.install_maps(
                    download_control,
                    download_progress_callback,
                )

            if self.music_playlist:
                print("\nBasic installed. Installing Music Playlist...")
                q3components.install_music(
                    download_control,
                    download_progress_callback,
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
            elif self.action == "update-autoexec":
                q3components.set_autoexec_update(True)
                # install_basic/update pipeline consumes this state; keep it one-shot in UI.
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

            CACHE_DIR.mkdir(parents=True, exist_ok=True)

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

            if not launcher_settings.get("auto_update_launcher", True):
                print("[settings] Launcher update found; automatic installation is disabled.")
                self.result_ready.emit("continue")
                return

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
# WINDOWS / RESHADE / LAUNCHER METADATA
# ============================================================================

def _is_admin():
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _reshade_registry_paths():
    return (
        (r"SOFTWARE\Khronos\Vulkan\ImplicitLayers", str(RESHADE_DEST / "ReShade64.json"), 0),
        (r"SOFTWARE\Khronos\Vulkan\ImplicitLayers", str(RESHADE_DEST / "ReShade32.json"), 32),
    )


def _set_reshade_registry_direct(enabled):
    import winreg
    entries = _reshade_registry_paths()
    for key_path, value_name, view in entries:
        access = winreg.KEY_SET_VALUE
        access |= winreg.KEY_WOW64_32KEY if view == 32 else winreg.KEY_WOW64_64KEY
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0, access)
            with key:
                if enabled:
                    winreg.SetValueEx(key, value_name, 0, winreg.REG_DWORD, 0)
                else:
                    try:
                        winreg.DeleteValue(key, value_name)
                    except FileNotFoundError:
                        pass
        except OSError:
            if not enabled:
                continue
            raise


def _run_reshade_registry_helper(enabled):
    """Elevate only the two HKLM registry operations; return the real child exit code."""
    import ctypes
    import subprocess
    import tempfile

    value64 = str(RESHADE_DEST / "ReShade64.json")
    value32 = str(RESHADE_DEST / "ReShade32.json")
    if enabled:
        commands = [
            f'reg add "HKLM\\SOFTWARE\\Khronos\\Vulkan\\ImplicitLayers" /v "{value64}" /t REG_DWORD /d 0 /f',
            f'reg add "HKLM\\SOFTWARE\\WOW6432Node\\Khronos\\Vulkan\\ImplicitLayers" /v "{value32}" /t REG_DWORD /d 0 /f',
        ]
    else:
        commands = [
            f'reg delete "HKLM\\SOFTWARE\\Khronos\\Vulkan\\ImplicitLayers" /v "{value64}" /f 2>nul',
            f'reg delete "HKLM\\SOFTWARE\\WOW6432Node\\Khronos\\Vulkan\\ImplicitLayers" /v "{value32}" /f 2>nul',
        ]

    helper = Path(tempfile.gettempdir()) / "Q3Elite_ReShade_Admin.cmd"
    helper.write_text(
        "@echo off\\r\\n" + "\\r\\n".join(commands) + "\\r\\nexit /b 0\\r\\n",
        encoding="ascii",
    )

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIconOrMonitor", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = "cmd.exe"
    sei.lpParameters = f'/c "{helper}"'
    sei.nShow = SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        raise RuntimeError("Administrator permission was cancelled.")

    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    try:
        helper.unlink(missing_ok=True)
    except Exception:
        pass
    if code.value != 0:
        raise RuntimeError(f"Administrator helper failed with exit code {code.value}.")


def reshade_layer_enabled():
    """Require both 64-bit and 32-bit ReShade Vulkan implicit-layer entries."""
    if os.name != "nt":
        return False
    import winreg
    checks = [
        (r"SOFTWARE\Khronos\Vulkan\ImplicitLayers", str(RESHADE_DEST / "ReShade64.json"), winreg.KEY_WOW64_64KEY),
        (r"SOFTWARE\Khronos\Vulkan\ImplicitLayers", str(RESHADE_DEST / "ReShade32.json"), winreg.KEY_WOW64_32KEY),
    ]
    for key_path, value_name, view in checks:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ | view) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
                if int(value) != 0:
                    return False
        except OSError:
            return False
    return True


def configure_reshade_vulkan(enabled=True, install_files=False):
    """Install ReShade files when needed and toggle the Vulkan layer registry state."""
    if os.name != "nt":
        raise RuntimeError("ReShade Vulkan layer management is only available on Windows.")

    if install_files:
        RESHADE_DEST.mkdir(parents=True, exist_ok=True)
        if RESHADE_SOURCE.is_dir():
            for source in RESHADE_SOURCE.rglob("*"):
                relative = source.relative_to(RESHADE_SOURCE)
                destination = RESHADE_DEST / relative
                if source.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)

        ini = RESHADE_DEST / "ReShadeApps.ini"
        game = str(VULKAN_EXE.resolve())
        existing = ini.read_text(encoding="utf-8", errors="ignore") if ini.is_file() else ""
        if game.lower() not in existing.lower():
            if not existing.strip():
                existing = "[GENERAL]\\nApps=" + game + "\\n"
            elif re.search(r"(?mi)^Apps=.*$", existing):
                existing = re.sub(
                    r"(?mi)^Apps=(.*)$",
                    lambda m: "Apps=" + m.group(1).rstrip(";") + ";" + game,
                    existing,
                    count=1,
                )
            else:
                existing = existing.rstrip() + "\\nApps=" + game + "\\n"
            ini.write_text(existing, encoding="utf-8")

    if _is_admin():
        _set_reshade_registry_direct(enabled)
    else:
        _run_reshade_registry_helper(enabled)

    # Verify the actual state after the operation.
    actual = reshade_layer_enabled()
    if bool(actual) != bool(enabled):
        raise RuntimeError("Registry operation completed, but the Vulkan layer state did not change.")


def set_start_with_windows(enabled):
    """Use HKCU Run: no administrator rights required."""
    if os.name != "nt":
        return
    import winreg
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    value_name = "Q3Elite Launcher"
    # Prefer the launcher executable when frozen; otherwise use pythonw + this script.
    if getattr(sys, "frozen", False):
        command = f'"{Path(sys.executable).resolve()}" --autostart'
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = f'"{pythonw}" "{Path(__file__).resolve()}" --autostart'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass


def launcher_version_file():
    # Keep this aligned with launcher_updater.py. Launcher_Version.json is the
    # authoritative local metadata written by the detached update helper.
    candidates = [
        LAUNCHER_DIR / "Launcher_Version.json",
        LAUNCHER_DIR / "Launcher_version.json",
        LAUNCHER_DIR / "Updater_Version.json",
        LAUNCHER_DIR / "updater_Version.json",
        LAUNCHER_DIR / "Version.json",
        LAUNCHER_DIR / "version.json",
        LAUNCHER_DIR / "version.txt",
    ]
    return next((p for p in candidates if p.is_file()), None)


def read_launcher_metadata():
    """Read launcher Version.json while accepting the release formats used by the updater."""
    path = launcher_version_file()
    if path is None:
        return {"version": "—", "releases": []}
    try:
        if path.suffix.lower() == ".txt":
            return {"version": path.read_text(encoding="utf-8").strip(), "releases": []}

        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"version": "—", "releases": []}

        version = str(raw.get("version", raw.get("Version", raw.get("launcher_version", "—"))))
        releases = raw.get("releases", raw.get("changelog", raw.get("history", [])))

        if isinstance(releases, dict):
            releases = [
                dict(v if isinstance(v, dict) else {"changes": v}, version=k)
                for k, v in releases.items()
            ]
        elif isinstance(releases, str):
            releases = [{"version": version, "changes": [releases]}]
        elif isinstance(releases, list):
            # Common compact syntax:
            # "changelog": ["Fixed X", "Added Y"]
            if releases and all(not isinstance(item, dict) for item in releases):
                releases = [{
                    "version": version,
                    "date": raw.get("date", raw.get("release_date", raw.get("published_at", ""))),
                    "changes": [str(item) for item in releases],
                }]
        else:
            releases = []

        # Some Launcher_Version.json files keep notes directly at the top level.
        if not releases:
            changes = raw.get(
                "changes",
                raw.get("notes", raw.get("items", raw.get("change_log", raw.get("release_notes", []))))
            )
            if isinstance(changes, str):
                changes = [changes]
            if isinstance(changes, list) and changes:
                releases = [{
                    "version": version,
                    "date": raw.get("date", raw.get("release_date", "")),
                    "changes": changes,
                }]

        return {"version": version, "releases": releases, "raw": raw}
    except Exception as error:
        print(f"[metadata] Could not read launcher metadata: {error}")
        return {"version": "—", "releases": []}



CHANGELOG_FILE = LAUNCHER_DIR / "Changelog.json"


def _html_escape(value):
    import html
    return html.escape(str(value), quote=True)


def _url_attr(value):
    return _html_escape(str(value).strip())


def _youtube_embed_url(url):
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlparse(value)
        host = parsed.netloc.casefold()
        video_id = ""
        if "youtu.be" in host:
            video_id = parsed.path.strip("/").split("/")[0]
        elif "youtube.com" in host:
            if parsed.path == "/watch":
                video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith("/embed/"):
                video_id = parsed.path.split("/embed/", 1)[1].split("/")[0]
            elif parsed.path.startswith("/shorts/"):
                video_id = parsed.path.split("/shorts/", 1)[1].split("/")[0]
        if video_id:
            return f"https://www.youtube.com/embed/{urllib.parse.quote(video_id)}"
    except Exception:
        pass
    return value



def _telegram_hq_image(media_page):
    try:
        req = urllib.request.Request(
            str(media_page),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            page = response.read().decode("utf-8", errors="replace")
        for pattern in (
            r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)""",
            r"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']""",
        ):
            m = re.search(pattern, page, flags=re.I | re.S)
            if m:
                u = _html.unescape(m.group(1)).replace("&amp;", "&")
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith("http"):
                    return u
    except Exception as error:
        print(f"[changelog] Telegram HQ image: {error}")
    return ""



def _telegram_reupload_link(html_block):
    """Find a YouTube URL when the Telegram post labels it as a reupload."""
    plain = re.sub(r"<br\\s*/?>", "\\n", html_block, flags=re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = _html.unescape(plain).casefold()
    if "reupload" not in plain:
        return ""

    href_pattern = r'''href\\s*=\\s*["']([^"']+)["']'''
    for href in re.findall(href_pattern, html_block, flags=re.I):
        href = _html.unescape(href).replace("&amp;", "&").strip()
        decoded = urllib.parse.unquote(href)
        for candidate in (href, decoded):
            m = re.search(
                r'''https?://(?:www\\.)?(?:youtube\\.com/watch\\?[^\\s"'<>]*v=[A-Za-z0-9_-]{6,}|youtu\\.be/[A-Za-z0-9_-]{6,})''',
                candidate,
                flags=re.I,
            )
            if m:
                return m.group(0)

    decoded_block = urllib.parse.unquote(_html.unescape(html_block))
    m = re.search(
        r'''https?://(?:www\\.)?(?:youtube\\.com/watch\\?[^\\s"'<>]*v=[A-Za-z0-9_-]{6,}|youtu\\.be/[A-Za-z0-9_-]{6,})''',
        decoded_block,
        flags=re.I,
    )
    return m.group(0) if m else ""


def _telegram_public_post_data(url):
    """Best-effort extraction of text + media from a Telegram public post."""
    value = str(url or "").strip()
    if not value or "t.me/" not in value:
        return {}

    fetch_url = value
    match = re.match(r"https?://t\.me/(?!s/)([^/?#]+)/(\d+)", value)
    if match:
        fetch_url = f"https://t.me/s/{match.group(1)}/{match.group(2)}"

    try:
        req = urllib.request.Request(
            fetch_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            page = response.read().decode("utf-8", errors="replace")

        import html as _html
        result = {}

        # Narrow to the requested message when possible, so neighbouring /s/
        # posts do not leak their media into this release.
        post_id = ""
        m = re.search(r"/(\d+)(?:[/?#]|$)", value)
        if m:
            post_id = m.group(1)
        block = page
        if post_id:
            marker = f'data-post="'
            pos = page.find(f'/{post_id}"')
            if pos >= 0:
                begin = page.rfind('<div class="tgme_widget_message', 0, pos)
                next_begin = page.find('<div class="tgme_widget_message', pos + 1)
                if begin >= 0:
                    block = page[begin: next_begin if next_begin >= 0 else len(page)]

        text_match = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block, flags=re.I | re.S
        )
        if text_match:
            fragment = text_match.group(1)
            fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
            fragment = re.sub(r"<[^>]+>", "", fragment)
            result["text"] = _html.unescape(fragment).strip()

        reupload = _telegram_reupload_link(block)
        if reupload:
            result["video_reupload"] = reupload

        # Telegram screenshots / albums.
        # Parse ONLY photo_wrap anchors from the target message. This excludes
        # avatar images and animated custom-emoji WEBM assets.
        photo_pages = []
        photos = []
        for tag in re.findall(r'<a\b[^>]*>', block, flags=re.I | re.S):
            class_match = re.search(
                r'class\s*=\s*(["\'])(.*?)\1', tag, flags=re.I | re.S
            )
            if not class_match:
                continue
            classes = class_match.group(2)
            if "tgme_widget_message_photo_wrap" not in classes:
                continue

            hm = re.search(r"""href\s*=\s*["']([^"']+)""", tag, flags=re.I | re.S)
            if hm:
                href = _html.unescape(hm.group(1)).replace("&amp;", "&")
                if href.startswith("//"):
                    href = "https:" + href
                if href.startswith("http") and href not in photo_pages:
                    photo_pages.append(href)

            style_match = re.search(
                r'style\s*=\s*(["\'])(.*?)\1', tag, flags=re.I | re.S
            )
            if not style_match:
                continue

            style = _html.unescape(style_match.group(2))
            bg = re.search(
                r'background-image\s*:\s*url\(\s*(?:&quot;|["\']|&#39;)?'
                r'(.*?)'
                r'(?:&quot;|["\']|&#39;)?\s*\)',
                style,
                flags=re.I | re.S,
            )
            if not bg:
                continue

            media = _html.unescape(bg.group(1)).strip(" '\"")
            media = media.replace("&amp;", "&")
            if media.startswith("//"):
                media = "https:" + media

            low = media.casefold()
            if (
                media.startswith("http")
                and not low.endswith((".webm", ".mp4", ".tgs"))
                and media not in photos
            ):
                photos.append(media)

        if photos:
            hq_photos = []
            for media_page in photo_pages:
                hq = _telegram_hq_image(media_page)
                if hq and hq not in hq_photos:
                    hq_photos.append(hq)
            final_photos = hq_photos if hq_photos else photos
            result["images"] = final_photos
            result["image"] = final_photos[0]

        # Detect real Telegram message video containers. Telegram currently
        # uses several class variants such as *_video_player.
        video_marker = re.search(
            r'<[^>]+class=["\'][^"\']*tgme_widget_message_video[^"\']*["\'][^>]*>',
            block, flags=re.I | re.S
        )
        if video_marker:
            result["has_video"] = True
            video_area = block[video_marker.start():]
            pm = re.search(
                r'<video[^>]+poster=["\']([^"\']+)["\']',
                video_area, flags=re.I | re.S
            )
            if not pm:
                pm = re.search(
                    r'background-image\s*:\s*url\((?:["\']?)(.*?)(?:["\']?)\)',
                    video_area[:12000], flags=re.I | re.S
                )
            if pm:
                poster = _html.unescape(pm.group(1)).replace("&amp;", "&")
                if poster.startswith("//"):
                    poster = "https:" + poster
                result["video_poster"] = poster


        # OG fallbacks.
        patterns = {
            "text": r'<meta\s+property="og:description"\s+content="([^"]*)"',
            "title": r'<meta\s+property="og:title"\s+content="([^"]*)"',
        }
        for key, pattern in patterns.items():
            if result.get(key):
                continue
            found = re.search(pattern, page, flags=re.I)
            if found:
                result[key] = _html.unescape(found.group(1)).strip()

        emoji_ids = re.findall(r'data-document-id="(\d+)"', block, flags=re.I)
        if emoji_ids:
            result["custom_emoji_ids"] = list(dict.fromkeys(emoji_ids))

        return result
    except Exception as error:
        print(f"[changelog] Telegram source unavailable: {error}")
        return {}


def read_changelog_entries():
    """
    Changelog.json is the preferred rich feed. It may be:
      - one JSON array of entries (recommended), or
      - {"releases": [...]}.

    Launcher_Version.json remains a fallback for backwards compatibility.
    """
    if CHANGELOG_FILE.is_file():
        try:
            raw = json.loads(CHANGELOG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw = raw.get("releases", raw.get("changelog", [raw]))
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)]
        except Exception as error:
            print(f"[changelog] Could not read {CHANGELOG_FILE.name}: {error}")

    # Backwards-compatible launcher-only metadata.
    return sorted_launcher_releases()


def _rich_changelog_html(entries):
    parts = ['<div class="feed">']
    for release in entries:
        update_type = str(release.get("type", "Update"))
        version = str(release.get("version", "?"))
        date = str(release.get("date", release.get("release_date", "")))

        # Telegram can fill missing text/image metadata.
        source_url = str(
            release.get("telegram", release.get("text_source", ""))
        ).strip()
        tg = _telegram_public_post_data(source_url) if source_url else {}
        self.telegram_video_reupload = str(tg.get("video_reupload", "") or "").strip()

        custom_text = release.get("text", "")
        if not custom_text:
            custom_text = tg.get("text", "")

        images = release.get("image", release.get("images", []))
        if isinstance(images, str):
            images = [images]
        if not isinstance(images, list):
            images = []
        if not images and tg.get("image"):
            images = [tg["image"]]

        # If a Telegram post itself is supplied as an image source, resolve its
        # OpenGraph preview image instead of trying to display the HTML page.
        resolved_images = []
        for image in images:
            image = str(image).strip()
            if "t.me/" in image:
                resolved = _telegram_public_post_data(image).get("image", "")
                if resolved:
                    resolved_images.append(resolved)
            elif image:
                resolved_images.append(image)

        parts.append('<div class="release">')
        parts.append(
            f'<h2>{_html_escape(update_type)} '
            f'<span class="version">v{_html_escape(version)}</span></h2>'
        )
        if date:
            parts.append(f'<p class="date">{_html_escape(date)}</p>')

        if custom_text:
            # Newlines are intentional; Unicode emoji are preserved by Qt.
            safe_text = _html_escape(custom_text).replace("\n", "<br>")
            parts.append(f'<p class="releaseText">{safe_text}</p>')

        changes = release.get(
            "changelog",
            release.get("changes", release.get("items", release.get("notes", [])))
        )
        if isinstance(changes, str):
            changes = [changes]
        if isinstance(changes, list) and changes:
            parts.append("<ul>")
            for item in changes:
                if isinstance(item, dict):
                    label = item.get("text", item.get("label", item.get("title", "")))
                    link = item.get("link", item.get("url", ""))
                    if link:
                        parts.append(
                            f'<li><a href="{_url_attr(link)}">{_html_escape(label or link)}</a></li>'
                        )
                    elif label:
                        parts.append(f"<li>{_html_escape(label)}</li>")
                else:
                    parts.append(f"<li>{_html_escape(item)}</li>")
            parts.append("</ul>")

        for image in resolved_images:
            parts.append(
                f'<p><a href="{_url_attr(image)}">'
                f'<img src="{_url_attr(image)}" width="560"></a></p>'
            )

        video = release.get("video", "")
        if video:
            # QTextBrowser cannot host a real video player. Show a clean
            # clickable media action; YouTube opens in the default browser.
            parts.append(
                f'<p><a class="mediaLink" href="{_url_attr(video)}">▶ WATCH VIDEO</a></p>'
            )

        link = release.get("link", "")
        if link:
            parts.append(
                f'<p><a class="sourceLink" href="{_url_attr(link)}">OPEN SOURCE ↗</a></p>'
            )
        elif source_url:
            parts.append(
                f'<p><a class="sourceLink" href="{_url_attr(source_url)}">OPEN TELEGRAM POST ↗</a></p>'
            )

        parts.append("</div><hr>")
    parts.append("</div>")
    return "".join(parts)


def sorted_launcher_releases():
    from datetime import datetime, timedelta
    releases = read_launcher_metadata().get("releases", [])

    def key(item):
        if not isinstance(item, dict):
            return datetime.min
        value = str(item.get("date", item.get("release_date", item.get("published_at", ""))))
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(value[:10], fmt)
            except ValueError:
                pass
        return datetime.min

    return sorted(
        [x for x in releases if isinstance(x, dict)],
        key=key,
        reverse=True,
    )


# ============================================================================
# STEP 19 — LAUNCHER SETTINGS
# ============================================================================

SETTINGS_FILE = Path(os.environ.get("APPDATA", Path.home())) / "Quake 3 Elite" / "Launcher" / "settings.json"

DEFAULT_SETTINGS = {
    "auto_update_q3elite": True,
    "auto_update_launcher": True,
    "auto_update_osp": True,
    "start_with_windows": False,
    "minimize_to_tray": False,
    "show_changelog_media": False,
    "cleanup_screenshots_days": 0,
    "cleanup_demos_days": 0,
    "pause_server_refresh_unfocused": False,
}



CLEANUP_SCREENSHOT_DIRS = (
    Path("baseq3") / "mods" / "osp" / "screenshots",
    Path("Q3Elite") / "Screenshots",
)
CLEANUP_DEMO_DIRS = (
    Path("baseq3") / "mods" / "osp" / "demos",
)

def cleanup_old_files(relative_dirs, days):
    """Delete files older than N days from explicitly allowed folders only."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0

    cutoff = time.time() - (days * 86400)
    removed = 0
    for relative_dir in relative_dirs:
        folder = GAME_ROOT / relative_dir
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except (OSError, PermissionError):
                continue
    return removed


def load_launcher_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.is_file():
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULT_SETTINGS:
                    if key in raw:
                        if key in ("cleanup_screenshots_days", "cleanup_demos_days"):
                            try:
                                data[key] = max(0, int(raw[key]))
                            except (TypeError, ValueError):
                                data[key] = 0
                        else:
                            data[key] = bool(raw[key])

                # Migrate the short-lived combined cleanup setting from v0.09.2.
                if "cleanup_media_days" in raw:
                    try:
                        legacy_days = max(0, int(raw["cleanup_media_days"]))
                    except (TypeError, ValueError):
                        legacy_days = 0
                    if "cleanup_screenshots_days" not in raw:
                        data["cleanup_screenshots_days"] = legacy_days
                    if "cleanup_demos_days" not in raw:
                        data["cleanup_demos_days"] = legacy_days

                # Migrate the old screenshot-only preference once.
                if "show_changelog_media" not in raw and "show_changelog_screenshots" in raw:
                    data["show_changelog_media"] = bool(raw["show_changelog_screenshots"])
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


try:
    _screenshots_cleaned = cleanup_old_files(
        CLEANUP_SCREENSHOT_DIRS,
        launcher_settings.get("cleanup_screenshots_days", 0),
    )
    _demos_cleaned = cleanup_old_files(
        CLEANUP_DEMO_DIRS,
        launcher_settings.get("cleanup_demos_days", 0),
    )
    if _screenshots_cleaned:
        print(f"[cleanup] Removed {_screenshots_cleaned} old screenshot file(s).")
    if _demos_cleaned:
        print(f"[cleanup] Removed {_demos_cleaned} old demo file(s).")
except Exception as _cleanup_error:
    print(f"[cleanup] Failed: {_cleanup_error}")

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
    if not q3elite_is_installed():
        prepare_first_install()
        return
    try:
        _disconnect_main_button()
        window.playButton.setText("▶   PLAY")
        window.playButton.setEnabled(True)
        window.playButton.clicked.connect(window.launch)
        window.progressBar.setRange(0, 100)
        window.progressBar.setValue(100)
        window.pauseButton.hide()
        if hasattr(window, "firstInstallCard"):
            window.firstInstallCard.hide()
        if hasattr(window, "autoexecUpdateButton"):
            window.autoexecUpdateButton.setEnabled(True)
        if hasattr(window, "applyAddonsButton"):
            window.applyAddonsButton.setEnabled(True)
        if hasattr(window, "configEditorButton"):
            window.configEditorButton.setEnabled(config_editor_available())

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
    installed = q3elite_is_installed()
    state = q3components.load_state()

    window.mapsBox.blockSignals(True)
    window.musicBox.blockSignals(True)
    window.mapsBox.setChecked(bool(state.get("external_maps", False)) if installed else False)
    window.musicBox.setChecked(bool(state.get("music_playlist", False)) if installed else False)
    window.mapsBox.blockSignals(False)
    window.musicBox.blockSignals(False)

    window.mapsBox.setEnabled(installed)
    window.musicBox.setEnabled(installed)
    if hasattr(window, "applyAddonsButton"):
        window.applyAddonsButton.setEnabled(installed)
    if hasattr(window, "autoexecUpdateButton"):
        window.autoexecUpdateButton.setEnabled(installed)

    window.capture_component_baseline()


def start_component_action(action):
    global component_worker
    if not q3elite_is_installed():
        window.set_addon_message("Install Quake 3 Elite first.", error=True)
        return
    if component_worker is not None and component_worker.isRunning():
        return

    download_control.reset()
    window.set_addon_message("")

    # Addon downloads must behave like the first installation: the transfer
    # continues in the background while Home / Addons / Settings / Changelog
    # remain navigable.
    window.set_navigation_enabled(True)

    # Addon installation/removal uses the same central action/progress area as
    # first installation and updates. Do not leave the user on a locked submenu.
    window.show_page("home")

    labels = {
        "install-maps": "Installing External Maps...",
        "remove-maps": "Removing External Maps...",
        "install-music": "Installing Music Playlist...",
        "remove-music": "Removing Music Playlist...",
        "update-autoexec": "Updating Autoexec...",
    }
    set_gui_checking(labels.get(action, "Updating addons..."))

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
    window.show_page("home")
    window.set_addon_message("Changes applied successfully.")
    set_status("Addons updated", "Selected addon changes were applied successfully.", "ok")


def component_action_result(success, detail):
    if not success:
        window.set_navigation_enabled(True)
        refresh_component_gui()
        set_gui_ready(offline=install_state["offline"])
        window.show_page("home")
        window.set_addon_message("Operation failed: " + detail, error=True)
        set_status("Addon installation failed", detail, "critical")
        return
    _next_component_action()


def apply_component_changes():
    if not q3elite_is_installed():
        window.set_addon_message("Install Quake 3 Elite first.", error=True)
        return
    window.prepare_component_actions()
    if not window.pending_component_actions:
        window.set_addon_message("No changes to apply.")
        return
    window.set_addon_message("")
    _next_component_action()


def apply_settings():
    global launcher_settings
    old_vulkan = reshade_layer_enabled()
    old_changelog_media = launcher_settings.get("show_changelog_media", False)

    def selected_cleanup_days(checkbox, combo, custom):
        if not checkbox.isChecked():
            return 0
        days = combo.currentData()
        if days == -1:
            days = custom.value()
        return max(1, int(days or 1))

    cleanup_screenshots_days = selected_cleanup_days(
        window.cleanupScreenshotsBox,
        window.cleanupScreenshotsCombo,
        window.cleanupScreenshotsCustom,
    )
    cleanup_demos_days = selected_cleanup_days(
        window.cleanupDemosBox,
        window.cleanupDemosCombo,
        window.cleanupDemosCustom,
    )

    launcher_settings = {
        "auto_update_q3elite": window.autoQ3Box.isChecked(),
        "auto_update_launcher": window.autoLauncherBox.isChecked(),
        "auto_update_osp": window.autoOspBox.isChecked(),
        "start_with_windows": window.startWindowsBox.isChecked(),
        "minimize_to_tray": window.trayBox.isChecked(),
        # Despite the historical label, this controls ALL changelog media.
        "show_changelog_media": window.changelogMediaBox.isChecked(),
        "cleanup_screenshots_days": cleanup_screenshots_days,
        "cleanup_demos_days": cleanup_demos_days,
        "pause_server_refresh_unfocused": window.pauseServerRefreshBox.isChecked(),
    }
    try:
        set_start_with_windows(launcher_settings["start_with_windows"])
        requested_vulkan = window.vulkanLayerBox.isChecked()
        if requested_vulkan != old_vulkan:
            configure_reshade_vulkan(requested_vulkan, install_files=requested_vulkan)
        save_launcher_settings(launcher_settings)
        if old_changelog_media != launcher_settings["show_changelog_media"]:
            enabled = launcher_settings["show_changelog_media"]
            for card in window.changelogPage.findChildren(ChangelogCard):
                card.apply_media_preview_preference(enabled)
        window.settingsMessage.setText("Settings saved.")
    except Exception as error:
        window.vulkanLayerBox.setChecked(reshade_layer_enabled())
        window.settingsMessage.setText(f"Could not apply settings: {error}")


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
        prepare_first_install()


def prepare_first_install():
    """Fresh installs wait for explicit user confirmation."""
    _disconnect_main_button()
    window.firstInstallCard.show()
    window.playButton.setText("INSTALL")
    window.playButton.setEnabled(True)
    window.playButton.clicked.connect(start_first_install)
    window.progressBar.setRange(0, 100)
    window.progressBar.setValue(0)
    window.pauseButton.hide()
    window.downloadInfo.setText("Choose optional components, then press INSTALL.")
    window.installedValue.setText("Not installed")
    if hasattr(window, "autoexecUpdateButton"):
        window.autoexecUpdateButton.setEnabled(False)
    if hasattr(window, "applyAddonsButton"):
        window.applyAddonsButton.setEnabled(False)
    if hasattr(window, "configEditorButton"):
        window.configEditorButton.setEnabled(False)
    set_status("Ready to install", "Choose components and press INSTALL.", "normal")


def start_first_install():
    if q3elite_download.isRunning():
        return
    _disconnect_main_button()
    # Read Qt widgets on the GUI thread before starting QThread.
    q3elite_download.set_options(
        external_maps=window.firstInstallMapsBox.isChecked(),
        music_playlist=window.firstInstallMusicBox.isChecked(),
    )
    window.firstInstallCard.setEnabled(False)
    download_control.reset()
    install_state["base_done"] = False
    install_state["base_ok"] = False
    install_state["q3elite_done"] = False
    install_state["q3elite_ok"] = False
    install_state["post_update_started"] = False
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
        print("Waiting for the user to press INSTALL.")
        print()

        install_state["q3elite_done"] = False
        install_state["q3elite_ok"] = False
        prepare_first_install()


def launcher_self_update_result(action):
    if action == "restart":
        print()
        print("Launcher update staged. Restarting...")
        print()
        app.quit()
        return
    start_game_checks()




# ============================================================================
# GOTHIC VISUAL LAYER
# ============================================================================

class GothicShell(QtWidgets.QFrame):
    """Paints the launcher artwork once and keeps a blurred copy for glass panels."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._background = QtGui.QPixmap()
        self._blurred = QtGui.QPixmap()
        candidates = [BACKGROUND_IMAGE]
        for path in candidates:
            if path.is_file() and self._background.load(str(path)):
                break
        if not self._background.isNull():
            self._rebuild_blur()

    def _rebuild_blur(self):
        if self._background.isNull():
            return
        # Blur a downscaled copy: inexpensive and visually close to CSS backdrop-filter.
        small = self._background.scaled(420, 240, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        QtCore.Qt.TransformationMode.SmoothTransformation)
        scene = QtWidgets.QGraphicsScene()
        item = QtWidgets.QGraphicsPixmapItem(small)
        effect = QtWidgets.QGraphicsBlurEffect()
        effect.setBlurRadius(16.0)
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        out = QtGui.QPixmap(small.size())
        out.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(out)
        scene.render(painter, QtCore.QRectF(out.rect()), QtCore.QRectF(small.rect()))
        painter.end()
        self._blurred = out

    def _cover(self, pm, size):
        if pm.isNull(): return QtGui.QPixmap()
        scaled = pm.scaled(size, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           QtCore.Qt.TransformationMode.SmoothTransformation)
        x=max(0,(scaled.width()-size.width())//2); y=max(0,(scaled.height()-size.height())//2)
        return scaled.copy(x,y,size.width(),size.height())

    def paintEvent(self, event):
        painter=QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        if not self._background.isNull():
            painter.drawPixmap(self.rect(), self._cover(self._background, self.size()))
        else:
            painter.fillRect(self.rect(), QtGui.QColor('#080808'))
        # black film + subtle blood-red radial atmosphere
        painter.fillRect(self.rect(), QtGui.QColor(0,0,0,112))
        grad=QtGui.QRadialGradient(self.width()*.72,self.height()*.42,self.width()*.62)
        grad.setColorAt(0,QtGui.QColor(100,0,0,34)); grad.setColorAt(.55,QtGui.QColor(20,0,0,12)); grad.setColorAt(1,QtGui.QColor(0,0,0,0))
        painter.fillRect(self.rect(),grad)
        painter.end()
        super().paintEvent(event)


class GlassFrame(QtWidgets.QFrame):
    """Backdrop-style frosted panel sampling GothicShell's blurred artwork."""
    def paintEvent(self, event):
        shell=self.window().findChild(GothicShell, 'shell')
        if shell is not None and not shell._blurred.isNull():
            painter=QtGui.QPainter(self)
            # map this panel into shell coordinates, then sample equivalent normalized area
            top_left=self.mapTo(shell, QtCore.QPoint(0,0))
            sx=max(0,int(top_left.x()/max(1,shell.width())*shell._blurred.width()))
            sy=max(0,int(top_left.y()/max(1,shell.height())*shell._blurred.height()))
            sw=max(1,int(self.width()/max(1,shell.width())*shell._blurred.width()))
            sh=max(1,int(self.height()/max(1,shell.height())*shell._blurred.height()))
            src=QtCore.QRect(sx,sy,sw,sh).intersected(shell._blurred.rect())
            if src.isValid(): painter.drawPixmap(self.rect(), shell._blurred, src)
            painter.fillRect(self.rect(), QtGui.QColor(5,5,7,172))
            painter.end()
        super().paintEvent(event)


class GlowButton(QtWidgets.QPushButton):
    """Native animated hover glow; QSS itself cannot interpolate shadows."""
    def __init__(self, text='', parent=None):
        super().__init__(text,parent)
        fx=QtWidgets.QGraphicsDropShadowEffect(self); fx.setOffset(0,0); fx.setBlurRadius(0)
        fx.setColor(QtGui.QColor(180,0,0,210)); self.setGraphicsEffect(fx); self._glow=fx
        self._anim=QtCore.QPropertyAnimation(fx,b'blurRadius',self); self._anim.setDuration(180)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
    def _to(self,v):
        self._anim.stop(); self._anim.setStartValue(self._glow.blurRadius()); self._anim.setEndValue(v); self._anim.start()
    def enterEvent(self,e): self._to(22); super().enterEvent(e)
    def leaveEvent(self,e): self._to(0); super().leaveEvent(e)


class AnimatedEmoji(QtWidgets.QLabel):
    """Alpha-safe launcher emoji. Prefer animated WebP/GIF; PNG is static fallback."""
    def __init__(self, name, size=30, parent=None):
        super().__init__(parent); self.setFixedSize(size,size); self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        root=ASSETS_DIR/'emojis'; self._movie=None
        for ext in ('.webp','.gif','.png'):
            path=root/(name+ext)
            if not path.is_file(): continue
            if ext in ('.webp','.gif'):
                movie=QtGui.QMovie(str(path)); movie.setScaledSize(QtCore.QSize(size,size))
                if movie.isValid(): self._movie=movie; self.setMovie(movie); movie.start(); return
            pm=QtGui.QPixmap(str(path))
            if not pm.isNull(): self.setPixmap(pm.scaled(size,size,QtCore.Qt.AspectRatioMode.KeepAspectRatio,QtCore.Qt.TransformationMode.SmoothTransformation)); return
        self.setText('⛧'); self.setStyleSheet('color:#a00000;font-size:22px;background:transparent;')



class ConfigEditorDialog(QtWidgets.QDialog):
    """Two-pane Q3 config editor with zero-indent // section navigation and Ctrl+F."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Q3Elite Config Editor")
        self.resize(1600, 900)
        self.autoexec_path = GAME_ROOT / "baseq3" / "mods" / "osp" / "autoexec.cfg"
        self.userconfig_path = GAME_ROOT / "baseq3" / "mods" / "osp" / "UserConfig.cfg"

        root = QtWidgets.QVBoxLayout(self)
        panes = QtWidgets.QHBoxLayout()
        panes.setSpacing(12)

        self.autoexecPanel = self._build_editor_panel(
            "AUTOEXEC.CFG  •  READ ONLY", read_only=True
        )
        self.userPanel = self._build_editor_panel(
            "USERCONFIG.CFG  •  EDITABLE", read_only=False
        )
        panes.addWidget(self.autoexecPanel["widget"], 1)
        panes.addWidget(self.userPanel["widget"], 1)
        root.addLayout(panes, 1)

        row = QtWidgets.QHBoxLayout()
        self.message = QtWidgets.QLabel("")
        self.message.setObjectName("message")
        row.addWidget(self.message, 1)

        reload_button = GlowButton("RELOAD")
        reload_button.setObjectName("secondaryButton")
        reload_button.clicked.connect(self.reload_files)
        row.addWidget(reload_button)

        save_button = GlowButton("SAVE USER CONFIG")
        save_button.setObjectName("applyButton")
        save_button.clicked.connect(self.save_user_config)
        row.addWidget(save_button)
        root.addLayout(row)

        self.autoexecEdit = self.autoexecPanel["edit"]
        self.userEdit = self.userPanel["edit"]
        self.reload_files()

    def _build_editor_panel(self, title, read_only):
        frame = QtWidgets.QFrame()
        frame.setObjectName("configEditorPanel")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)

        label = QtWidgets.QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)

        find_row = QtWidgets.QHBoxLayout()
        find_edit = QtWidgets.QLineEdit()
        find_edit.setPlaceholderText("Find...")
        find_edit.hide()
        find_prev = QtWidgets.QPushButton("↑")
        find_next = QtWidgets.QPushButton("↓")
        find_close = QtWidgets.QPushButton("×")
        find_count = QtWidgets.QLabel("")
        for w in (find_prev, find_next, find_count, find_close):
            w.hide()
        find_row.addWidget(find_edit, 1)
        find_row.addWidget(find_prev)
        find_row.addWidget(find_next)
        find_row.addWidget(find_count)
        find_row.addWidget(find_close)
        layout.addLayout(find_row)

        body = QtWidgets.QHBoxLayout()
        sections = QtWidgets.QListWidget()
        sections.setObjectName("configSections")
        sections.setFixedWidth(150)
        sections.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        body.addWidget(sections)

        edit = QtWidgets.QPlainTextEdit()
        edit.setReadOnly(read_only)
        edit.setTabStopDistance(
            QtGui.QFontMetricsF(edit.font()).horizontalAdvance(" ") * 4
        )
        body.addWidget(edit, 1)
        layout.addLayout(body, 1)

        panel = {
            "widget": frame,
            "edit": edit,
            "sections": sections,
            "find": find_edit,
            "find_prev": find_prev,
            "find_next": find_next,
            "find_close": find_close,
            "find_count": find_count,
        }

        sections.itemClicked.connect(
            lambda item, p=panel: self._jump_to_section(p, item)
        )
        find_edit.returnPressed.connect(
            lambda p=panel: self._find(p, backwards=False)
        )
        find_next.clicked.connect(lambda _=False, p=panel: self._find(p, False))
        find_prev.clicked.connect(lambda _=False, p=panel: self._find(p, True))
        find_close.clicked.connect(lambda _=False, p=panel: self._close_find(p))

        shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+F"), edit)
        shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda p=panel: self._open_find(p))

        escape = QtGui.QShortcut(QtGui.QKeySequence("Esc"), frame)
        escape.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(lambda p=panel: self._close_find(p))

        return panel

    def _read(self, path, editable=False):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            if editable:
                return (
                    "// UserConfig.cfg does not exist yet.\n"
                    "// Saving this editor will create it.\n"
                    f"// Path: {path}\n"
                )
            return f"// File not found:\n// {path}\n"

    @staticmethod
    def _section_rows(text):
        """Only zero-indent // comments are navigation sections."""
        rows = []
        for line_number, line in enumerate(text.splitlines()):
            if line.startswith("//"):
                title = line[2:].strip()
                if title:
                    rows.append((title, line_number))
        return rows

    def _rebuild_sections(self, panel):
        panel["sections"].clear()
        for title, line_number in self._section_rows(panel["edit"].toPlainText()):
            item = QtWidgets.QListWidgetItem(title)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, line_number)
            panel["sections"].addItem(item)

    def _jump_to_section(self, panel, item):
        line_number = int(item.data(QtCore.Qt.ItemDataRole.UserRole) or 0)
        block = panel["edit"].document().findBlockByNumber(line_number)
        cursor = QtGui.QTextCursor(block)
        panel["edit"].setTextCursor(cursor)
        panel["edit"].centerCursor()
        panel["edit"].setFocus()

    def _open_find(self, panel):
        for key in ("find", "find_prev", "find_next", "find_count", "find_close"):
            panel[key].show()
        selected = panel["edit"].textCursor().selectedText()
        if selected and "\n" not in selected:
            panel["find"].setText(selected)
        panel["find"].setFocus()
        panel["find"].selectAll()
        self._update_find_count(panel)

    def _close_find(self, panel):
        for key in ("find", "find_prev", "find_next", "find_count", "find_close"):
            panel[key].hide()
        panel["edit"].setFocus()

    def _all_matches(self, panel):
        query = panel["find"].text()
        if not query:
            return []
        document = panel["edit"].document()
        cursor = QtGui.QTextCursor(document)
        matches = []
        while True:
            cursor = document.find(query, cursor)
            if cursor.isNull():
                break
            matches.append((cursor.selectionStart(), cursor.selectionEnd()))
        return matches

    def _update_find_count(self, panel):
        matches = self._all_matches(panel)
        if not matches:
            panel["find_count"].setText("0 / 0")
            return
        pos = panel["edit"].textCursor().selectionStart()
        current = 1
        for index, (start, end) in enumerate(matches, 1):
            if start <= pos <= end:
                current = index
                break
            if start < pos:
                current = min(index + 1, len(matches))
        panel["find_count"].setText(f"{current} / {len(matches)}")

    def _find(self, panel, backwards=False):
        query = panel["find"].text()
        if not query:
            return
        flags = QtGui.QTextDocument.FindFlag.FindBackward if backwards else QtGui.QTextDocument.FindFlag(0)
        found = panel["edit"].find(query, flags)
        if not found:
            cursor = panel["edit"].textCursor()
            cursor.movePosition(
                QtGui.QTextCursor.MoveOperation.End if backwards
                else QtGui.QTextCursor.MoveOperation.Start
            )
            panel["edit"].setTextCursor(cursor)
            panel["edit"].find(query, flags)
        self._update_find_count(panel)

    def reload_files(self):
        self.autoexecEdit.setPlainText(self._read(self.autoexec_path))
        self.userEdit.setPlainText(self._read(self.userconfig_path, editable=True))
        self._rebuild_sections(self.autoexecPanel)
        self._rebuild_sections(self.userPanel)
        self.message.setText("Reloaded.")

    def save_user_config(self):
        try:
            self.userconfig_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.userconfig_path.with_suffix(
                self.userconfig_path.suffix + ".tmp"
            )
            temp.write_text(self.userEdit.toPlainText(), encoding="utf-8")
            os.replace(temp, self.userconfig_path)
            self._rebuild_sections(self.userPanel)
            self.message.setText("UserConfig.cfg saved.")
        except Exception as error:
            self.message.setText(f"Could not save UserConfig.cfg: {error}")






if TELEGRAM_WEBENGINE_AVAILABLE:
    from PyQt6.QtWebEngineCore import QWebEnginePage

    class TelegramExternalPage(QWebEnginePage):
        """Never let user links replace the embedded Telegram post."""
        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                QtGui.QDesktopServices.openUrl(url)
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

        def createWindow(self, window_type):
            # Telegram sometimes requests a new tab/window. Return a temporary
            # page that forwards its first URL to the system browser.
            page = QWebEnginePage(self)
            page.urlChanged.connect(
                lambda url: QtGui.QDesktopServices.openUrl(url)
                if url.isValid() and url.scheme() in ("http", "https") else None
            )
            return page
else:
    TelegramExternalPage = None


class TelegramTextView(QWebEngineView if QWebEngineView is not None else QtWidgets.QWidget):
    """Telegram's real web renderer, reduced to message text/custom emoji only."""
    mediaDetected = QtCore.pyqtSignal(object)
    def __init__(self, url, parent=None):
        if QWebEngineView is None:
            super().__init__(parent)
            return

        super().__init__(parent)
        self.setObjectName("telegramTextView")
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.NoContextMenu)
        self.setPage(TelegramExternalPage(self))
        self.setMinimumHeight(40)
        self.setMaximumHeight(900)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        # WebEngine is transparent again. The Qt host below paints the
        # changelog/card surface, preventing the Windows-desktop bleed-through
        # while allowing Background.png to remain visible through the card.
        # Chromium transparency on Windows can punch through the whole
        # translucent launcher. Keep Chromium composited on a very dark,
        # slightly translucent-looking surface instead of WA_TranslucentBackground.
        self.page().setBackgroundColor(QtGui.QColor(8, 9, 10, 255))
        self.loadFinished.connect(self._telegram_loaded)
        self.titleChanged.connect(self._telegram_title_changed)
        self.setUrl(QtCore.QUrl(self._public_url(url)))

    @staticmethod
    def _public_url(url):
        value = str(url or "").strip()
        # Accept all useful user forms:
        # t.me/Q3News/260
        # t.me/s/Q3News/260
        # t.me/Q3News/s/260
        m = re.search(
            r"https?://t\.me/(?:s/)?([^/?#]+)/(?:(?:s)/)?(\d+)",
            value,
            flags=re.I,
        )
        if not m:
            return value
        channel, post_id = m.group(1), m.group(2)
        return f"https://t.me/s/{channel}/{post_id}"

    def _telegram_title_changed(self, title):
        prefix = "Q3ELITE_MEDIA:"
        if not str(title).startswith(prefix):
            return
        try:
            import json as _json
            payload = urllib.parse.unquote(str(title)[len(prefix):])
            data = _json.loads(payload)
            self.mediaDetected.emit(data)
        except Exception as error:
            print(f"[changelog] Telegram DOM media decode failed: {error}")

    def reload_post(self):
        self.show()
        self.setFixedHeight(40)
        self.reload()

    def _telegram_loaded(self, ok):
        if not ok:
            self.hide()
            return

        # Important: this is NOT the official iframe widget. We load Telegram's
        # own public post page as the top-level document, so we can restyle the
        # DOM after Telegram has rendered its custom emoji.
        js = r"""
        (() => {
            const messages = [...document.querySelectorAll('.tgme_widget_message')];
            let msg = messages.find(m => {
                const a = m.getAttribute('data-post') || '';
                return location.pathname.includes(a) || location.pathname.endsWith('/' + a.split('/').pop());
            }) || messages[messages.length - 1];

            if (!msg) return 0;

            const text = msg.querySelector('.tgme_widget_message_text');
            if (!text) return 0;

            // Extract only actual screenshot/photo containers.
            const media = {images: [], video: "", poster: ""};
            const addImage = u => {
                if (!u) return;
                try { u = new URL(u, location.href).href; } catch(e) {}
                const low = String(u).toLowerCase();
                if (low.endsWith('.webm') || low.endsWith('.mp4') || low.endsWith('.tgs')) return;
                if (!media.images.includes(u)) media.images.push(u);
            };

            msg.querySelectorAll('a.tgme_widget_message_photo_wrap').forEach(el => {
                let bg = el.style.backgroundImage || getComputedStyle(el).backgroundImage || '';
                let m = bg.match(/url\((?:"|')?(.*?)(?:"|')?\)/i);
                if (m && m[1]) addImage(m[1]);
            });

            for (const a of Array.from(msg.querySelectorAll('a[href]'))) {
                const href = a.href || a.getAttribute('href') || '';
                if (!/(?:youtube\.com\/|youtu\.be\/)/i.test(href)) continue;
                const p = (a.parentElement && a.parentElement.innerText) || '';
                const d = (a.closest('div') && a.closest('div').innerText) || '';
                const context = (a.innerText + ' ' + p + ' ' + d).toLowerCase();
                if (context.includes('reupload')) {
                    media.reupload = href;
                    break;
                }
            }

            // Telegram uses several video class variants, including
            // tgme_widget_message_video_player. Match by class substring.
            const videoBox = msg.querySelector('[class*="tgme_widget_message_video"]');
            if (videoBox) {
                const v = videoBox.matches('video') ? videoBox : videoBox.querySelector('video');
                let poster =
                    (v && (v.poster || v.getAttribute('poster'))) ||
                    videoBox.getAttribute('data-poster') ||
                    '';

                const candidates = [videoBox];
                const nested = videoBox.querySelectorAll('*');
                nested.forEach(el => candidates.push(el));

                if (!poster) {
                    for (const el of candidates) {
                        let bg = el.style.backgroundImage || getComputedStyle(el).backgroundImage || '';
                        let m = bg.match(/url\((?:"|')?(.*?)(?:"|')?\)/i);
                        if (m && m[1]) {
                            poster = m[1];
                            break;
                        }
                    }
                }

                if (poster) {
                    try { poster = new URL(poster, location.href).href; } catch(e) {}
                    media.poster = poster;
                    media.video = "__telegram_post__";
                }
            }

            document.title = 'Q3ELITE_MEDIA:' + encodeURIComponent(JSON.stringify(media));

            // Preserve Telegram's text DOM (including custom emoji elements),
            // but remove everything else from the public channel page.
            document.body.innerHTML = '';
            document.body.appendChild(text);

            const style = document.createElement('style');
            style.textContent = `
                html, body {
                    margin: 0 !important;
                    padding: 0 !important;
                    background: #08090a !important;
                    overflow: hidden !important;
                    color: #d7d2cc !important;
                }
                body {
                    display: flex !important;
                    justify-content: flex-start !important;
                }
                body, .tgme_widget_message_text {
                    font-family: "Segoe UI", Arial, sans-serif !important;
                    font-size: 13px !important;
                    line-height: 1.48 !important;
                    color: #d7d2cc !important;
                    background: #08090a !important;
                    margin: 0 !important;
                    padding: 0 !important;
                }
                .tgme_widget_message_text {
                    width: min(100%, 760px) !important;
                    max-width: 760px !important;
                    background: #08090a !important;
                }
                a { color: #d79a28 !important; }
                .emoji, .tgme_widget_message_text .emoji {
                    vertical-align: -0.18em !important;
                }
            `;
            document.head.appendChild(style);

            // Telegram normally intercepts links and displays its own
            // "Open this link?" confirmation. Remove Telegram's handlers and
            // let QWebEnginePage route the click straight to the OS browser.
            document.querySelectorAll('a[href]').forEach(a => {
                const clean = a.cloneNode(true);
                clean.removeAttribute('onclick');
                clean.removeAttribute('target');
                a.replaceWith(clean);
            });

            return Math.ceil(text.getBoundingClientRect().height + 6);
        })();
        """
        self.page().runJavaScript(js, self._apply_height)
        # Animated/custom emoji and web fonts can settle after loadFinished.
        # Re-measure the retained text shortly afterwards so first load has the
        # same compact geometry as a manual Reload.
        QtCore.QTimer.singleShot(180, self._remeasure_height)
        QtCore.QTimer.singleShot(650, self._remeasure_height)

    def _remeasure_height(self):
        if QWebEngineView is None or not self.isVisible():
            return
        self.page().runJavaScript(
            """(() => {
                const text = document.querySelector('.tgme_widget_message_text');
                return text ? Math.ceil(text.getBoundingClientRect().height + 6) : 0;
            })();""",
            self._apply_height,
        )

    def _apply_height(self, height):
        try:
            height = int(height or 0)
        except Exception:
            height = 0
        if height <= 0:
            self.hide()
            return
        # Give the full Telegram text to the OUTER changelog QScrollArea.
        # The embedded browser itself never becomes independently scrollable.
        self.setFixedHeight(max(40, min(height, 900)))
        self.updateGeometry()
        parent = self.parentWidget()
        while parent is not None:
            if parent.layout() is not None:
                parent.layout().invalidate()
                parent.layout().activate()
            parent.updateGeometry()
            parent = parent.parentWidget()

    def emit_media_now(self):
        if QWebEngineView is None or not self.isVisible():
            return
        script = r"""
        (() => {
            const msg = document.querySelector('.tgme_widget_message') || document.body;
            if (!msg) return;
            const media = {images: [], video: '', poster: '', reupload: ''};

            const addImage = (u) => {
                if (!u) return;
                try { u = new URL(u, location.href).href; } catch(e) {}
                const low = String(u).toLowerCase();
                if (low.endsWith('.webm') || low.endsWith('.mp4') || low.endsWith('.tgs')) return;
                if (!media.images.includes(u)) media.images.push(u);
            };
            msg.querySelectorAll('a.tgme_widget_message_photo_wrap').forEach(el => {
                const bg = el.style.backgroundImage || getComputedStyle(el).backgroundImage || '';
                const m = bg.match(/url\((?:"|')?(.*?)(?:"|')?\)/i);
                if (m && m[1]) addImage(m[1]);
            });

            // The rendered DOM is more reliable than raw Telegram HTML for links.
            for (const a of Array.from(msg.querySelectorAll('a[href]'))) {
                const href = a.href || a.getAttribute('href') || '';
                if (!/(?:youtube\.com\/|youtu\.be\/)/i.test(href)) continue;
                const p = (a.parentElement && a.parentElement.innerText) || '';
                const d = (a.closest('div') && a.closest('div').innerText) || '';
                const context = (a.innerText + ' ' + p + ' ' + d).toLowerCase();
                if (context.includes('reupload')) {
                    media.reupload = href;
                    break;
                }
            }

            const videoBox = msg.querySelector('[class*="tgme_widget_message_video"]');
            if (videoBox) {
                const v = videoBox.matches('video') ? videoBox : videoBox.querySelector('video');
                let poster = (v && (v.poster || v.getAttribute('poster'))) ||
                             videoBox.getAttribute('data-poster') || '';
                if (!poster) {
                    const candidates = [videoBox, ...videoBox.querySelectorAll('*')];
                    for (const el of candidates) {
                        const bg = el.style.backgroundImage || getComputedStyle(el).backgroundImage || '';
                        const m = bg.match(/url\((?:"|')?(.*?)(?:"|')?\)/i);
                        if (m && m[1]) { poster = m[1]; break; }
                    }
                }
                if (poster) {
                    try { poster = new URL(poster, location.href).href; } catch(e) {}
                    media.poster = poster;
                }
                media.video = '__telegram_post__';
            }

            document.title = 'Q3ELITE_MEDIA:' +
                encodeURIComponent(JSON.stringify(media));
        })();
        """
        self.page().runJavaScript(script)

    def refresh_after_expand(self):
        """Re-measure a Telegram post after a previously hidden card becomes visible."""
        if QWebEngineView is None:
            return

        # Hidden WebEngine cards do not get their delayed 180/650 ms measurements.
        # Run several cheap DOM measurements after Qt has exposed/relaid-out the card.
        for delay in (0, 40, 120, 300, 650):
            QtCore.QTimer.singleShot(delay, self._remeasure_height)




class ChangelogImage(QtWidgets.QLabel):
    """Fixed 16:9 media viewport with rounded clipping and fullscreen preview."""
    BASE_WIDTH = 720

    def __init__(self, url="", parent=None):
        super().__init__(parent)
        self.setObjectName("changelogImage")
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self._pixmap_original = None
        self._url = str(url or "")

        # Reserve final geometry immediately. This prevents the "large -> small"
        # jump when a remote image finishes loading.
        self.setFixedSize(self.BASE_WIDTH, round(self.BASE_WIDTH * 9 / 16))
        self._apply_rounded_mask()
        self.setText("Loading image...")
        if self._url:
            self._load()

    def _cache_path(self):
        import hashlib
        suffix = Path(urllib.parse.urlparse(self._url).path).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            suffix = ".img"
        cache = Path(os.environ.get("APPDATA", Path.home())) / "Quake 3 Elite" / "Cache" / "Changelog"
        cache.mkdir(parents=True, exist_ok=True)
        return cache / (hashlib.sha256(self._url.encode("utf-8")).hexdigest() + suffix)

    def _load(self):
        try:
            cache_file = self._cache_path()
            if cache_file.is_file():
                data = cache_file.read_bytes()
            else:
                req = urllib.request.Request(
                    self._url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/140.0.0.0 Safari/537.36"
                        ),
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                        "Referer": "https://t.me/",
                    },
                )
                with urllib.request.urlopen(req, timeout=12) as response:
                    data = response.read()
                if data:
                    cache_file.write_bytes(data)

            pix = QtGui.QPixmap()
            if data and pix.loadFromData(data):
                self._pixmap_original = pix
                self.setText("")
                self._render_pixmap()
            else:
                self.setText("Image preview unavailable")
        except Exception as error:
            print(f"[changelog] Image unavailable: {error}")
            self.setText("Image preview unavailable")
            # Avoid a large empty 16:9 hole for failed Telegram/CDN media.
            self.setFixedHeight(42)

    def _render_pixmap(self):
        if self._pixmap_original is None or self._pixmap_original.isNull():
            return

        target = self.size()
        scaled = self._pixmap_original.scaled(
            target,
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - target.width()) // 2)
        y = max(0, (scaled.height() - target.height()) // 2)
        cropped = scaled.copy(x, y, target.width(), target.height())

        # Compose clipping + border into the pixmap itself. QWidget masks can
        # clip the anti-aliased right/bottom border on Windows.
        result = QtGui.QPixmap(target)
        result.fill(QtCore.Qt.GlobalColor.transparent)

        painter = QtGui.QPainter(result)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)

        outer = QtCore.QRectF(0.75, 0.75, target.width() - 1.5, target.height() - 1.5)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(outer, 9.0, 9.0)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, cropped)
        painter.setClipping(False)

        pen = QtGui.QPen(QtGui.QColor(170, 160, 145, 155))
        pen.setWidthF(1.25)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(outer, 9.0, 9.0)
        painter.end()

        self.clearMask()
        self.setPixmap(result)

    def _apply_rounded_mask(self):
        # Kept as a no-op for calls made before/while the remote image loads.
        # The loaded image is clipped precisely in _render_pixmap().
        self.clearMask()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_pixmap()


class ChangelogMediaCarousel(QtWidgets.QFrame):
    def __init__(self, urls, parent=None):
        super().__init__(parent)
        self.setObjectName("changelogMedia")
        self.setFixedSize(720, 429)
        self.urls = [str(u) for u in urls if str(u).strip()]
        self.index = 0

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.stage = QtWidgets.QWidget()
        self.stage.setObjectName("changelogMediaStage")
        self.stage.setFixedSize(720, 405)

        self.image = ChangelogImage(parent=self.stage)
        self.image.move(0, 0)

        self.prevButton = QtWidgets.QPushButton("", self.stage)
        self.prevButton.setObjectName("changelogArrow")
        self.prevButton.setFixedSize(38, 58)
        self.prevButton.move(12, (405 - 58) // 2)
        if qta is not None:
            self.prevButton.setIcon(qta.icon("fa5s.chevron-left", color="#f1ece5"))
        else:
            self.prevButton.setText("‹")
        self.prevButton.clicked.connect(lambda: self.change(-1))

        self.nextButton = QtWidgets.QPushButton("", self.stage)
        self.nextButton.setObjectName("changelogArrow")
        self.nextButton.setFixedSize(38, 58)
        self.nextButton.move(720 - 50, (405 - 58) // 2)
        if qta is not None:
            self.nextButton.setIcon(qta.icon("fa5s.chevron-right", color="#f1ece5"))
        else:
            self.nextButton.setText("›")
        self.nextButton.clicked.connect(lambda: self.change(1))

        lay.addWidget(self.stage)

        self.counter = QtWidgets.QLabel("")
        self.counter.setObjectName("changelogCounter")
        self.counter.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.counter.setFixedHeight(20)
        lay.addWidget(self.counter)

        self.show_current()

    def show_current(self):
        count = len(self.urls)
        if not count:
            self.hide()
            return
        self.image._url = self.urls[self.index]
        self.image._pixmap_original = None
        self.image.setPixmap(QtGui.QPixmap())
        self.image.setText("Loading image...")
        self.image._load()
        self.counter.setText(f"{self.index + 1} / {count}" if count > 1 else "")
        self.prevButton.setVisible(count > 1)
        self.nextButton.setVisible(count > 1)
        self.prevButton.raise_()
        self.nextButton.raise_()

    def change(self, delta):
        if self.urls:
            self.index = (self.index + delta) % len(self.urls)
            self.show_current()




class PlayOverlayButton(QtWidgets.QPushButton):
    """Paints a geometrically centered play triangle."""
    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        w, h = 20.0, 24.0
        # Triangle's visual centroid is offset; compensate slightly left.
        cx = self.width() / 2.0 - 1.5
        cy = self.height() / 2.0
        path = QtGui.QPainterPath()
        path.moveTo(cx - w / 2.0, cy - h / 2.0)
        path.lineTo(cx - w / 2.0, cy + h / 2.0)
        path.lineTo(cx + w / 2.0, cy)
        path.closeSubpath()
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255))
        p.drawPath(path)
        p.end()


class YoutubeThumbnail(QtWidgets.QFrame):
    """Lightweight YouTube preview: HQ thumbnail + native Play overlay."""

    def __init__(self, youtube_url, parent=None):
        super().__init__(parent)
        self.setObjectName("youtubeThumbnail")
        self.youtube_url = str(youtube_url or "").strip()
        self.video_id = self._video_id(self.youtube_url)
        self.setFixedSize(800, 450)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

        self.image = QtWidgets.QLabel(self)
        self.image.setObjectName("youtubeThumbnailImage")
        self.image.setGeometry(0, 0, 800, 450)
        self.image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.image.setScaledContents(False)

        self.play = PlayOverlayButton(self)
        self.play.setObjectName("youtubePlayOverlay")
        self.play.setFixedSize(76, 54)
        self.play.clicked.connect(self.open_video)
        self.play.raise_()

        self._manager = QtNetwork.QNetworkAccessManager(self)
        self._try_index = 0
        self._candidates = []
        if self.video_id:
            # maxres is normally 1280x720. sd/default are graceful fallbacks.
            self._candidates = [
                f"https://i.ytimg.com/vi/{self.video_id}/maxresdefault.jpg",
                f"https://i.ytimg.com/vi/{self.video_id}/sddefault.jpg",
                f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg",
            ]
            self._load_next()
        else:
            self.image.setText("YouTube preview unavailable")

    @staticmethod
    def _video_id(url):
        value = str(url or "")
        for pattern in (
            r"(?:youtube\.com/watch\?(?:[^#]*&)?v=)([A-Za-z0-9_-]{6,})",
            r"(?:youtu\.be/)([A-Za-z0-9_-]{6,})",
            r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{6,})",
        ):
            match = re.search(pattern, value, flags=re.I)
            if match:
                return match.group(1)
        return ""

    def _rounded_video_pixmap(self, pixmap):
        if pixmap is None or pixmap.isNull():
            return pixmap
        size = pixmap.size()
        result = QtGui.QPixmap(size)
        result.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(result)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QtCore.QRectF(0.75, 0.75, size.width() - 1.5, size.height() - 1.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 9.0, 9.0)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.setClipping(False)
        pen = QtGui.QPen(QtGui.QColor(170, 160, 145, 155))
        pen.setWidthF(1.25)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 9.0, 9.0)
        painter.end()
        return result

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.image.setGeometry(self.rect())
        self.play.move(
            (self.width() - self.play.width()) // 2,
            (self.height() - self.play.height()) // 2,
        )
        self.play.raise_()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.open_video()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def open_video(self):
        if self.youtube_url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.youtube_url))

    def _load_next(self):
        if self._try_index >= len(self._candidates):
            self.image.setText("YouTube preview unavailable")
            return
        url = self._candidates[self._try_index]
        self._try_index += 1
        reply = self._manager.get(QtNetwork.QNetworkRequest(QtCore.QUrl(url)))
        reply.finished.connect(lambda r=reply: self._thumbnail_reply(r))

    def _thumbnail_reply(self, reply):
        try:
            if reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                pix = QtGui.QPixmap()
                if pix.loadFromData(data) and pix.width() >= 480:
                    scaled = pix.scaled(
                        self.size(),
                        QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        QtCore.Qt.TransformationMode.SmoothTransformation,
                    )
                    x = max(0, (scaled.width() - self.width()) // 2)
                    y = max(0, (scaled.height() - self.height()) // 2)
                    cropped = scaled.copy(x, y, self.width(), self.height())
                    self.image.setPixmap(self._rounded_video_pixmap(cropped))
                    return
        finally:
            reply.deleteLater()
        self._load_next()


class TelegramVideoThumbnail(QtWidgets.QFrame):
    """Telegram video poster; Play opens the original Telegram post."""
    def __init__(self, poster_url, post_url, parent=None):
        super().__init__(parent)
        self.setObjectName("telegramVideoThumbnail")
        self.poster_url = str(poster_url or "").strip()
        self.post_url = str(post_url or "").strip()
        self.setFixedSize(800, 450)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

        self.image = QtWidgets.QLabel(self)
        self.image.setObjectName("telegramVideoThumbnailImage")
        self.image.setGeometry(self.rect())
        self.image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.play = PlayOverlayButton(self)
        self.play.setObjectName("telegramPlayOverlay")
        self.play.setFixedSize(76, 54)
        self.play.clicked.connect(self.open_post)

        self._manager = QtNetwork.QNetworkAccessManager(self)
        if self.poster_url:
            request = QtNetwork.QNetworkRequest(QtCore.QUrl(self.poster_url))
            request.setRawHeader(b"User-Agent", b"Mozilla/5.0")
            reply = self._manager.get(request)
            reply.finished.connect(lambda r=reply: self._poster_ready(r))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.image.setGeometry(self.rect())
        self.play.move((self.width()-self.play.width())//2,
                       (self.height()-self.play.height())//2)
        self.play.raise_()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.open_post()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def open_post(self):
        if self.post_url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.post_url))

    def _poster_ready(self, reply):
        try:
            if reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError:
                return
            pix = QtGui.QPixmap()
            if not pix.loadFromData(bytes(reply.readAll())):
                return
            target = self.size()
            scaled = pix.scaled(target, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                QtCore.Qt.TransformationMode.SmoothTransformation)
            x=max(0,(scaled.width()-target.width())//2)
            y=max(0,(scaled.height()-target.height())//2)
            cropped=scaled.copy(x,y,target.width(),target.height())

            result=QtGui.QPixmap(target)
            result.fill(QtCore.Qt.GlobalColor.transparent)
            p=QtGui.QPainter(result)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            rect=QtCore.QRectF(.75,.75,target.width()-1.5,target.height()-1.5)
            path=QtGui.QPainterPath(); path.addRoundedRect(rect,9,9)
            p.setClipPath(path); p.drawPixmap(0,0,cropped); p.setClipping(False)
            pen=QtGui.QPen(QtGui.QColor(170,160,145,155)); pen.setWidthF(1.25)
            p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect,9,9); p.end()
            self.image.setPixmap(result)
        finally:
            reply.deleteLater()


class ChangelogVideoContainer(QtWidgets.QFrame):
    """Video entries use a lightweight thumbnail; playback opens in browser."""
    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.setObjectName("changelogVideoContainer")
        self.url = str(url or "")
        self.setFixedSize(800, 450)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        if re.search(r"(youtube\.com|youtu\.be)", self.url, flags=re.I):
            self.player = YoutubeThumbnail(self.url, self)
        else:
            self.player = ChangelogVideoView(self.url, self)
        lay.addWidget(self.player)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(0.75, 0.75, self.width() - 1.5, self.height() - 1.5)
        pen = QtGui.QPen(QtGui.QColor(170, 160, 145, 155))
        pen.setWidthF(1.25)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 9.0, 9.0)
        p.end()



class ChangelogVideoView(QWebEngineView if QWebEngineView is not None else QtWidgets.QWidget):
    """Fallback only for direct MP4/WEBM URLs."""
    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.setObjectName("changelogVideo")
        self.url = str(url or "").strip()
        self.setFixedSize(800, 450)
        if QWebEngineView is None:
            return
        self.page().setBackgroundColor(QtGui.QColor(0, 0, 0, 255))
        if re.search(r"\.(?:mp4|webm)(?:\?|$)", self.url, flags=re.I):
            safe = _html_escape(self.url)
            self.setHtml(
                f"""<!doctype html><html><body style="margin:0;background:#000;overflow:hidden">
                <video controls muted playsinline preload="metadata"
                       style="width:100%;height:100%;object-fit:contain;background:#000">
                  <source src="{safe}">
                </video></body></html>""",
                QtCore.QUrl(self.url),
            )
        else:
            self.setUrl(QtCore.QUrl(self.url))


class ChangelogCard(QtWidgets.QFrame):
    def __init__(self, release, parent=None, expanded=True):
        super().__init__(parent)
        self.setObjectName("changelogCard")
        release = dict(release)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(9)

        source_url = str(release.get("telegram", release.get("text_source", ""))).strip()
        self.telegram_url = source_url
        self.telegram_video_reupload = ""
        tg = _telegram_public_post_data(source_url) if source_url else {}

        self.headerWidget = QtWidgets.QWidget()
        self.headerWidget.setObjectName("changelogHeader")
        self.headerWidget.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        header = QtWidgets.QHBoxLayout(self.headerWidget)
        header.setContentsMargins(0, 0, 0, 0)

        title = QtWidgets.QLabel()
        title.setObjectName("changelogCardTitle")
        title.setTextFormat(QtCore.Qt.TextFormat.RichText)
        title.setText(
            f'{_html_escape(release.get("type", "Update"))} '
            f'<span style="color:#d82d25;">v{_html_escape(release.get("version", "?"))}</span>'
        )
        header.addWidget(title)
        header.addStretch(1)

        date = str(release.get("date", release.get("release_date", "")))
        if date:
            date_label = QtWidgets.QLabel(date)
            date_label.setObjectName("changelogDate")
            header.addWidget(date_label)

        self.toggleButton = QtWidgets.QPushButton("")
        self.toggleButton.setObjectName("changelogToggle")
        self.toggleButton.setFixedSize(28, 28)
        header.addWidget(self.toggleButton)
        lay.addWidget(self.headerWidget)

        self.headerWidget.installEventFilter(self)
        title.installEventFilter(self)
        if date:
            date_label.installEventFilter(self)

        self.bodyWidget = QtWidgets.QWidget()
        self.bodyWidget.setObjectName("changelogCardBody")
        bodyLayout = QtWidgets.QVBoxLayout(self.bodyWidget)
        bodyLayout.setContentsMargins(0, 0, 0, 0)
        bodyLayout.setSpacing(9)
        lay.addWidget(self.bodyWidget)

        # Everything after the title bar belongs to the collapsible body.
        lay = bodyLayout

        self._expanded = bool(expanded)
        self.toggleButton.clicked.connect(self._toggle)
        self.bodyWidget.setVisible(self._expanded)
        self._update_toggle_icon()

        # Hybrid mode:
        # Telegram renders only Telegram-sourced text/custom emoji.
        # Everything else (header, media, controls) remains native Q3Elite UI.
        explicit_text = str(release.get("text", "") or "")
        use_telegram_text = bool(source_url and not explicit_text and TELEGRAM_WEBENGINE_AVAILABLE)

        self.telegramView = None
        if use_telegram_text:
            self.telegramView = TelegramTextView(source_url, self)
            self.telegramView.mediaDetected.connect(self._apply_telegram_media)
            lay.addWidget(self.telegramView)

        # Manual/native text + changelog are combined into ONE QTextBrowser.
        # This fixes selection being limited to one QLabel/one line.
        changes = release.get(
            "changelog",
            release.get("changes", release.get("items", release.get("notes", [])))
        )
        list_mode = bool(release.get("list", True))

        if isinstance(changes, str):
            raw_changes = changes.strip()
            if raw_changes.casefold().startswith("[list]") and raw_changes.casefold().endswith("[/list]"):
                list_mode = True
                raw_changes = raw_changes[6:-7].strip()
            elif raw_changes.casefold().startswith("[nolist]") and raw_changes.casefold().endswith("[/nolist]"):
                list_mode = False
                raw_changes = raw_changes[8:-9].strip()
            changes = [line for line in raw_changes.splitlines() if line.strip()]

        native_body = explicit_text
        if not native_body and source_url and not use_telegram_text:
            native_body = str(tg.get("text", "") or "")

        native_parts = []
        if native_body:
            native_parts.append(
                '<div class="body">' +
                _html_escape(native_body).replace("\n", "<br>") +
                '</div>'
            )

        if isinstance(changes, list) and changes:
            if list_mode:
                native_parts.append("<ul>")
            else:
                native_parts.append('<div class="plainList">')

            for item in changes:
                if isinstance(item, dict):
                    label = str(item.get("text", item.get("label", item.get("title", ""))))
                    link = str(item.get("link", item.get("url", "")))
                    content = (
                        f'<a href="{_url_attr(link)}">{_html_escape(label or link)}</a>'
                        if link else _html_escape(label)
                    )
                else:
                    content = _html_escape(str(item))

                if list_mode:
                    native_parts.append(f"<li>{content}</li>")
                else:
                    native_parts.append(f'<div class="plainLine">{content}</div>')

            native_parts.append("</ul>" if list_mode else "</div>")

        if native_parts:
            native = QtWidgets.QTextBrowser()
            native.setObjectName("changelogSelectableText")
            native.setMaximumWidth(820)
            native.setOpenExternalLinks(True)
            native.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            native.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            native.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            native.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            native.document().setDefaultStyleSheet("""
                body { color:#ded9d3; font-size:13px; margin:0; padding:0; }
                .body { margin:0 0 7px 0; line-height:1.45; }
                ul { margin:0; padding-left:17px; }
                li { margin:3px 0; }
                .plainList { margin:0; }
                .plainLine { margin:3px 0; }
                a { color:#d79a28; text-decoration:none; }
            """)
            native.setHtml("".join(native_parts))
            native.document().documentLayout().documentSizeChanged.connect(
                lambda size, w=native: w.setFixedHeight(max(28, int(size.height()) + 8))
            )
            native.setFixedHeight(max(28, int(native.document().size().height()) + 8))
            lay.addWidget(native)

        # Native media ------------------------------------------------------
        self.dynamicMediaHost = QtWidgets.QWidget()
        self.dynamicMediaHost.setObjectName("dynamicMediaHost")
        self.dynamicMediaLayout = QtWidgets.QVBoxLayout(self.dynamicMediaHost)
        self.dynamicMediaLayout.setContentsMargins(0, 0, 0, 0)
        self.dynamicMediaLayout.setSpacing(0)
        self.dynamicMediaHost.hide()
        lay.addWidget(self.dynamicMediaHost, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

        images = release.get("images", release.get("image", []))
        if isinstance(images, str):
            images = [images]
        if not isinstance(images, list):
            images = []

        resolved = []
        for value in images:
            value = str(value).strip()
            if not value:
                continue
            if "t.me/" in value:
                media_data = _telegram_public_post_data(value)
                for media in media_data.get("images", []):
                    if media and media not in resolved:
                        resolved.append(media)
            else:
                parsed = urllib.parse.urlparse(value)
                if parsed.netloc.casefold() in ("imgur.com", "www.imgur.com"):
                    name = parsed.path.strip("/")
                    if name and "." not in name:
                        value = f"https://i.imgur.com/{name}.png"
                if value not in resolved:
                    resolved.append(value)

        # A Telegram-backed post can populate its media automatically.
        if source_url:
            for media in tg.get("images", []):
                if media and media not in resolved:
                    resolved.append(media)

        video = str(release.get("video", "") or tg.get("video", "")).strip()

        if video:
            player = ChangelogVideoContainer(video, self)
            self.dynamicMediaLayout.addWidget(player)
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))
        elif (
            tg.get("has_video")
            and tg.get("video_poster")
            and source_url
        ):
            target = tg.get("video_reupload") or source_url
            if YoutubeThumbnail._video_id(target):
                media = YoutubeThumbnail(target, self)
            else:
                media = TelegramVideoThumbnail(tg["video_poster"], target, self)
            self.dynamicMediaLayout.addWidget(media)
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))
        elif resolved:
            media = ChangelogMediaCarousel(resolved, self)
            self.dynamicMediaLayout.addWidget(media)
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))
        elif (
            tg.get("video_poster")
            and source_url
        ):
            target = tg.get("video_reupload") or source_url
            if YoutubeThumbnail._video_id(target):
                media = YoutubeThumbnail(target, self)
            else:
                media = TelegramVideoThumbnail(tg["video_poster"], target, self)
            self.dynamicMediaLayout.addWidget(media)
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))

        # Native action buttons ---------------------------------------------
        source_link = str(release.get("link", "")).strip() or source_url
        buttons = QtWidgets.QHBoxLayout()

        if video:
            watch = GlowButton("WATCH VIDEO")
            watch.setObjectName("changelogLinkButton")
            if qta is not None:
                watch.setIcon(qta.icon("fa5s.play", color="#d79a28"))
            watch.clicked.connect(
                lambda checked=False, u=video:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(u))
            )
            buttons.addWidget(watch)

        if source_url and self.telegramView is not None:
            reload_post = GlowButton("RELOAD")
            reload_post.setObjectName("changelogLinkButton")
            if qta is not None:
                reload_post.setIcon(qta.icon("fa5s.sync-alt", color="#d79a28"))
            reload_post.clicked.connect(
                lambda checked=False: self.telegramView.reload_post()
            )
            buttons.addWidget(reload_post)

        if source_link:
            source = GlowButton("OPEN SOURCE")
            source.setObjectName("changelogLinkButton")
            if qta is not None:
                source.setIcon(qta.icon("fa5s.external-link-alt", color="#d79a28"))
            source.clicked.connect(
                lambda checked=False, u=source_link:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(u))
            )
            buttons.addWidget(source)

        buttons.addStretch(1)
        lay.addLayout(buttons)


    def _apply_telegram_media(self, data):
        if not isinstance(data, dict):
            return
        # Explicit JSON media wins over auto-detected Telegram media.
        if self.dynamicMediaHost.isVisible() and self.dynamicMediaLayout.count():
            return

        images = []
        for url in data.get("images", []) or []:
            url = str(url or "").strip()
            if url and url not in images:
                images.append(url)

        video = str(data.get("video", "") or "").strip()
        poster = str(data.get("poster", "") or "").strip()
        reupload = str(data.get("reupload", "") or "").strip()
        if reupload:
            self.telegram_video_reupload = reupload

        if self.dynamicMediaLayout.count():
            current = self.dynamicMediaLayout.itemAt(0).widget()
            if (
                reupload
                and isinstance(current, TelegramVideoThumbnail)
                and YoutubeThumbnail._video_id(reupload)
            ):
                self.dynamicMediaLayout.takeAt(0)
                current.deleteLater()
                self.dynamicMediaLayout.addWidget(YoutubeThumbnail(reupload, self))
            self.dynamicMediaHost.setVisible(
                launcher_settings.get("show_changelog_media", False)
            )
            return

        if (
            launcher_settings.get("show_changelog_media", False)
            and video == "__telegram_post__"
            and poster
            and self.telegram_url
        ):
            target = self.telegram_video_reupload or self.telegram_url
            # If a YouTube reupload was found in Sources, use YouTube's
            # max-resolution thumbnail instead of Telegram's reduced poster.
            yt_id = YoutubeThumbnail._video_id(target)
            if yt_id:
                self.dynamicMediaLayout.addWidget(YoutubeThumbnail(target, self))
            else:
                self.dynamicMediaLayout.addWidget(
                    TelegramVideoThumbnail(poster, target, self)
                )
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))
        elif video and launcher_settings.get("show_changelog_media", False):
            self.dynamicMediaLayout.addWidget(ChangelogVideoContainer(video, self))
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))
        elif images and launcher_settings.get("show_changelog_media", False):
            self.dynamicMediaLayout.addWidget(ChangelogMediaCarousel(images, self))
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))
        elif poster and self.telegram_url:
            self.dynamicMediaLayout.addWidget(
                TelegramVideoThumbnail(poster, self.telegram_url, self)
            )
            self.dynamicMediaHost.setVisible(launcher_settings.get("show_changelog_media", False))

    def eventFilter(self, obj, event):
        if hasattr(self, "serversScroll") and event.type() == QtCore.QEvent.Type.Wheel:
            # Fallback for child widgets such as the levelshot QLabel.
            widget = obj if isinstance(obj, QtWidgets.QWidget) else None
            while widget is not None:
                if widget in getattr(self, "serverCards", []):
                    delta = event.angleDelta().y() or event.angleDelta().x()
                    if delta and len(self.serverCards) > 3:
                        self.scroll_server_monitors(1 if delta < 0 else -1)
                        return True
                    break
                widget = widget.parentWidget()

        if (
            obj in (self.headerWidget,)
            or obj.parentWidget() is self.headerWidget
        ):
            if (
                obj is not self.toggleButton
                and event.type() == QtCore.QEvent.Type.MouseButtonRelease
                and event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                self._toggle()
                return True
        return super().eventFilter(obj, event)

    def _update_toggle_icon(self):
        if qta is not None:
            name = "fa5s.chevron-up" if self._expanded else "fa5s.chevron-down"
            self.toggleButton.setIcon(qta.icon(name, color="#b9b4ae"))
            self.toggleButton.setIconSize(QtCore.QSize(11, 11))
        else:
            self.toggleButton.setText("▲" if self._expanded else "▼")

    def apply_media_preview_preference(self, enabled):
        """Apply media visibility without destroying/rebuilding Telegram WebEngine cards."""
        enabled = bool(enabled)
        self.dynamicMediaHost.setVisible(enabled and self.dynamicMediaLayout.count() > 0)
        if enabled and self.telegramView is not None:
            QtCore.QTimer.singleShot(0, self.telegramView.emit_media_now)
            QtCore.QTimer.singleShot(180, self.telegramView.emit_media_now)

    def _toggle(self):
        self._expanded = not self._expanded
        self.bodyWidget.setVisible(self._expanded)
        self._update_toggle_icon()

        if self._expanded:
            # Cards #4+ start hidden. Telegram/WebEngine may have measured itself
            # while hidden, leaving the stale viewport-sized gap above its media.
            # Re-measure only after the body is actually visible; no network reload.
            if self.telegramView is not None:
                self.telegramView.refresh_after_expand()

            # Media can also have been created while its parent was hidden.
            # Force Qt to discard those stale size hints immediately.
            self.dynamicMediaHost.updateGeometry()
            if self.dynamicMediaHost.layout() is not None:
                self.dynamicMediaHost.layout().invalidate()
                self.dynamicMediaHost.layout().activate()

            self.bodyWidget.updateGeometry()
            if self.bodyWidget.layout() is not None:
                self.bodyWidget.layout().invalidate()
                self.bodyWidget.layout().activate()

            # One more parent-card pass after the show event is processed.
            QtCore.QTimer.singleShot(0, self._refresh_expanded_geometry)
            QtCore.QTimer.singleShot(180, self._refresh_expanded_geometry)

    def _refresh_expanded_geometry(self):
        if not self._expanded:
            return

        for widget in (self.telegramView, self.dynamicMediaHost, self.bodyWidget, self):
            if widget is None:
                continue
            widget.updateGeometry()
            layout = widget.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()

        parent = self.parentWidget()
        depth = 0
        while parent is not None and depth < 6:
            parent.updateGeometry()
            layout = parent.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            parent = parent.parentWidget()
            depth += 1




class ServerQueryWorker(QtCore.QThread):
    serverReady = QtCore.pyqtSignal(int, object)
    batchFinished = QtCore.pyqtSignal()

    def __init__(self, servers, parent=None):
        super().__init__(parent)
        self.servers = list(servers)

    def run(self):
        for index, server in enumerate(self.servers):
            result = server_monitor.query_server(server.get("address", ""))
            result["configured_name"] = server.get("name", "")
            result["builtin"] = bool(server.get("builtin", False))
            self.serverReady.emit(index, result)
        self.batchFinished.emit()


class ServerPlayerTable(QtWidgets.QTreeWidget):
    wheelRequested = QtCore.pyqtSignal(int)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.wheelRequested.emit(1 if delta < 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


class ServerCard(QtWidgets.QFrame):
    connectRequested = QtCore.pyqtSignal(str)
    copyRequested = QtCore.pyqtSignal(str)
    removeRequested = QtCore.pyqtSignal(str)
    moveRequested = QtCore.pyqtSignal(str, int)
    wheelRequested = QtCore.pyqtSignal(int)

    def __init__(self, server, levelshots_dir, parent=None):
        super().__init__(parent)
        self.server = dict(server)
        self.levelshots_dir = Path(levelshots_dir)
        self.setObjectName("serverCard")
        self.setFixedWidth(320)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        self.nameLabel = QtWidgets.QLabel(server.get("name") or server.get("address", "Server"))
        self.nameLabel.setObjectName("serverName")
        top.addWidget(self.nameLabel, 1)
        self.stateLabel = QtWidgets.QLabel("● CHECKING")
        self.stateLabel.setObjectName("serverState")
        top.addWidget(self.stateLabel)
        lay.addLayout(top)

        self.addressLabel = QtWidgets.QLabel(server.get("address", ""))
        self.addressLabel.setObjectName("serverAddress")
        lay.addWidget(self.addressLabel)

        self.levelshot = QtWidgets.QLabel("NO LEVELSHOT")
        self.levelshot.setObjectName("serverLevelshot")
        self.levelshot.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.levelshot.setFixedSize(300, 169)
        self.levelshot.setScaledContents(False)
        lay.addWidget(self.levelshot)

        info = QtWidgets.QHBoxLayout()
        self.mapLabel = QtWidgets.QLabel("MAP  —")
        self.mapLabel.setObjectName("serverMap")
        info.addWidget(self.mapLabel, 1)
        self.pingLabel = QtWidgets.QLabel("PING  —")
        self.pingLabel.setObjectName("serverPing")
        info.addWidget(self.pingLabel)
        lay.addLayout(info)

        self.playersLabel = QtWidgets.QLabel("PLAYERS  — / —")
        self.playersLabel.setObjectName("serverPlayers")
        lay.addWidget(self.playersLabel)

        self.playerList = ServerPlayerTable()
        self.playerList.setObjectName("serverPlayerList")
        self.playerList.setColumnCount(3)
        self.playerList.setHeaderLabels(["PLAYER", "SCORE", "PING"])
        self.playerList.setRootIsDecorated(False)
        self.playerList.setIndentation(0)
        self.playerList.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.playerList.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.playerList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.playerList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.playerList.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.playerList.setColumnWidth(1, 54)
        self.playerList.setColumnWidth(2, 74)
        self.playerList.headerItem().setTextAlignment(1, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
        self.playerList.headerItem().setTextAlignment(2, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
        self.playerList.setMinimumHeight(110)
        self.playerList.wheelRequested.connect(self.wheelRequested.emit)
        lay.addWidget(self.playerList, 1)

        self.editActions = QtWidgets.QWidget()
        edit_l = QtWidgets.QHBoxLayout(self.editActions)
        edit_l.setContentsMargins(0, 0, 0, 0)
        edit_l.setSpacing(6)
        left_btn = GlowButton("◀  MOVE")
        left_btn.setObjectName("serverEditButton")
        left_btn.clicked.connect(lambda: self.moveRequested.emit(self.server.get("address", ""), -1))
        edit_l.addWidget(left_btn)
        right_btn = GlowButton("MOVE  ▶")
        right_btn.setObjectName("serverEditButton")
        right_btn.clicked.connect(lambda: self.moveRequested.emit(self.server.get("address", ""), 1))
        edit_l.addWidget(right_btn)
        edit_l.addStretch(1)
        remove_btn = GlowButton("REMOVE")
        remove_btn.setObjectName("serverRemoveButton")
        remove_btn.clicked.connect(lambda: self.removeRequested.emit(self.server.get("address", "")))
        edit_l.addWidget(remove_btn)
        self.editActions.hide()
        lay.addWidget(self.editActions)

        actions = QtWidgets.QHBoxLayout()
        copy_btn = GlowButton("COPY ADDRESS")
        copy_btn.setObjectName("serverSmallButton")
        copy_btn.clicked.connect(lambda: self.copyRequested.emit(self.server.get("address", "")))
        actions.addWidget(copy_btn)
        actions.addStretch(1)
        connect_btn = GlowButton("▶  CONNECT")
        connect_btn.setObjectName("serverConnectButton")
        connect_btn.clicked.connect(lambda: self.connectRequested.emit(self.server.get("address", "")))
        actions.addWidget(connect_btn)
        lay.addLayout(actions)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.wheelRequested.emit(1 if delta < 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def set_edit_mode(self, enabled):
        self.editActions.setVisible(bool(enabled))

    def _set_levelshot(self, mapname):
        safe = str(mapname or "").strip()
        candidates = []
        if safe:
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                candidates.append(self.levelshots_dir / (safe + ext))
                candidates.append(self.levelshots_dir / (safe.lower() + ext))
        candidates.append(self.levelshots_dir / "unknown.png")
        path = next((p for p in candidates if p.is_file()), None)
        if not path:
            self.levelshot.setPixmap(QtGui.QPixmap())
            self.levelshot.setText((safe or "UNKNOWN MAP").upper())
            return
        pix = QtGui.QPixmap(str(path))
        if pix.isNull():
            return
        scaled = pix.scaled(self.levelshot.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            QtCore.Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - self.levelshot.width()) // 2)
        y = max(0, (scaled.height() - self.levelshot.height()) // 2)
        self.levelshot.setText("")
        self.levelshot.setPixmap(scaled.copy(x, y, self.levelshot.width(), self.levelshot.height()))

    def apply_result(self, result):
        self.server.update(result)
        online = bool(result.get("online"))
        self.stateLabel.setText("● ONLINE" if online else "● OFFLINE")
        self.stateLabel.setProperty("online", online)
        self.stateLabel.style().unpolish(self.stateLabel)
        self.stateLabel.style().polish(self.stateLabel)

        if not online:
            self.pingLabel.setText("PING  —")
            self.mapLabel.setText("MAP  —")
            self.playersLabel.setText("PLAYERS  0 / —")
            self.playerList.clear()
            self.playerList.addTopLevelItem(QtWidgets.QTreeWidgetItem(["Server did not respond.", "", ""]))
            self._set_levelshot("")
            return

        hostname = str(result.get("hostname", "")).strip()
        if hostname:
            self.nameLabel.setText(hostname)
        mapname = str(result.get("mapname", "")).strip()
        self.mapLabel.setText(f"MAP  {mapname or '—'}")
        self.pingLabel.setText(f"PING  {result.get('ping', '—')} ms")
        self.playersLabel.setText(f"PLAYERS  {result.get('clients', 0)} / {result.get('maxclients', 0) or '—'}")
        players = result.get("players", [])
        self.playerList.clear()
        if players:
            for p in players:
                item = QtWidgets.QTreeWidgetItem([str(p.get("name","")), str(p.get("score",0)), f'{p.get("ping",0)} ms'])
                item.setTextAlignment(1, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
                item.setTextAlignment(2, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
                self.playerList.addTopLevelItem(item)
        else:
            self.playerList.addTopLevelItem(QtWidgets.QTreeWidgetItem(["Server is empty.", "", ""]))
        self._set_levelshot(mapname)


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

        icon_path = APP_ICON_ICO if APP_ICON_ICO.is_file() else APP_ICON_PNG
        if icon_path.is_file():
            self.setWindowIcon(QtGui.QIcon(str(icon_path)))

        self.trayIcon = QtWidgets.QSystemTrayIcon(self.windowIcon(), self)
        tray_menu = QtWidgets.QMenu(self)
        tray_show = tray_menu.addAction("Open Q3Elite Launcher")
        tray_show.triggered.connect(self.restore_from_tray)
        tray_menu.addSeparator()
        tray_exit = tray_menu.addAction("Exit")
        tray_exit.triggered.connect(self.exit_from_tray)
        self.trayIcon.setContextMenu(tray_menu)
        self.trayIcon.activated.connect(self._tray_activated)
        self._allow_close = False

        self._drag_pos = None
        self.pending_component_actions = []
        self._component_baseline = {}

        self.shell = GothicShell(self)
        self.shell.setObjectName("shell")
        self.shell.setGeometry(8, 8, 1352, 752)

        root = QtWidgets.QHBoxLayout(self.shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # LEFT RAIL ---------------------------------------------------------
        self.sidebar = GlassFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(224)
        side = QtWidgets.QVBoxLayout(self.sidebar)
        side.setContentsMargins(22, 24, 22, 22)
        side.setSpacing(10)

        brand = QtWidgets.QLabel("Q3<span style='color:#a40000'>ELITE</span>")
        brand.setObjectName("brand")
        brand.setTextFormat(QtCore.Qt.TextFormat.RichText)
        side.addWidget(brand)

        sub = QtWidgets.QLabel("QUAKE 3 ARENA COMPILATION")
        sub.setObjectName("brandSub")
        side.addWidget(sub)
        side.addSpacing(34)

        self.homeNav = self._nav_button("HOME", "home", "fa5s.home")
        self.addonsNav = self._nav_button("INSTALL ADDONS", "addons", "fa5s.puzzle-piece")
        self.statisticsNav = self._nav_button("STATISTICS", "statistics", "fa5s.chart-bar")
        self.serversNav = self._nav_button("SERVERS", "servers", "fa5s.server")
        self.settingsNav = self._nav_button("SETTINGS", "settings", "fa5s.cog")
        self.changelogNav = self._nav_button("CHANGELOG", "changelog", "fa5s.scroll")
        for button in (self.homeNav, self.addonsNav, self.statisticsNav, self.serversNav, self.settingsNav, self.changelogNav):
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

        self.launcherVersion = QtWidgets.QLabel(f"Launcher v{read_launcher_metadata().get('version', '—')}")
        self.launcherVersion.setObjectName("versionLabel")
        top.addWidget(self.launcherVersion)

        self.minButton = GlowButton("—")
        self.minButton.setObjectName("windowButton")
        self.minButton.setFixedSize(42, 36)
        self.minButton.clicked.connect(self.minimize_launcher)
        top.addWidget(self.minButton)

        self.closeButton = GlowButton("")
        self.closeButton.setObjectName("closeButton")
        self.closeButton.setFixedSize(42, 36)
        if qta is not None:
            self.closeButton.setIcon(qta.icon("fa5s.times", color="#aaaaaa"))
            self.closeButton.setIconSize(QtCore.QSize(15, 15))
        else:
            self.closeButton.setText("×")
        self.closeButton.clicked.connect(self.close)
        top.addWidget(self.closeButton)
        body_layout.addLayout(top)

        self.pages = QtWidgets.QStackedWidget()
        self.pages.setObjectName("pages")
        self.homePage = self._build_home()
        self.addonsPage = self._build_addons()
        self.statisticsPage = self._build_statistics()
        self.serversPage = self._build_servers()
        self.settingsPage = self._build_settings()
        self.changelogPage = self._build_changelog()
        for page in (self.homePage, self.addonsPage, self.statisticsPage, self.serversPage, self.settingsPage, self.changelogPage):
            self.pages.addWidget(page)
        body_layout.addWidget(self.pages, 1)
        root.addWidget(body, 1)

        self.show_page("home")
        self._load_component_state_initial()
        self.load_settings_ui()

        # Screenshots are remote; only the launcher background is stored locally.
        self._hero_local = []
        self._hero_urls = [
            "https://i.imgur.com/2fRzLTO.png",
            "https://i.imgur.com/LdHeuau.png",
            "https://i.imgur.com/GF6zwPt.png",
            "https://i.imgur.com/cgYryat.png",
            "https://i.imgur.com/mWc8Kq2.png",
            "https://i.imgur.com/YNBOXne.png",
            "https://i.imgur.com/UQNArcD.png",
            "https://i.imgur.com/QosQqFM.png",
            "https://i.imgur.com/5fOcRHo.png",
            "https://i.imgur.com/UQ7J66U.png",
            "https://i.imgur.com/U8UN1dj.png",
            "https://i.imgur.com/Jmj7Ftm.png",
            "https://i.imgur.com/oGslbD6.png",
            "https://i.imgur.com/YfwCXyw.png",
            "https://i.imgur.com/qTw4JRT.png",
            "https://i.imgur.com/XMOCcCe.png",
        ]
        self._hero_index = 0
        self._hero_pixmaps = {}
        self._network = QtNetwork.QNetworkAccessManager(self)
        self.load_hero_image()

    def _nav_button(self, text, page, icon_name=None):
        b = GlowButton(text)
        if qta is not None and icon_name:
            try:
                b.setIcon(qta.icon(icon_name, color='#b8b8b8', color_active='#b00000'))
                b.setIconSize(QtCore.QSize(18,18))
            except Exception:
                pass
        b.setObjectName("navButton")
        b.setCheckable(True)
        b.setProperty("page", page)
        b.clicked.connect(lambda checked=False, p=page: self.show_page(p))
        return b

    def _card(self, name="card"):
        f = GlassFrame()
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
        hero_l.setContentsMargins(0, 0, 0, 0)
        hero_l.setSpacing(0)

        self.heroImage = QtWidgets.QLabel("Loading screenshot...")
        self.heroImage.setObjectName("heroImage")
        self.heroImage.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.heroImage.setMinimumHeight(255)
        self.heroImage.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        hero_l.addWidget(self.heroImage, 1)

        hero_controls = QtWidgets.QFrame()
        hero_controls.setObjectName("heroOverlay")
        hc = QtWidgets.QHBoxLayout(hero_controls)
        hc.setContentsMargins(22, 11, 16, 11)

        hero_text = QtWidgets.QVBoxLayout()
        hero_brand_row = QtWidgets.QHBoxLayout()
        hero_brand_row.setSpacing(8)
        hero_brand_row.addWidget(AnimatedEmoji("HorrorEye", 32))
        kicker = QtWidgets.QLabel("QUAKE 3 ELITE")
        hero_brand_row.addWidget(kicker)
        hero_brand_row.addStretch(1)
        kicker.setObjectName("heroKicker")
        hero_title = QtWidgets.QLabel("RELOADED FOR A NEW ERA")
        hero_title.setObjectName("heroTitle")
        hero_text.addLayout(hero_brand_row)
        hero_text.addWidget(hero_title)
        hc.addLayout(hero_text, 1)

        self.heroCounter = QtWidgets.QLabel("01 / 16")
        self.heroCounter.setObjectName("heroCounter")
        hc.addWidget(self.heroCounter)

        self.heroPrev = QtWidgets.QPushButton("")
        self.heroPrev.setObjectName("sliderArrow")
        self.heroPrev.setFixedSize(38, 38)
        if qta is not None:
            self.heroPrev.setIcon(qta.icon("fa5s.chevron-left", color="#b9b4ae"))
            self.heroPrev.setIconSize(QtCore.QSize(13, 13))
        else:
            self.heroPrev.setText("‹")
        self.heroPrev.clicked.connect(lambda: self.change_hero_image(-1))
        hc.addWidget(self.heroPrev)

        self.heroNext = QtWidgets.QPushButton("")
        self.heroNext.setObjectName("sliderArrow")
        self.heroNext.setFixedSize(38, 38)
        if qta is not None:
            self.heroNext.setIcon(qta.icon("fa5s.chevron-right", color="#b9b4ae"))
            self.heroNext.setIconSize(QtCore.QSize(13, 13))
        else:
            self.heroNext.setText("›")
        self.heroNext.clicked.connect(lambda: self.change_hero_image(1))
        hc.addWidget(self.heroNext)

        hero_l.addWidget(hero_controls)
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

        self.firstInstallCard = self._card("firstInstallCard")
        fic = QtWidgets.QHBoxLayout(self.firstInstallCard)
        fic.setContentsMargins(18, 12, 18, 12)
        fic.setSpacing(16)

        install_text = QtWidgets.QVBoxLayout()
        fit = QtWidgets.QLabel("INSTALL OPTIONS")
        fit.setObjectName("sectionTitle")
        install_text.addWidget(fit)
        fis = QtWidgets.QLabel("Optional content can also be installed later from INSTALL ADDONS.")
        fis.setObjectName("muted")
        install_text.addWidget(fis)
        fic.addLayout(install_text, 1)

        self.firstInstallMapsBox = QtWidgets.QCheckBox("External Maps")
        self.firstInstallMusicBox = QtWidgets.QCheckBox("Music Playlist")
        fic.addWidget(self.firstInstallMapsBox)
        fic.addWidget(self.firstInstallMusicBox)

        self.firstInstallCard.setVisible(not q3elite_is_installed())
        layout.addWidget(self.firstInstallCard)

        action = QtWidgets.QHBoxLayout()
        self.playButton = GlowButton("CHECKING...")
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
        lay.addWidget(self._addon_row("External Maps", "Community map collection • cached ZIP retained", self.mapsBox))
        lay.addWidget(self._addon_row("Music Playlist", "Extended Q3Elite music collection • cached ZIP retained", self.musicBox))

        autoexec_card = self._card("addonCard")
        autoexec_l = QtWidgets.QHBoxLayout(autoexec_card)
        autoexec_t = QtWidgets.QVBoxLayout()
        autoexec_title = QtWidgets.QLabel("Autoexec Update")
        autoexec_title.setObjectName("addonTitle")
        autoexec_t.addWidget(autoexec_title)
        autoexec_t.addWidget(QtWidgets.QLabel("Synchronize the distributed autoexec.cfg once"))
        autoexec_l.addLayout(autoexec_t, 1)
        self.autoexecUpdateButton = GlowButton("UPDATE AUTOEXEC")
        self.autoexecUpdateButton.setObjectName("applyButton")
        self.autoexecUpdateButton.clicked.connect(lambda: start_component_action("update-autoexec"))
        self.autoexecUpdateButton.setEnabled(q3elite_is_installed())
        autoexec_l.addWidget(self.autoexecUpdateButton)
        lay.addWidget(autoexec_card)
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
        self.applyAddonsButton = GlowButton("APPLY CHANGES")
        self.applyAddonsButton.setObjectName("applyButton")
        self.applyAddonsButton.clicked.connect(apply_component_changes)
        self.applyAddonsButton.setEnabled(q3elite_is_installed())
        buttons.addWidget(self.applyAddonsButton)
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
        self.startWindowsBox = QtWidgets.QCheckBox("Start with Windows")
        self.trayBox = QtWidgets.QCheckBox("Minimize to Windows system tray")
        self.vulkanLayerBox = QtWidgets.QCheckBox("Enable ReShade for Vulkan")

        changelog_label = QtWidgets.QLabel("[Changelogs]")
        changelog_label.setObjectName("sectionTitle")
        c.addSpacing(10)
        c.addWidget(changelog_label)
        self.changelogMediaBox = QtWidgets.QCheckBox("Show screenshots in changelog")

        servers_label = QtWidgets.QLabel("[Servers]")
        servers_label.setObjectName("sectionTitle")
        self.pauseServerRefreshBox = QtWidgets.QCheckBox("Do not refresh servers while Launcher is out of focus")

        cleanup_label = QtWidgets.QLabel("TEMPORARY FILE CLEANUP")
        cleanup_label.setObjectName("sectionTitle")
        c.addSpacing(10)
        c.addWidget(cleanup_label)

        def make_cleanup_row(label_text, combo_name, spin_name):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)

            checkbox = QtWidgets.QCheckBox(label_text)
            checkbox.setMinimumWidth(170)

            combo = QtWidgets.QComboBox()
            combo.setObjectName(combo_name)
            combo.addItem("1 day", 1)
            combo.addItem("3 days", 3)
            combo.addItem("7 days", 7)
            combo.addItem("30 days", 30)
            combo.addItem("Custom", -1)
            combo.setFixedWidth(125)
            combo.setEnabled(False)

            custom = QtWidgets.QSpinBox()
            custom.setObjectName(spin_name)
            custom.setRange(1, 3650)
            custom.setSuffix(" days")
            custom.setValue(14)
            custom.setFixedWidth(110)
            custom.setVisible(False)
            custom.setEnabled(False)

            def refresh():
                enabled = checkbox.isChecked()
                combo.setEnabled(enabled)
                custom.setEnabled(enabled)
                custom.setVisible(enabled and combo.currentData() == -1)

            checkbox.toggled.connect(refresh)
            combo.currentIndexChanged.connect(refresh)

            row.addWidget(checkbox)
            row.addWidget(combo)
            row.addWidget(custom)
            row.addStretch(1)
            c.addLayout(row)
            return checkbox, combo, custom

        (
            self.cleanupScreenshotsBox,
            self.cleanupScreenshotsCombo,
            self.cleanupScreenshotsCustom,
        ) = make_cleanup_row(
            "Clean screenshots",
            "cleanupScreenshotsCombo",
            "cleanupScreenshotsCustom",
        )

        (
            self.cleanupDemosBox,
            self.cleanupDemosCombo,
            self.cleanupDemosCustom,
        ) = make_cleanup_row(
            "Clean demos",
            "cleanupDemosCombo",
            "cleanupDemosCustom",
        )

        cleanup_hint = QtWidgets.QLabel(
            "Screenshots: OSP screenshots + Q3Elite screenshots.  Demos: OSP demos."
        )
        cleanup_hint.setObjectName("settingsHint")
        cleanup_hint.setWordWrap(True)
        c.addWidget(cleanup_hint)

        for box in (
            self.autoQ3Box, self.autoLauncherBox, self.autoOspBox,
            self.startWindowsBox, self.trayBox,
            self.vulkanLayerBox, self.changelogMediaBox,
        ):
            c.addWidget(box)
        c.addSpacing(10)
        c.addWidget(servers_label)
        c.addWidget(self.pauseServerRefreshBox)
        lay.addWidget(card)

        cache = self._card("settingsCard")
        cc = QtWidgets.QVBoxLayout(cache)
        cache_title = QtWidgets.QLabel("DOWNLOAD CACHE")
        cache_title.setObjectName("sectionTitle")
        cc.addWidget(cache_title)
        cache_row = QtWidgets.QHBoxLayout()
        cp = QtWidgets.QLabel(str(CACHE_DIR))
        cp.setObjectName("muted")
        cp.setWordWrap(True)
        cache_row.addWidget(cp, 1)
        open_cache = QtWidgets.QPushButton("📂")
        open_cache.setObjectName("smallButton")
        open_cache.setToolTip("Open Cache folder")
        open_cache.setFixedWidth(48)
        open_cache.clicked.connect(self.open_cache_folder)
        cache_row.addWidget(open_cache)
        cc.addLayout(cache_row)
        lay.addWidget(cache)

        config_card = self._card("settingsCard")
        config_l = QtWidgets.QHBoxLayout(config_card)
        config_text = QtWidgets.QVBoxLayout()
        config_title = QtWidgets.QLabel("CONFIGURATION")
        config_title.setObjectName("sectionTitle")
        config_text.addWidget(config_title)
        config_text.addWidget(QtWidgets.QLabel("View autoexec.cfg and edit UserConfig.cfg"))
        config_l.addLayout(config_text, 1)
        self.configEditorButton = GlowButton("OPEN CONFIG EDITOR")
        self.configEditorButton.setObjectName("applyButton")
        self.configEditorButton.clicked.connect(self.open_config_editor)
        self.configEditorButton.setEnabled(config_editor_available())
        config_l.addWidget(self.configEditorButton)
        lay.addWidget(config_card)
        lay.addStretch(1)

        self.settingsMessage = QtWidgets.QLabel("")
        self.settingsMessage.setObjectName("message")
        lay.addWidget(self.settingsMessage)
        apply = GlowButton("SAVE SETTINGS")
        apply.setObjectName("applyButton")
        apply.clicked.connect(apply_settings)
        lay.addWidget(apply, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        return page

    # ------------------------------------------------------------------
    # STATISTICS — experimental UI / API adapter
    # ------------------------------------------------------------------
    def _build_statistics(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("STATISTICS")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        hint = QtWidgets.QLabel("Q3MSK  •  PLAYER LOOKUP  •  EXPERIMENTAL")
        hint.setObjectName("muted")
        header.addWidget(hint)
        root.addLayout(header)

        search_card = QtWidgets.QFrame()
        search_card.setObjectName("statisticsSearchCard")
        search_l = QtWidgets.QVBoxLayout(search_card)
        search_l.setContentsMargins(18, 15, 18, 15)
        search_l.setSpacing(9)

        search_title = QtWidgets.QLabel("FIND PLAYER")
        search_title.setObjectName("sectionTitle")
        search_l.addWidget(search_title)

        search_row = QtWidgets.QHBoxLayout()
        search_row.setSpacing(10)
        self.statisticsNickname = QtWidgets.QLineEdit()
        self.statisticsNickname.setObjectName("statisticsNickname")
        self.statisticsNickname.setPlaceholderText("Enter nickname — e.g. Mus1n")
        self.statisticsNickname.setClearButtonEnabled(True)
        self.statisticsNickname.returnPressed.connect(self.lookup_statistics)
        search_row.addWidget(self.statisticsNickname, 1)

        self.statisticsSearchButton = GlowButton("SEARCH")
        self.statisticsSearchButton.setObjectName("applyButton")
        self.statisticsSearchButton.setFixedWidth(145)
        self.statisticsSearchButton.clicked.connect(self.lookup_statistics)
        search_row.addWidget(self.statisticsSearchButton)
        search_l.addLayout(search_row)

        self.statisticsMessage = QtWidgets.QLabel(
            "Nickname search is ready for the upcoming statistics API. "
            "Mus1n currently loads local preview data so the launcher UI can be tested."
        )
        self.statisticsMessage.setObjectName("statisticsMessage")
        self.statisticsMessage.setWordWrap(True)
        search_l.addWidget(self.statisticsMessage)
        root.addWidget(search_card)

        self.statisticsResults = QtWidgets.QScrollArea()
        self.statisticsResults.setObjectName("statisticsScroll")
        self.statisticsResults.setWidgetResizable(True)
        self.statisticsResults.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.statisticsResults.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.statisticsContent = QtWidgets.QWidget()
        self.statisticsContent.setObjectName("statisticsContent")
        self.statisticsLayout = QtWidgets.QVBoxLayout(self.statisticsContent)
        self.statisticsLayout.setContentsMargins(0, 0, 8, 0)
        self.statisticsLayout.setSpacing(11)
        self.statisticsResults.setWidget(self.statisticsContent)
        root.addWidget(self.statisticsResults, 1)

        self._show_statistics_empty()
        return page

    @staticmethod
    def _statistics_clean_nickname(value):
        # Quake 3 color sequences are not part of the searchable nickname.
        return re.sub(r"\^[0-9A-Za-z]", "", str(value or "")).strip()

    @staticmethod
    def _statistics_preview_payload(nickname):
        """Temporary local payload. Replace only this adapter when the API is ready."""
        clean = ModernLauncherWindow._statistics_clean_nickname(nickname)
        if not clean.casefold().startswith("mus1n"):
            return None
        return {
            "nickname": clean or "Mus1n",
            "matches": 576,
            "kills": 10994,
            "deaths": 5624,
            "kd": 1.955,
            "thaws": 4252,
            "unfreezes": 1946,
            "suicides": 166,
            "damage_given": 3311148,
            "damage_received": 2230404,
            "armor": 283730,
            "health": 249380,
            "yellow_armor": 2474,
            "red_armor": 1412,
            "mega": 1208,
            # Experimental weapon preview. The real API will replace these rows.
            # Keep the schema simple: hits / attempts / kills / deaths.
            "weapons": [
                {"weapon": "Gauntlet", "hits": 0, "attempts": 0, "kills": 44, "deaths": 17},
                {"weapon": "Machinegun", "hits": 4821, "attempts": 14852, "kills": 423, "deaths": 212},
                {"weapon": "Shotgun", "hits": 6842, "attempts": 16731, "kills": 1256, "deaths": 593},
                {"weapon": "Grenade Launcher", "hits": 1158, "attempts": 5126, "kills": 304, "deaths": 132},
                {"weapon": "Rocket Launcher", "hits": 12574, "attempts": 28942, "kills": 3558, "deaths": 1867},
                {"weapon": "Lightning Gun", "hits": 52761, "attempts": 173984, "kills": 2491, "deaths": 1204},
                {"weapon": "Railgun", "hits": 9318, "attempts": 21706, "kills": 2045, "deaths": 1098},
                {"weapon": "Plasma Gun", "hits": 15933, "attempts": 61218, "kills": 647, "deaths": 328},
                {"weapon": "BFG", "hits": 86, "attempts": 241, "kills": 72, "deaths": 31},
                {"weapon": "Grappling Hook", "hits": 0, "attempts": 0, "kills": 0, "deaths": 0},
            ],
        }

    def _clear_statistics_results(self):
        while self.statisticsLayout.count():
            item = self.statisticsLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_statistics_empty(self, text="Search for a player to display statistics."):
        self._clear_statistics_results()
        empty = QtWidgets.QFrame()
        empty.setObjectName("statisticsEmpty")
        lay = QtWidgets.QVBoxLayout(empty)
        lay.setContentsMargins(22, 34, 22, 34)
        lay.setSpacing(7)
        icon = QtWidgets.QLabel("⌁")
        icon.setObjectName("statisticsEmptyIcon")
        icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)
        label = QtWidgets.QLabel(text)
        label.setObjectName("statisticsEmptyText")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        lay.addWidget(label)
        self.statisticsLayout.addWidget(empty)
        self.statisticsLayout.addStretch(1)

    def lookup_statistics(self):
        nickname = self.statisticsNickname.text().strip()
        if not nickname:
            self.statisticsMessage.setText("Enter a nickname first.")
            self.statisticsNickname.setFocus()
            return

        self.statisticsSearchButton.setEnabled(False)
        self.statisticsSearchButton.setText("SEARCHING...")
        QtWidgets.QApplication.processEvents()
        try:
            payload = self._statistics_preview_payload(nickname)
            if payload is None:
                self.statisticsMessage.setText(
                    "The live statistics API is not connected yet. "
                    "For now, use Mus1n to test the finished statistics layout."
                )
                self._show_statistics_empty("No preview data for this nickname yet.")
                return
            self.statisticsMessage.setText(
                "Preview data loaded. The UI is API-ready; only the data adapter will be replaced."
            )
            self._render_statistics(payload)
        finally:
            self.statisticsSearchButton.setText("SEARCH")
            self.statisticsSearchButton.setEnabled(True)

    def _stat_tile(self, label, value, accent=False):
        tile = QtWidgets.QFrame()
        tile.setObjectName("statisticsTileAccent" if accent else "statisticsTile")
        lay = QtWidgets.QVBoxLayout(tile)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        name = QtWidgets.QLabel(label.upper())
        name.setObjectName("statisticsStatName")
        value_label = QtWidgets.QLabel(str(value))
        value_label.setObjectName("statisticsStatValue")
        lay.addWidget(name)
        lay.addWidget(value_label)
        return tile

    def _statistics_tab_button(self, text):
        button = QtWidgets.QPushButton(text)
        button.setObjectName("statisticsTabButton")
        button.setCheckable(True)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setMinimumWidth(130)
        return button

    def _render_statistics(self, data):
        self._clear_statistics_results()

        profile = QtWidgets.QFrame()
        profile.setObjectName("statisticsProfileCard")
        profile_l = QtWidgets.QHBoxLayout(profile)
        profile_l.setContentsMargins(18, 14, 18, 14)
        profile_l.setSpacing(14)

        identity = QtWidgets.QVBoxLayout()
        kicker = QtWidgets.QLabel("PLAYER")
        kicker.setObjectName("sectionTitle")
        identity.addWidget(kicker)
        nick = QtWidgets.QLabel(str(data.get("nickname", "Unknown")))
        nick.setObjectName("statisticsPlayerName")
        identity.addWidget(nick)
        profile_l.addLayout(identity, 1)

        matches = QtWidgets.QVBoxLayout()
        matches.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        matches_label = QtWidgets.QLabel("MATCHES")
        matches_label.setObjectName("statisticsStatName")
        matches_value = QtWidgets.QLabel(f"{int(data.get('matches', 0)):,}")
        matches_value.setObjectName("statisticsMatchesValue")
        matches_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        matches_value.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        matches.addWidget(matches_label)
        matches.addWidget(matches_value)
        profile_l.addLayout(matches)
        self.statisticsLayout.addWidget(profile)

        # Two result tabs: general overview + weapon accuracy.
        tabs = QtWidgets.QHBoxLayout()
        tabs.setSpacing(6)
        self.statisticsOverviewTab = self._statistics_tab_button("OVERVIEW")
        self.statisticsAccuracyTab = self._statistics_tab_button("WEAPON ACCURACY")
        self.statisticsOverviewTab.setChecked(True)
        tabs.addWidget(self.statisticsOverviewTab)
        tabs.addWidget(self.statisticsAccuracyTab)
        tabs.addStretch(1)
        self.statisticsLayout.addLayout(tabs)

        self.statisticsTabs = QtWidgets.QStackedWidget()
        self.statisticsTabs.setObjectName("statisticsTabs")
        self.statisticsTabs.addWidget(self._build_statistics_overview(data))
        self.statisticsTabs.addWidget(self._build_statistics_accuracy(data))
        self.statisticsLayout.addWidget(self.statisticsTabs)

        def select_tab(index):
            self.statisticsTabs.setCurrentIndex(index)
            self.statisticsOverviewTab.setChecked(index == 0)
            self.statisticsAccuracyTab.setChecked(index == 1)

        self.statisticsOverviewTab.clicked.connect(lambda: select_tab(0))
        self.statisticsAccuracyTab.clicked.connect(lambda: select_tab(1))
        self.statisticsLayout.addStretch(1)

    def _build_statistics_overview(self, data):
        page = QtWidgets.QWidget()
        page.setObjectName("statisticsTabPage")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(10)

        combat_title = QtWidgets.QLabel("COMBAT")
        combat_title.setObjectName("statisticsGroupTitle")
        lay.addWidget(combat_title)
        combat = QtWidgets.QGridLayout()
        combat.setHorizontalSpacing(10)
        combat.setVerticalSpacing(10)
        combat_values = [
            ("Kills", f"{int(data.get('kills', 0)):,}", True),
            ("Deaths", f"{int(data.get('deaths', 0)):,}", False),
            ("K / D", f"{float(data.get('kd', 0.0)):.3f}", True),
            ("Thaws", f"{int(data.get('thaws', 0)):,}", False),
            ("Unfreezes", f"{int(data.get('unfreezes', 0)):,}", False),
            ("Suicides", f"{int(data.get('suicides', 0)):,}", False),
        ]
        for i, (label, value, accent) in enumerate(combat_values):
            combat.addWidget(self._stat_tile(label, value, accent), i // 3, i % 3)
        lay.addLayout(combat)

        performance_title = QtWidgets.QLabel("DAMAGE & PICKUPS")
        performance_title.setObjectName("statisticsGroupTitle")
        lay.addWidget(performance_title)
        performance = QtWidgets.QGridLayout()
        performance.setHorizontalSpacing(10)
        performance.setVerticalSpacing(10)
        performance_values = [
            ("Damage given", f"{int(data.get('damage_given', 0)):,}"),
            ("Damage received", f"{int(data.get('damage_received', 0)):,}"),
            ("Armor taken", f"{int(data.get('armor', 0)):,}"),
            ("Health taken", f"{int(data.get('health', 0)):,}"),
            ("Yellow armor", f"{int(data.get('yellow_armor', 0)):,}"),
            ("Red armor", f"{int(data.get('red_armor', 0)):,}"),
            ("Mega health", f"{int(data.get('mega', 0)):,}"),
        ]
        for i, (label, value) in enumerate(performance_values):
            performance.addWidget(self._stat_tile(label, value), i // 4, i % 4)
        lay.addLayout(performance)
        lay.addStretch(1)
        return page

    def _build_statistics_accuracy(self, data):
        page = QtWidgets.QWidget()
        page.setObjectName("statisticsTabPage")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(8)

        title = QtWidgets.QLabel("WEAPON ACCURACY")
        title.setObjectName("statisticsGroupTitle")
        lay.addWidget(title)

        table = QtWidgets.QTableWidget()
        table.setObjectName("statisticsWeaponTable")
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["WEAPON", "HITS", "ATTEMPTS", "ACCURACY", "KILLS", "DEATHS"])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            table.horizontalHeader().setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        weapons = data.get("weapons", []) or []
        table.setRowCount(len(weapons))
        for row, weapon in enumerate(weapons):
            hits = int(weapon.get("hits", 0) or 0)
            attempts = int(weapon.get("attempts", weapon.get("atts", 0)) or 0)
            kills = int(weapon.get("kills", 0) or 0)
            deaths = int(weapon.get("deaths", 0) or 0)
            accuracy = (hits / attempts * 100.0) if attempts > 0 else None
            values = [
                str(weapon.get("weapon", weapon.get("name", "Unknown"))),
                f"{hits:,}",
                f"{attempts:,}",
                f"{accuracy:.1f}%" if accuracy is not None else "—",
                f"{kills:,}",
                f"{deaths:,}",
            ]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if col > 0:
                    item.setTextAlignment(int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
                table.setItem(row, col, item)
            table.setRowHeight(row, 36)

        # Keep the table itself non-scrolling; the Statistics page owns scrolling.
        table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setFixedHeight(36 + max(1, len(weapons)) * 36 + 4)
        lay.addWidget(table)

        note = QtWidgets.QLabel("Accuracy = hits / attempts. Weapon values are preview data until the statistics API is connected.")
        note.setObjectName("statisticsMessage")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # SERVERS — native Quake 3 UDP monitor
    # ------------------------------------------------------------------
    def _build_servers(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SERVERS")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.serverRefreshLabel = QtWidgets.QLabel("Quake 3 UDP monitor")
        self.serverRefreshLabel.setObjectName("muted")
        header.addWidget(self.serverRefreshLabel)
        self.addServerButton = GlowButton("+  ADD SERVER")
        self.addServerButton.setObjectName("serverToolbarButton")
        self.addServerButton.clicked.connect(self.add_custom_server)
        header.addWidget(self.addServerButton)
        self.editServersButton = GlowButton("EDIT")
        self.editServersButton.setObjectName("serverToolbarButton")
        self.editServersButton.setCheckable(True)
        self.editServersButton.toggled.connect(self.set_server_edit_mode)
        header.addWidget(self.editServersButton)
        self.refreshServersButton = GlowButton("↻  REFRESH")
        self.refreshServersButton.setObjectName("serverToolbarButton")
        self.refreshServersButton.clicked.connect(self.refresh_servers)
        header.addWidget(self.refreshServersButton)
        root.addLayout(header)

        # Exactly three cards fit in the viewport. Additional cards continue
        # horizontally and the row wraps infinitely when the user scrolls.
        self.serversScroll = QtWidgets.QScrollArea()
        self.serversScroll.setObjectName("serversScroll")
        self.serversScroll.setWidgetResizable(False)
        self.serversScroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.serversScroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.serversScroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.serversContent = QtWidgets.QWidget()
        self.serversContent.setObjectName("serversContent")
        self.serversRow = QtWidgets.QHBoxLayout(self.serversContent)
        self.serversRow.setContentsMargins(0, 2, 0, 4)
        self.serversRow.setSpacing(12)
        self.serversRow.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.serversScroll.setWidget(self.serversContent)
        self.serversScroll.viewport().installEventFilter(self)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        root.addWidget(self.serversScroll, 1)

        nav = QtWidgets.QHBoxLayout()
        nav.addStretch(1)
        self.serverPrevButton = GlowButton("‹")
        self.serverPrevButton.setObjectName("serverCarouselButton")
        self.serverPrevButton.clicked.connect(lambda: self.scroll_server_monitors(-1))
        nav.addWidget(self.serverPrevButton)
        self.serverNextButton = GlowButton("›")
        self.serverNextButton.setObjectName("serverCarouselButton")
        self.serverNextButton.clicked.connect(lambda: self.scroll_server_monitors(1))
        nav.addWidget(self.serverNextButton)
        nav.addStretch(1)
        root.addLayout(nav)

        self.serverConfigPath = Path(os.environ.get("APPDATA", str(Path.home()))) / "Quake 3 Elite" / "Launcher" / "servers.json"
        self.serverLevelshotsDir = ASSETS_DIR / "servers" / "levelshots"
        self.serverCards = []
        self.serverQueryWorker = None
        self._serverScrollAccum = 0
        self.serverRefreshTimer = QtCore.QTimer(self)
        self.serverRefreshTimer.setInterval(30000)
        self.serverRefreshTimer.timeout.connect(self._auto_refresh_servers)
        self._reload_server_cards()
        return page

    def _reload_server_cards(self):
        while self.serversRow.count():
            item = self.serversRow.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.serverCards = []
        self.configuredServers = server_monitor.load_servers(self.serverConfigPath)
        for server in self.configuredServers:
            card = ServerCard(server, self.serverLevelshotsDir, self.serversContent)
            card.connectRequested.connect(self.connect_to_server)
            card.copyRequested.connect(self.copy_server_address)
            card.removeRequested.connect(self.remove_custom_server)
            card.moveRequested.connect(self.move_server)
            card.wheelRequested.connect(self.scroll_server_monitors)
            self.serverCards.append(card)
            self.serversRow.addWidget(card)
            card.installEventFilter(self)
            for child in card.findChildren(QtWidgets.QWidget):
                child.installEventFilter(self)
        if not self.serverCards:
            empty = QtWidgets.QLabel("No servers configured.\nUse + ADD SERVER or edit servers.json.")
            empty.setObjectName("serverEmpty")
            empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.serversRow.addWidget(empty)
        self._resize_servers_content()
        if hasattr(self, "editServersButton"):
            self.set_server_edit_mode(self.editServersButton.isChecked())
        self._update_server_position_label()

    def _resize_servers_content(self):
        count = max(1, len(self.serverCards))
        spacing = 12
        viewport_w = max(900, self.serversScroll.viewport().width())
        card_w = max(270, (viewport_w - spacing * 2) // 3)
        viewport_h = self.serversScroll.viewport().height()
        height = max(430, viewport_h - 2)
        for card in self.serverCards:
            card.setFixedWidth(card_w)
            card.setFixedHeight(height - 6)
        width = count * card_w + max(0, count - 1) * spacing
        self.serversContent.setFixedSize(width, height)

    def set_server_edit_mode(self, enabled):
        for card in self.serverCards:
            card.set_edit_mode(enabled)
        if hasattr(self, "editServersButton"):
            self.editServersButton.setText("DONE" if enabled else "EDIT")

    def scroll_server_monitors(self, direction):
        """Infinite carousel: rotate the visible card queue by one position."""
        if len(self.serverCards) <= 3:
            return
        direction = 1 if int(direction) > 0 else -1
        if direction > 0:
            card = self.serverCards.pop(0)
            self.serverCards.append(card)
        else:
            card = self.serverCards.pop()
            self.serverCards.insert(0, card)

        # Rebuild only the visual order. Server configuration order is untouched;
        # EDIT mode remains responsible for changing servers.json.
        for card in self.serverCards:
            self.serversRow.removeWidget(card)
        for card in self.serverCards:
            self.serversRow.addWidget(card)
        self._resize_servers_content()
        self.serversScroll.horizontalScrollBar().setValue(0)

    def _update_server_position_label(self):
        enabled = len(getattr(self, "serverCards", [])) > 3
        if hasattr(self, "serverPrevButton"):
            self.serverPrevButton.setEnabled(enabled)
            self.serverNextButton.setEnabled(enabled)

    def refresh_servers(self):
        if self.serverQueryWorker is not None and self.serverQueryWorker.isRunning():
            return
        if not self.configuredServers:
            self.serverRefreshLabel.setText("No servers in servers.json")
            return
        self.serverRefreshLabel.setText("Refreshing servers…")
        self.refreshServersButton.setEnabled(False)
        visual_servers = [dict(card.server) for card in self.serverCards]
        self.serverQueryWorker = ServerQueryWorker(visual_servers, self)
        self.serverQueryWorker.serverReady.connect(self._server_result_ready)
        self.serverQueryWorker.batchFinished.connect(self._server_refresh_finished)
        self.serverQueryWorker.start()

    def _server_result_ready(self, index, result):
        if 0 <= index < len(self.serverCards):
            self.serverCards[index].apply_result(result)

    def _server_refresh_finished(self):
        self.serverRefreshLabel.setText("Auto refresh every 30 seconds")
        self.refreshServersButton.setEnabled(True)

    def _auto_refresh_servers(self):
        if self.pages.currentWidget() is not self.serversPage:
            return
        if launcher_settings.get("pause_server_refresh_unfocused", False) and not self.isActiveWindow():
            self.serverRefreshLabel.setText("Auto refresh paused — Launcher out of focus")
            return
        self.refresh_servers()

    def add_custom_server(self):
        address, ok = QtWidgets.QInputDialog.getText(
            self, "Add Quake 3 server", "Server address (host:port):",
            QtWidgets.QLineEdit.EchoMode.Normal, ""
        )
        if not ok or not address.strip():
            return
        try:
            host, port = server_monitor.split_address(address.strip())
            normalized = f"{host}:{port}"
        except Exception:
            self.qerror("Invalid server address.\nUse host:port, for example q3msk.net:27977")
            return
        name, ok_name = QtWidgets.QInputDialog.getText(
            self, "Server name", "Display name (optional):",
            QtWidgets.QLineEdit.EchoMode.Normal, normalized
        )
        if not ok_name:
            return
        current = server_monitor.load_servers(self.serverConfigPath)
        if any(str(x.get("address","")).lower() == normalized.lower() for x in current):
            self.qerror("This server is already in the monitor.")
            return
        current.append({"name": name.strip() or normalized, "address": normalized})
        server_monitor.save_custom_servers(self.serverConfigPath, current)
        self._reload_server_cards()
        self.refresh_servers()

    def remove_custom_server(self, address):
        current = server_monitor.load_servers(self.serverConfigPath)
        current = [x for x in current if str(x.get("address","")).lower() != str(address).lower()]
        server_monitor.save_custom_servers(self.serverConfigPath, current)
        self._reload_server_cards()
        self.refresh_servers()

    def move_server(self, address, direction):
        current = server_monitor.load_servers(self.serverConfigPath)
        index = next((i for i, x in enumerate(current)
                      if str(x.get("address","")).lower() == str(address).lower()), -1)
        if index < 0 or len(current) < 2:
            return
        target = (index + int(direction)) % len(current)
        item = current.pop(index)
        current.insert(target, item)
        server_monitor.save_custom_servers(self.serverConfigPath, current)
        self._reload_server_cards()

    def copy_server_address(self, address):
        QApplication.clipboard().setText(str(address))
        self.serverRefreshLabel.setText(f"Copied: {address}")

    def connect_to_server(self, address):
        address = str(address or "").strip()
        if not address:
            return
        if not q3elite_is_installed():
            self.qerror("Install Quake 3 Elite before connecting to a server.")
            return

        launcher_bat = GAME_ROOT / "Q3Elite" / "Engines" / "Q3Elite (Vulkan) - Cinematic.bat"
        if not launcher_bat.is_file():
            self.qerror(f"Q3Elite Vulkan launcher was not found:\n{launcher_bat}")
            return

        try:
            import subprocess
            # Pass exactly two extra BAT arguments:
            #   +connect "ip:port"
            # The BAT's trailing %* appends them to XQ3E_Vulkan.x64.exe.
            subprocess.Popen(
                [str(launcher_bat), "+connect", address],
                cwd=str(launcher_bat.parent),
                shell=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.serverRefreshLabel.setText(f"Connecting to {address}…")
        except Exception as error:
            self.qerror(f"Could not connect to server:\n{error}")


    def _build_changelog(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("CHANGELOG")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        hint = QtWidgets.QLabel("Launcher  •  Quake 3 Elite  •  Telegram")
        hint.setObjectName("muted")
        header.addWidget(hint)
        root.addLayout(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("changelogScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        content = QtWidgets.QWidget()
        content.setObjectName("changelogContent")
        feed = QtWidgets.QVBoxLayout(content)
        feed.setContentsMargins(0, 0, 8, 0)
        feed.setSpacing(12)

        entries = read_changelog_entries()
        if entries:
            from datetime import datetime
            def _key(item):
                value = str(item.get("date", item.get("release_date", "")))
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
                    try:
                        return datetime.strptime(value[:10], fmt)
                    except ValueError:
                        pass
                return datetime.min
            sorted_entries = sorted(entries, key=_key, reverse=True)
            for index, release in enumerate(sorted_entries):
                # The newest three updates are immediately readable. Older
                # history keeps only its title bar until explicitly expanded.
                feed.addWidget(
                    ChangelogCard(release, content, expanded=(index < 3))
                )
        else:
            empty = QtWidgets.QLabel("No changelog entries found.")
            empty.setObjectName("muted")
            feed.addWidget(empty)

        feed.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        return page

    def rebuild_changelog_page(self):
        """Rebuild changelog cards after preview preferences change."""
        old_page = self.changelogPage
        old_index = self.pages.indexOf(old_page)
        was_current = self.pages.currentWidget() is old_page

        new_page = self._build_changelog()
        self.pages.insertWidget(old_index, new_page)
        self.pages.removeWidget(old_page)
        old_page.deleteLater()
        self.changelogPage = new_page

        if was_current:
            self.pages.setCurrentWidget(new_page)

    def show_page(self, page):
        mapping = {
            "home": (self.homePage, self.homeNav),
            "addons": (self.addonsPage, self.addonsNav),
            "statistics": (self.statisticsPage, self.statisticsNav),
            "servers": (self.serversPage, self.serversNav),
            "settings": (self.settingsPage, self.settingsNav),
            "changelog": (self.changelogPage, self.changelogNav),
        }
        widget, active = mapping[page]
        self.pages.setCurrentWidget(widget)
        for b in (self.homeNav, self.addonsNav, self.statisticsNav, self.serversNav, self.settingsNav, self.changelogNav):
            b.setChecked(b is active)
        if page == "addons":
            refresh_component_gui()
        if page == "servers":
            self.serverRefreshTimer.start()
            QtCore.QTimer.singleShot(0, self._resize_servers_content)
            self.refresh_servers()
        elif hasattr(self, "serverRefreshTimer"):
            self.serverRefreshTimer.stop()

    def set_navigation_enabled(self, enabled):
        for b in (self.homeNav, self.addonsNav, self.statisticsNav, self.serversNav, self.settingsNav, self.changelogNav, self.refreshButton):
            b.setEnabled(enabled)

    def _load_component_state_initial(self):
        """Load component state during __init__ without touching global `window`."""
        state = q3components.load_state()
        self.mapsBox.setChecked(bool(state.get("external_maps", False)))
        self.musicBox.setChecked(bool(state.get("music_playlist", False)))
        self.capture_component_baseline()

    def capture_component_baseline(self):
        self._component_baseline = {
            "maps": self.mapsBox.isChecked(),
            "music": self.musicBox.isChecked(),
        }

    def prepare_component_actions(self):
        old = self._component_baseline
        new = {
            "maps": self.mapsBox.isChecked(),
            "music": self.musicBox.isChecked(),
        }
        actions = []
        if old.get("maps") != new["maps"]:
            actions.append("install-maps" if new["maps"] else "remove-maps")
        if old.get("music") != new["music"]:
            actions.append("install-music" if new["music"] else "remove-music")
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
        self.startWindowsBox.setChecked(launcher_settings.get("start_with_windows", False))
        self.trayBox.setChecked(launcher_settings.get("minimize_to_tray", False))
        self.vulkanLayerBox.setChecked(reshade_layer_enabled())
        self.changelogMediaBox.setChecked(
            launcher_settings.get("show_changelog_media", False)
        )
        self.pauseServerRefreshBox.setChecked(
            launcher_settings.get("pause_server_refresh_unfocused", False)
        )
        def load_cleanup_control(days, checkbox, combo, custom):
            days = max(0, int(days or 0))
            checkbox.setChecked(days > 0)
            if days <= 0:
                combo.setCurrentIndex(combo.findData(7))
                custom.setVisible(False)
                combo.setEnabled(False)
                custom.setEnabled(False)
                return

            index = combo.findData(days)
            if index >= 0:
                combo.setCurrentIndex(index)
                custom.setVisible(False)
            else:
                combo.setCurrentIndex(combo.findData(-1))
                custom.setValue(min(days, 3650))
                custom.setVisible(True)
            combo.setEnabled(True)
            custom.setEnabled(True)

        load_cleanup_control(
            launcher_settings.get("cleanup_screenshots_days", 0),
            self.cleanupScreenshotsBox,
            self.cleanupScreenshotsCombo,
            self.cleanupScreenshotsCustom,
        )
        load_cleanup_control(
            launcher_settings.get("cleanup_demos_days", 0),
            self.cleanupDemosBox,
            self.cleanupDemosCombo,
            self.cleanupDemosCustom,
        )

    def open_cache_folder(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(CACHE_DIR)))

    def open_config_editor(self):
        if not config_editor_available():
            return
        dialog = ConfigEditorDialog(self)
        dialog.exec()

    def minimize_launcher(self):
        if launcher_settings.get("minimize_to_tray", False) and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.trayIcon.show()
            self.hide()
        else:
            self.showMinimized()

    def restore_from_tray(self):
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def exit_from_tray(self):
        self._allow_close = True
        self.trayIcon.hide()
        self.close()

    def _tray_activated(self, reason):
        if reason in (
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_from_tray()

    def closeEvent(self, event):
        if (
            not self._allow_close
            and launcher_settings.get("minimize_to_tray", False)
            and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()
        ):
            self.trayIcon.show()
            self.hide()
            event.ignore()
            return
        event.accept()

    def launch(self):
        """Start the game directly without re-running the launcher."""
        if not q3elite_is_installed():
            prepare_first_install()
            return
        try:
            if not launch():
                self.qerror(
                    "Could not start Q3Elite.\n"
                    "The Vulkan/OpenGL game launcher was not found or failed to start."
                )
                return
            self.statusDetail.setText("Q3Elite started.")
        except Exception as error:
            self.qerror(f"Could not start Q3Elite:\n{error}")

    def change_hero_image(self, delta):
        if not self._hero_urls:
            return
        self._hero_index = (self._hero_index + delta) % (len(self._hero_local) if getattr(self, "_hero_local", None) else len(self._hero_urls))
        self.load_hero_image()

    def load_hero_image(self):
        if not self._hero_urls:
            return
        self.heroCounter.setText(f"{self._hero_index + 1:02d} / {len(self._hero_urls):02d}")
        if getattr(self, "_hero_local", None):
            path = self._hero_local[self._hero_index % len(self._hero_local)]
            key = str(path)
            cached = self._hero_pixmaps.get(key)
            if cached is None:
                cached = QtGui.QPixmap(key)
                if not cached.isNull(): self._hero_pixmaps[key] = cached
            if cached is not None and not cached.isNull():
                self.heroCounter.setText(f"{(self._hero_index % len(self._hero_local)) + 1:02d} / {len(self._hero_local):02d}")
                self._set_hero_pixmap(cached); return
        url = self._hero_urls[self._hero_index]
        cached = self._hero_pixmaps.get(url)
        if cached is not None:
            self._set_hero_pixmap(cached)
            return
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
        request.setRawHeader(b"User-Agent", f"Q3Elite-Launcher/{read_launcher_metadata().get('version', 'unknown')}".encode("ascii", "ignore"))
        reply = self._network.get(request)
        reply.finished.connect(lambda r=reply, u=url: self._hero_download_finished(r, u))

    def _hero_download_finished(self, reply, url):
        try:
            if reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError:
                self.heroImage.setText("Q3ELITE  •  VULKAN  •  OSP2-BE")
                return
            pixmap = QtGui.QPixmap()
            if pixmap.loadFromData(bytes(reply.readAll())):
                self._hero_pixmaps[url] = pixmap
                if url == self._hero_urls[self._hero_index]:
                    self._set_hero_pixmap(pixmap)
        finally:
            reply.deleteLater()

    def _set_hero_pixmap(self, pixmap):
        target = self.heroImage.size()
        if target.width() < 10 or target.height() < 10:
            return
        scaled = pixmap.scaled(
            target,
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - target.width()) // 2)
        y = max(0, (scaled.height() - target.height()) // 2)
        self.heroImage.setPixmap(scaled.copy(x, y, target.width(), target.height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_hero_urls") and self._hero_urls:
            if getattr(self, "_hero_local", None):
                key = str(self._hero_local[self._hero_index % len(self._hero_local)])
            else:
                key = self._hero_urls[self._hero_index]
            pixmap = self._hero_pixmaps.get(key)
            if pixmap is not None: self._set_hero_pixmap(pixmap)

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
        if hasattr(window, "firstInstallCard"):
            window.firstInstallCard.setEnabled(True)
        check_install_finished()
        return

    # FirstLaunch.bat logic is now owned by the launcher.
    try:
        configure_reshade_vulkan(True, install_files=True)
        window.vulkanLayerBox.setChecked(True)
    except Exception as error:
        print(f"[warning] ReShade Vulkan layer setup failed: {error}")
        # ReShade is optional; do not invalidate the game installation.

    # On first install PAK verification starts only AFTER Basic has finished.
    if not install_state["base_done"] and not fdownload.isRunning():
        set_gui_checking("Checking PAKs...")
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
        icon_path = APP_ICON_ICO if APP_ICON_ICO.is_file() else APP_ICON_PNG
        if icon_path.is_file():
            app.setWindowIcon(QtGui.QIcon(str(icon_path)))

        # Prevent a second launcher process from starting.
        instance_dir = Path(os.environ.get("APPDATA", str(LAUNCHER_DIR))) / "Quake 3 Elite" / "Launcher"
        instance_dir.mkdir(parents=True, exist_ok=True)
        instance_lock = QLockFile(str(instance_dir / "Q3EliteLauncher.lock"))
        instance_lock.setStaleLockTime(0)
        if not instance_lock.tryLock(100):
            print("Q3Elite Launcher is already running.")
            return 0

        # External stylesheet is now the single source of visual styling.
        style_path = LAUNCHER_DIR / "modules" / "style.css"
        if style_path.is_file():
            app.setStyleSheet(style_path.read_text(encoding="utf-8"))
        else:
            print(f"[warning] UI stylesheet not found: {style_path}")

        # Fonts live only in Launcher/assets/fonts/*.ttf.
        fonts_dir = ASSETS_DIR / "fonts"
        family = "Arial"
        preferred_font = fonts_dir / "FiraCode-Regular.ttf"
        font_files = []
        if fonts_dir.is_dir():
            font_files = sorted(fonts_dir.glob("*.ttf"))

        # Register every bundled TTF so style.css can reference any of them.
        registered_families = {}
        for font_path in font_files:
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id != -1:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    registered_families[font_path.name] = families[0]

        if preferred_font.name in registered_families:
            family = registered_families[preferred_font.name]
        elif registered_families:
            family = next(iter(registered_families.values()))

        window = ModernLauncherWindow()

        autostart_mode = "--autostart" in sys.argv
        if autostart_mode and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            window.trayIcon.show()
            window.hide()
        else:
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
            if "app" in globals() and app is not None:
                QtWidgets.QMessageBox.critical(None, "Q3Elite Launcher", message)
            else:
                print(message)
        except Exception:
            print(message)
        raise


# Explicit entry point for the standalone .pyw launcher.
if __name__ == "__main__":
    main()
