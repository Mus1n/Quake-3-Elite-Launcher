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


def sorted_launcher_releases():
    from datetime import datetime
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
    launcher_settings = {
        "auto_update_q3elite": window.autoQ3Box.isChecked(),
        "auto_update_launcher": window.autoLauncherBox.isChecked(),
        "auto_update_osp": window.autoOspBox.isChecked(),
        "start_with_windows": window.startWindowsBox.isChecked(),
        "minimize_to_tray": window.trayBox.isChecked(),
    }
    try:
        set_start_with_windows(launcher_settings["start_with_windows"])
        requested_vulkan = window.vulkanLayerBox.isChecked()
        if requested_vulkan != old_vulkan:
            configure_reshade_vulkan(requested_vulkan, install_files=requested_vulkan)
        save_launcher_settings(launcher_settings)
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
        self.settingsNav = self._nav_button("SETTINGS", "settings", "fa5s.cog")
        self.changelogNav = self._nav_button("CHANGELOG", "changelog", "fa5s.scroll")
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

        self.launcherVersion = QtWidgets.QLabel(f"Launcher v{read_launcher_metadata().get('version', '—')}")
        self.launcherVersion.setObjectName("versionLabel")
        top.addWidget(self.launcherVersion)

        self.minButton = GlowButton("—")
        self.minButton.setObjectName("windowButton")
        self.minButton.setFixedSize(42, 36)
        self.minButton.clicked.connect(self.minimize_launcher)
        top.addWidget(self.minButton)

        self.closeButton = GlowButton("×")
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

        self.heroPrev = QtWidgets.QPushButton("‹")
        self.heroPrev.setObjectName("sliderArrow")
        self.heroPrev.setFixedSize(38, 38)
        self.heroPrev.clicked.connect(lambda: self.change_hero_image(-1))
        hc.addWidget(self.heroPrev)

        self.heroNext = QtWidgets.QPushButton("›")
        self.heroNext.setObjectName("sliderArrow")
        self.heroNext.setFixedSize(38, 38)
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
        for box in (
            self.autoQ3Box, self.autoLauncherBox, self.autoOspBox,
            self.startWindowsBox, self.trayBox,
            self.vulkanLayerBox,
        ):
            c.addWidget(box)
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

    def _build_changelog(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 4, 4, 4)
        title = QtWidgets.QLabel("CHANGELOG")
        title.setObjectName("pageTitle")
        lay.addWidget(title)

        browser = QtWidgets.QTextBrowser()
        browser.setObjectName("changelogBrowser")
        releases = sorted_launcher_releases()
        if releases:
            parts = []
            for release in releases:
                version = str(release.get("version", "?"))
                import html
                date = str(release.get("date", release.get("release_date", release.get("published_at", ""))))
                changes = release.get(
                    "changes",
                    release.get("items", release.get("notes", release.get("change_log", release.get("release_notes", []))))
                )
                if isinstance(changes, str):
                    changes = [changes]
                if not isinstance(changes, list):
                    changes = []
                heading = f"<h2>Launcher {html.escape(version)}</h2>"
                if date:
                    heading += f"<p><b>{html.escape(date)}</b></p>"
                parts.append(heading)
                if changes:
                    parts.append("<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in changes) + "</ul>")
            browser.setHtml("".join(parts))
        else:
            meta = read_launcher_metadata()
            browser.setHtml(
                f"<h2>Launcher {meta.get('version', '—')}</h2>"
                "<p>No changelog entries were found in the local version metadata.</p>"
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
        style_path = LAUNCHER_DIR / "ui" / "style.css"
        if style_path.is_file():
            app.setStyleSheet(style_path.read_text(encoding="utf-8"))
        else:
            print(f"[warning] UI stylesheet not found: {style_path}")

        # Keep existing font support for the backend terminal.
        font_path = ASSETS_DIR / "fonts" / "FiraCode-Regular.ttf"
        if not font_path.is_file():
            font_path = LAUNCHER_DIR / "ui" / "FiraCode-Regular.ttf"
        family = "Arial"
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id != -1:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    family = families[0]

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
