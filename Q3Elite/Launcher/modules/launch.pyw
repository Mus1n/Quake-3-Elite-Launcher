import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import sys
import traceback
import shutil
import json
import html
import re
import urllib.parse
import ssl
import urllib.request
import http.cookiejar
import zipfile
import time
import uuid
import subprocess
import threading
import base64
import datetime
import hashlib
from collections import OrderedDict

from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets, QtNetwork

# Detached Telegram renderers live off-screen. Chromium normally throttles
# occluded/background windows, which would freeze animated custom emoji between
# snapshot frames. These flags only affect QtWebEngine and the renderers are
# explicitly Frozen whenever Changelog is not visible.
if os.name == "nt":
    _q3_chromium_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    for _flag in (
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    ):
        if _flag not in _q3_chromium_flags:
            _q3_chromium_flags = (_q3_chromium_flags + " " + _flag).strip()
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _q3_chromium_flags

try:
    from PyQt6 import QtMultimedia
except Exception:
    QtMultimedia = None

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

from ui_theme import ThemeManager
from obsidian_material import OBSIDIAN_MATERIAL

try:
    import qtawesome as qta
except Exception:
    qta = None

try:
    import pywinstyles
except Exception:
    pywinstyles = None

try:
    from PIL import Image as PILImage
except Exception:
    PILImage = None


# ============================================================================
# PATHS
# ============================================================================

LAUNCHER_DIR = Path(__file__).resolve().parent.parent
GAME_ROOT = LAUNCHER_DIR.parent.parent
ASSETS_DIR = LAUNCHER_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
IMAGES_DIR = ASSETS_DIR / "images"
APPDATA_ROOT = Path(os.environ.get("APPDATA", Path.home()))
LAUNCHER_DATA_DIR = APPDATA_ROOT / "Quake 3 Elite" / "Launcher"
CACHE_DIR = LAUNCHER_DATA_DIR / "cache"
IMAGE_CACHE_DIR = CACHE_DIR / "images"
TGA_CACHE_DIR = IMAGE_CACHE_DIR / "tga"
TEMP_DIR = LAUNCHER_DATA_DIR / "temp"
ONLINE_MAPS_DIR = LAUNCHER_DATA_DIR / "online_maps"
ONLINE_MAPS_DB = ONLINE_MAPS_DIR / "MapData.db"
ONLINE_MAPS_DB_URL = "https://dl.netquick.ch/MapData.db"
DEFAULT_SERVERS_FILE = LAUNCHER_DIR / "settings" / "servers.json"
USER_SERVERS_FILE = LAUNCHER_DATA_DIR / "servers.json"
MAPS_MANIFEST_FILE = LAUNCHER_DIR / "maps_manifest"
MAP_LEVELSHOTS_DIR = LAUNCHER_DIR / "servers" / "levelshots"
MAP_DISCOVERED_LEVELSHOTS_DIR = LAUNCHER_DATA_DIR / "levelshots"
MAP_UNKNOWN_LEVELSHOT = ASSETS_DIR / "server" / "levelshots" / "unknownmap.png"
ONLINE_MAP_CACHE_DIR = LAUNCHER_DATA_DIR / "online_maps"
ONLINE_MAPS_INSTALLED_FILE = LAUNCHER_DATA_DIR / "online_maps_installed.json"
DOWNLOADED_MAPS_DIR = GAME_ROOT / "baseq3" / "mods" / "baseq3"
WORLDSPAWN_BASE = "https://ws.q3df.org"
LVLWORLD_BASE = "https://lvlworld.com"

PROTECTED_MAP_PAKS = {
    *(f"pak{i}.pk3" for i in range(9)),
    "arenagate.pk3",
    "spillway.pk3",
    "hearth.pk3",
    "powerstation.pk3",
    "eviscerated.pk3",
    "forgotten.pk3",
    "campgrounds.pk3",
    "provinggrounds.pk3",
    "retribution.pk3",
    "brimstoneabbey.pk3",
    "heroskeep.pk3",
    "hellsgate.pk3",
    "namelessplace.pk3",
    "chemicalreaction.pk3",
    "dredwerkz.pk3",
    "verticalvengeance.pk3",
    "lostworld.pk3",
    "grimdungeons.pk3",
    "demonkeep.pk3",
    "fatalinstinct.pk3",
    "cobaltstation.pk3",
    "longestyard.pk3",
    "spacechamber.pk3",
    "terminalheights.pk3",
    "theepicenter.pk3",
    "beyondreality.pk3",
}
BACKGROUND_IMAGE = IMAGES_DIR / "Banner.png"
THEMES_DIR = LAUNCHER_DIR / "Themes"
APP_ICON_ICO = ICONS_DIR / "favicon.ico"
APP_ICON_PNG = ICONS_DIR / "favicon.png"
VULKAN_EXE = GAME_ROOT / "Q3Elite" / "Engines" / "XQ3E_Vulkan.x64.exe"
RESHADE_SOURCE = GAME_ROOT / "Q3Elite" / "ReShade" / "Program"
RESHADE_DEST = Path(os.environ.get("ProgramData", r"C:\\ProgramData")) / "ReShade"

# Runtime identity manager. Visual constants should come from theme tokens rather
# than being scattered through launcher feature code.
ui_theme = None

def theme_value(path, default=None):
    return ui_theme.value(path, default) if ui_theme is not None else default

def theme_int(path, default):
    return ui_theme.int(path, default) if ui_theme is not None else int(default)

def theme_float(path, default):
    return ui_theme.float(path, default) if ui_theme is not None else float(default)

def theme_bool(path, default=False):
    return ui_theme.bool(path, default) if ui_theme is not None else bool(default)

def theme_color(path, default="#ffffff"):
    return ui_theme.color(path, default) if ui_theme is not None else QtGui.QColor(default)

def theme_asset(path, default=None):
    return ui_theme.asset_path(path, default) if ui_theme is not None else default


# ============================================================================
# PIXMAP / OBSIDIAN RENDER CACHE — V4 MEMORY-AWARE PERFORMANCE PASS
# ============================================================================
# Obsidian's supersampled material is expensive to generate, so keeping raster
# results is still the right trade-off. V2/V3 kept them forever, however, which
# can grow into hundreds of MB after visiting every section. A byte-budgeted LRU
# keeps the hot/recent surfaces instant while evicting old page-sized pixmaps.
class _PixmapLRUCache:
    def __init__(self, limit_token, default_mb):
        self.limit_token = str(limit_token)
        self.default_mb = max(8, int(default_mb))
        self._items = OrderedDict()
        self._bytes = 0

    @staticmethod
    def _pixmap_bytes(pm):
        try:
            if pm is None or pm.isNull():
                return 0
            # QPixmap width/height are physical pixel dimensions even when a DPR
            # is attached, so 4 bytes/pixel is a useful upper-bound estimate.
            return max(0, int(pm.width())) * max(0, int(pm.height())) * 4
        except Exception:
            return 0

    def _limit_bytes(self):
        try:
            mb = theme_int(self.limit_token, self.default_mb)
        except Exception:
            mb = self.default_mb
        # <= 0 remains an escape hatch for development profiling/unbounded mode.
        return 0 if int(mb) <= 0 else int(mb) * 1024 * 1024

    def get(self, key, default=None):
        item = self._items.pop(key, None)
        if item is None:
            return default
        self._items[key] = item
        return item[0]

    def __setitem__(self, key, pm):
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        size = self._pixmap_bytes(pm)
        self._items[key] = (pm, size)
        self._bytes += size

        limit = self._limit_bytes()
        if limit <= 0:
            return
        # Keep at least the newest entry even if a single large surface exceeds
        # the nominal budget; otherwise it would be regenerated every repaint.
        while self._bytes > limit and len(self._items) > 1:
            _, (_, victim_size) = self._items.popitem(last=False)
            self._bytes -= victim_size

    def clear(self):
        self._items.clear()
        self._bytes = 0

    @property
    def bytes_used(self):
        return max(0, int(self._bytes))


_OBSIDIAN_RENDER_CACHE = _PixmapLRUCache("performance.material_cache_mb", 128)


def _theme_revision():
    return int(getattr(ui_theme, "revision", 0) or 0) if ui_theme is not None else 0


def _render_cached_surface(namespace, key, logical_size, render):
    width = max(1, int(round(logical_size.width())))
    height = max(1, int(round(logical_size.height())))
    app = QtWidgets.QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
    dpr = max(1.0, dpr)
    cache_key = (namespace, _theme_revision(), key, width, height, round(dpr, 3))
    cached = _OBSIDIAN_RENDER_CACHE.get(cache_key)
    if cached is not None and not cached.isNull():
        return cached

    physical = QtCore.QSize(max(1, round(width * dpr)), max(1, round(height * dpr)))
    pm = QtGui.QPixmap(physical)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pm)
    painter.scale(dpr, dpr)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
    render(painter, QtCore.QRectF(0.0, 0.0, float(width), float(height)))
    painter.end()
    pm.setDevicePixelRatio(dpr)
    _OBSIDIAN_RENDER_CACHE[cache_key] = pm
    return pm


def _scroll_perf_active(widget):
    """True while a heavy Obsidian scroll area is actively moving.

    Hover-sensitive material states are suppressed for a few milliseconds while
    scrolling so controls moving under a stationary mouse cursor do not trigger
    extra material rerenders on top of the scroll repaint itself.
    """
    try:
        window = widget.window()
        return bool(window is not None and window.property("q3_scroll_active"))
    except Exception:
        return False


def _clear_obsidian_render_cache():
    _OBSIDIAN_RENDER_CACHE.clear()
    try:
        _SMOOTH_ICON_PIXMAP_CACHE.clear()
    except Exception:
        pass


def css_color(value, default="rgba(255,255,255,0.1)"):
    """Convert theme colors to browser-safe CSS.

    Q3Elite theme JSON stores rgba alpha in Qt's 0..255 range, e.g.
    rgba(255,255,255,92). CSS/Chromium interprets alpha as 0..1, so passing
    the token verbatim makes material rims nearly/fully opaque.
    """
    raw = str(value if value not in (None, "") else default).strip()
    match = re.fullmatch(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
        r"(?:\s*,\s*([0-9.]+))?\s*\)",
        raw,
        flags=re.I,
    )
    if not match:
        return raw

    r = max(0, min(255, int(match.group(1))))
    g = max(0, min(255, int(match.group(2))))
    b = max(0, min(255, int(match.group(3))))
    alpha_raw = match.group(4)
    if alpha_raw is None:
        return f"rgb({r},{g},{b})"

    alpha = float(alpha_raw)
    if alpha > 1.0:
        alpha /= 255.0
    alpha = max(0.0, min(1.0, alpha))
    return f"rgba({r},{g},{b},{alpha:.4f})"

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
from launcher_updater import (
    check_for_update as check_launcher_update,
    check_for_repair as check_launcher_repair,
    stage_update as stage_launcher_update,
    launch_apply_helper,
)
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
    """
    Installation marker used by the launcher.

    The required QL Singleplayer maps are installed only after the Q3Elite
    Basic archive has been extracted.  Therefore any PK3 in QLmaps is a much
    better recovery marker than Version.json or the old engine/OSP pair:
    metadata may be missing after an interrupted first launch, while the game
    payload itself is already usable.
    """
    qlmaps = GAME_ROOT / "baseq3" / "maps" / "QLmaps"
    try:
        installed = qlmaps.is_dir() and any(
            p.is_file() and p.suffix.casefold() == ".pk3"
            for p in qlmaps.iterdir()
        )
    except OSError:
        installed = False

    # This predicate is called from several UI refresh paths (including demos).
    # Keep it silent: repeated status prints made debug output noisy while browsing.
    return installed


def q3elite_metadata_present():
    """True only when the local updater metadata from a completed install exists."""
    return (GAME_ROOT / "Q3Elite" / "Version.json").is_file()


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

        except BaseException as error:
            print()
            print(f"[error] Quake 3 Elite installation failed: {error}")
            traceback.print_exc()
            try:
                crash_log = LAUNCHER_DATA_DIR / "install_crash.log"
                crash_log.parent.mkdir(parents=True, exist_ok=True)
                crash_log.write_text(traceback.format_exc(), encoding="utf-8")
                print(f"[error] Traceback saved to: {crash_log}")
            except Exception:
                pass
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



_TELEGRAM_POST_CACHE = {}
_TELEGRAM_POST_CACHE_LOCK = threading.Lock()


def _telegram_cache_path(url):
    key = hashlib.blake2b(str(url).encode("utf-8", "ignore"), digest_size=12).hexdigest()
    return CACHE_DIR / "telegram_posts" / f"{key}.json"


def _telegram_cached_post(url, allow_stale=False):
    """Return cached Telegram data without touching the network.

    Fresh entries are promoted to the in-memory cache.  When ``allow_stale``
    is True an expired disk entry may still be shown immediately while a
    background worker refreshes it (stale-while-revalidate).
    """
    url = str(url or "").strip()
    if not url:
        return None
    with _TELEGRAM_POST_CACHE_LOCK:
        cached = _TELEGRAM_POST_CACHE.get(url)
        if isinstance(cached, dict):
            return dict(cached)

    path = _telegram_cache_path(url)
    try:
        if not path.is_file():
            return None
        max_age = max(1, theme_int("performance.telegram_cache_hours", 24)) * 3600
        stale = (time.time() - path.stat().st_mtime) > max_age
        if stale and not allow_stale:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # Do not promote stale data to the strict in-memory cache; otherwise a
        # later refresh probe would incorrectly treat it as fresh.
        if not stale:
            with _TELEGRAM_POST_CACHE_LOCK:
                _TELEGRAM_POST_CACHE[url] = dict(data)
        return dict(data)
    except (OSError, ValueError, TypeError):
        return None


def _store_telegram_cached_post(url, data):
    url = str(url or "").strip()
    if not url or not isinstance(data, dict):
        return
    clean = dict(data)
    with _TELEGRAM_POST_CACHE_LOCK:
        _TELEGRAM_POST_CACHE[url] = clean
    try:
        path = _telegram_cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _telegram_public_post_data(url):
    """Best-effort extraction of text + media from a Telegram public post."""
    value = str(url or "").strip()
    if not value or "t.me/" not in value:
        return {}

    cached = _telegram_cached_post(value)
    if cached is not None:
        return cached

    match = re.match(r"https?://t\.me/(?:s/)?([^/?#]+)/(\d+)", value)
    fetch_urls = [value]
    if match:
        channel, post_id = match.group(1), match.group(2)
        # Telegram has changed the public-post surface several times.  Keep a
        # small fallback chain so the native Changelog does not depend on one
        # exact HTML endpoint.
        fetch_urls = [
            f"https://t.me/s/{channel}/{post_id}",
            f"https://t.me/{channel}/{post_id}?embed=1&mode=tme",
            f"https://t.me/{channel}/{post_id}?embed=1",
        ]

    try:
        page = ""
        last_error = None
        for fetch_url in fetch_urls:
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
                with urllib.request.urlopen(req, timeout=8) as response:
                    candidate = response.read().decode("utf-8", errors="replace")
                if candidate.strip():
                    page = candidate
                    # The /s/ and embed pages both contain tgme_widget_message
                    # when Telegram served the requested post successfully.
                    if "tgme_widget_message" in candidate:
                        break
            except Exception as endpoint_error:
                last_error = endpoint_error
                continue
        if not page:
            if last_error is not None:
                raise last_error
            return {}

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

        if result:
            _store_telegram_cached_post(value, result)
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
        # Never perform Telegram HTTP on the GUI thread. The launcher preload
        # worker fills this cache shortly after startup; Changelog consumes only
        # the cached/native representation unless the user explicitly requests
        # the live Chromium renderer.
        tg = (_telegram_cached_post(source_url) or {}) if source_url else {}
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

SETTINGS_FILE = LAUNCHER_DATA_DIR / "settings.json"
MATCHMAKING_FILE = LAUNCHER_DATA_DIR / "matchmaking.json"
NOTIFY_SOUND = ASSETS_DIR / "sounds" / "notify.mp3"

DEFAULT_SETTINGS = {
    "auto_update_q3elite": True,
    "auto_update_launcher": True,
    "auto_update_osp": True,
    "start_with_windows": False,
    "start_minimized": False,
    "minimize_to_tray": False,
    "show_changelog_media": False,
    "cleanup_screenshots_days": 0,
    "cleanup_demos_days": 0,
    "pause_server_refresh_unfocused": False,
    "screenshot_source": "reshade",
    "suppress_startup_notification_sound": True,
    "statistics_profile_url": "",
    "statistics_period": "week",
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
                        elif key == "screenshot_source":
                            value = str(raw[key]).lower()
                            data[key] = value if value in ("reshade", "osp", "both") else "reshade"
                        elif key == "statistics_profile_url":
                            data[key] = str(raw[key] or "").strip()
                        elif key == "statistics_period":
                            value = str(raw[key] or "week").lower()
                            data[key] = value if value in ("week", "month") else "week"
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
    if hasattr(window, "homeStatusDot"):
        window.homeStatusDot.setProperty("state", kind)
        window.homeStatusDot.style().unpolish(window.homeStatusDot)
        window.homeStatusDot.style().polish(window.homeStatusDot)


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
    old_cleanup_screenshots_days = int(launcher_settings.get("cleanup_screenshots_days", 0) or 0)
    old_cleanup_demos_days = int(launcher_settings.get("cleanup_demos_days", 0) or 0)

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
        "start_minimized": window.startMinimizedBox.isChecked(),
        "minimize_to_tray": window.trayBox.isChecked(),
        # Despite the historical label, this controls ALL changelog media.
        "show_changelog_media": window.changelogMediaBox.isChecked(),
        "cleanup_screenshots_days": cleanup_screenshots_days,
        "cleanup_demos_days": cleanup_demos_days,
        "pause_server_refresh_unfocused": window.pauseServerRefreshBox.isChecked(),
        "suppress_startup_notification_sound": window.suppressStartupSoundBox.isChecked(),
        "screenshot_source": launcher_settings.get("screenshot_source", "reshade"),
        # Statistics preferences live in the same settings file and must survive
        # pressing APPLY on the Settings page.
        "statistics_profile_url": launcher_settings.get("statistics_profile_url", ""),
        "statistics_period": launcher_settings.get("statistics_period", "week"),
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

        # Apply changed cleanup rules immediately instead of waiting for the
        # next launcher start. Refresh the media views afterwards so files
        # removed by the new rule disappear from the current lists as well.
        cleanup_messages = []
        if old_cleanup_screenshots_days != cleanup_screenshots_days:
            removed = cleanup_old_files(
                [GAME_ROOT / "baseq3" / "mods" / "osp" / "screenshots",
                 GAME_ROOT / "Q3Elite" / "Screenshots"],
                cleanup_screenshots_days,
            )
            window.refresh_screenshots()
            if removed:
                cleanup_messages.append(f"{removed} screenshot(s) removed")

        if old_cleanup_demos_days != cleanup_demos_days:
            removed = cleanup_old_files(
                [GAME_ROOT / "baseq3" / "mods" / "osp" / "demos"],
                cleanup_demos_days,
            )
            window.refresh_demos()
            if removed:
                cleanup_messages.append(f"{removed} demo(s) removed")

        message = "Settings saved."
        if cleanup_messages:
            message += " " + "; ".join(cleanup_messages) + "."
        window.settingsMessage.setText(message)
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

    # Do not show stale progress left by the launcher self-update while the
    # Basic installer is resolving metadata / waiting for pCloud to prepare ZIP.
    _download_progress["name"] = "Preparing Q3Elite Basic..."
    _download_progress["downloaded"] = 0
    _download_progress["total"] = None
    _download_progress["speed"] = 0.0
    window.progressBar.setRange(0, 0)
    window.downloadInfo.setText("Preparing Q3Elite Basic...")

    install_state["base_done"] = False
    install_state["base_ok"] = False
    install_state["q3elite_done"] = False
    install_state["q3elite_ok"] = False
    install_state["post_update_started"] = False
    set_gui_checking("Installing...")
    window.statusDetail.setText("Preparing Q3Elite Basic installation...")
    window.downloadInfo.setText("Preparing Q3Elite Basic...")
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

        # Recovery rule: the required QLmaps PK3 marker is authoritative for
        # installation state.  If a previous first launch was interrupted after
        # payload extraction but before Version.json was committed, do NOT send
        # that usable installation into the normal version-update path.
        if not q3elite_metadata_present():
            print("[recovery] QLmaps payload exists but local Version.json is missing.")
            print("[recovery] Treating Q3Elite as installed; skipping version check this launch.")
            install_state["q3elite_done"] = True
            install_state["q3elite_ok"] = True
            install_state["q3elite_update_done"] = True
            install_state["q3elite_update_ok"] = True
            set_gui_checking("Checking PAKs...")
            fdownload.start()
            return

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
# Q3ELITE IDENTITY VISUAL LAYER
# ============================================================================

class GothicShell(QtWidgets.QFrame):
    """Token-driven Q3Elite shell: cinematic artwork + subtle structural glow."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._background = QtGui.QPixmap()
        self._blurred = QtGui.QPixmap()
        self._texture = QtGui.QPixmap()
        self._ambient_phase = 0.0
        self._ambient_timer = QtCore.QTimer(self)
        self._ambient_timer.timeout.connect(self._advance_ambient)
        self._load_background()
        self._sync_ambient_timer()

    def _sync_ambient_timer(self):
        enabled = theme_bool("background.ambient_motion", True)
        if enabled:
            self._ambient_timer.start(max(30, theme_int("motion.ambient_interval", 60)))
        else:
            self._ambient_timer.stop()

    def _advance_ambient(self):
        cycle = max(4.0, theme_float("motion.ambient_cycle_seconds", 22.0))
        interval = max(30, theme_int("motion.ambient_interval", 60)) / 1000.0
        self._ambient_phase = (self._ambient_phase + interval / cycle) % 1.0
        self.update()

    def _load_background(self):
        mode = str(theme_value("background.mode", "dark_nova") or "dark_nova").casefold()
        self._load_texture()
        # Material-only presets intentionally support an empty artwork path.
        if mode in {"native_glass", "procedural_black", "steel", "paper_cut", "dark_nova"}:
            raw = str(theme_value("background.image", "") or "").strip()
            if not raw:
                return
        candidate = theme_asset("background.image", None)
        candidate = Path(candidate) if candidate is not None else None
        if candidate is not None and candidate.is_file():
            self._background.load(str(candidate))
        if not self._background.isNull():
            self._rebuild_blur()

    def _load_texture(self):
        self._texture = QtGui.QPixmap()
        raw = str(theme_value("background.texture_path", "") or "").strip()
        if not raw:
            return
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = LAUNCHER_DIR / candidate
        if candidate.is_file():
            self._texture.load(str(candidate))

    def reload_theme_visuals(self):
        self._background = QtGui.QPixmap()
        self._blurred = QtGui.QPixmap()
        self._texture = QtGui.QPixmap()
        _clear_obsidian_render_cache()
        self._load_background()
        self._sync_ambient_timer()
        self.update()

    def _cached_obsidian_shell(self, radius):
        phase = self._ambient_phase if theme_bool("background.ambient_motion", False) else 0.0
        key = (round(float(radius), 3), round(float(phase), 4))
        return _render_cached_surface(
            "obsidian-shell", key, self.size(),
            lambda qp, qr: OBSIDIAN_MATERIAL.paint_shell(
                qp, qr, float(radius), ui_theme, phase=phase
            ),
        )

    def _cached_obsidian_rims(self, border_rect, border_radius):
        # Render in local shell coordinates once.  Supersampled irregular rims
        # are among the most expensive Obsidian operations during child hover.
        inset = (
            round(border_rect.left(), 2), round(border_rect.top(), 2),
            round(border_rect.right(), 2), round(border_rect.bottom(), 2),
            round(float(border_radius), 2),
        )
        def render(qp, _qr):
            outer = QtCore.QRectF(border_rect)
            OBSIDIAN_MATERIAL._paint_imperfect_edge(
                qp, outer, float(border_radius), ui_theme,
                seed=17017, intensity=theme_float("material.edge.shell_intensity", 1.22),
            )
            inner_rect = outer.adjusted(2.25, 2.25, -2.25, -2.25)
            inner_radius = max(1.0, float(border_radius) - 2.25)
            OBSIDIAN_MATERIAL._paint_imperfect_edge(
                qp, inner_rect, inner_radius, ui_theme,
                seed=17029, intensity=theme_float("material.edge.inner_intensity", 0.34),
            )
        return _render_cached_surface("obsidian-shell-rims", inset, self.size(), render)

    def _rebuild_blur(self):
        if self._background.isNull():
            return
        small = self._background.scaled(
            theme_int("background.blur_sample_width", 840),
            theme_int("background.blur_sample_height", 472),
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        scene = QtWidgets.QGraphicsScene()
        item = QtWidgets.QGraphicsPixmapItem(small)
        effect = QtWidgets.QGraphicsBlurEffect()
        effect.setBlurRadius(theme_float("background.blur_radius", 10.0))
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        out = QtGui.QPixmap(small.size())
        out.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(out)
        scene.render(painter, QtCore.QRectF(out.rect()), QtCore.QRectF(small.rect()))
        painter.end()
        self._blurred = out

    @staticmethod
    def _cover(pm, size):
        if pm.isNull():
            return QtGui.QPixmap()
        scaled = pm.scaled(
            size, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - size.width()) // 2)
        y = max(0, (scaled.height() - size.height()) // 2)
        return scaled.copy(x, y, size.width(), size.height())

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)

        expanded = bool(self.window().isMaximized() or self.window().isFullScreen())
        radius = 0.0 if expanded else float(theme_value("radius.window", 16))
        rect = QtCore.QRectF(self.rect())
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)

        # Paint a solid dark undercoat all the way to the anti-aliased shell edge.
        # Artwork is inset by ~1 px so partially covered corner pixels can only
        # blend dark UI material with the desktop, never a bright Banner pixel.
        undercoat_key = "effects.window_undercoat" if (os.name == "nt" and pywinstyles is not None and theme_bool("effects.native_backdrop", True)) else "colors.surface.void"
        painter.fillPath(clip, theme_color(undercoat_key, "#020202"))
        safe = 0.0 if expanded else max(0.0, theme_float("background.edge_safe_inset", 1.35))
        art_rect = rect.adjusted(safe, safe, -safe, -safe)
        art_path = QtGui.QPainterPath()
        art_path.addRoundedRect(art_rect, max(1.0, radius - safe), max(1.0, radius - safe))

        painter.save()
        painter.setClipPath(art_path, QtCore.Qt.ClipOperation.IntersectClip)
        mode = str(theme_value("background.mode", "cinematic")).casefold()
        if not self._background.isNull():
            if mode == "noctra_black":
                # V7 keeps a heavily dimmed blurred scene beneath a black material.
                # It reads as black at first glance but gives Acrylic/sidebar glass
                # enough structure to blur instead of blurring a flat #000 surface.
                if not self._blurred.isNull():
                    painter.save()
                    painter.setOpacity(max(0.0, min(1.0, theme_float("background.blur_artwork_opacity", 0.28))))
                    painter.drawPixmap(self.rect(), self._cover(self._blurred, self.size()))
                    painter.restore()
                painter.save()
                painter.setOpacity(max(0.0, min(1.0, theme_float("background.sharp_opacity", 0.055))))
                painter.drawPixmap(self.rect(), self._cover(self._background, self.size()))
                painter.restore()
            elif mode in ("blurred", "cinematic") and not self._blurred.isNull():
                painter.drawPixmap(self.rect(), self._cover(self._blurred, self.size()))
            else:
                painter.drawPixmap(self.rect(), self._cover(self._background, self.size()))

            if mode == "cinematic":
                painter.save()
                painter.setOpacity(max(0.0, min(1.0, theme_float("background.sharp_opacity", 0.38))))
                painter.drawPixmap(self.rect(), self._cover(self._background, self.size()))
                painter.restore()
        painter.restore()

        # V9 procedural placeholder materials.  These keep the presets useful
        # before custom artwork is supplied and, unlike a flat #000 fill, give
        # glass/reflection effects something to catch.
        if mode == "procedural_black":
            base = QtGui.QLinearGradient(0, 0, self.width(), self.height())
            base.setColorAt(0.0, QtGui.QColor(0, 0, 0, 255))
            base.setColorAt(0.52, QtGui.QColor(8, 8, 9, 250))
            base.setColorAt(1.0, QtGui.QColor(0, 0, 0, 255))
            painter.fillRect(self.rect(), base)
            sheen = QtGui.QRadialGradient(self.width()*0.58, self.height()*0.36, max(self.width(), self.height())*.64)
            sheen.setColorAt(0.0, QtGui.QColor(255,255,255,8))
            sheen.setColorAt(1.0, QtGui.QColor(0,0,0,0))
            painter.fillRect(self.rect(), sheen)
        elif mode == "steel":
            steel = QtGui.QLinearGradient(0, 0, self.width(), self.height())
            steel.setColorAt(0.0, QtGui.QColor(1,1,1,255))
            steel.setColorAt(0.34, QtGui.QColor(18,19,21,255))
            steel.setColorAt(0.48, QtGui.QColor(47,49,53,235))
            steel.setColorAt(0.56, QtGui.QColor(12,13,14,250))
            steel.setColorAt(1.0, QtGui.QColor(2,2,2,255))
            painter.fillRect(self.rect(), steel)
            reflection = QtGui.QLinearGradient(self.width()*.18, 0, self.width()*.72, self.height())
            reflection.setColorAt(0.0, QtGui.QColor(255,255,255,0))
            reflection.setColorAt(0.46, QtGui.QColor(255,255,255,16))
            reflection.setColorAt(0.52, QtGui.QColor(255,255,255,40))
            reflection.setColorAt(0.60, QtGui.QColor(255,255,255,0))
            painter.fillRect(self.rect(), reflection)
        elif mode == "obsidian":
            # V15 Obsidian Material Engine: near-black coated surfaces lit like a
            # studio product shot.  No artwork is required; optional user textures
            # can be layered through tokens without changing Python.
            if theme_bool("performance.cache_material_surfaces", True):
                painter.drawPixmap(0, 0, self._cached_obsidian_shell(radius))
            else:
                OBSIDIAN_MATERIAL.paint_shell(
                    painter, QtCore.QRectF(self.rect()), radius, ui_theme, phase=self._ambient_phase
                )

        elif mode == "dark_nova":
            # Dark Nova is artwork-free: graphite/smoke material is generated
            # from neutral gradients so the launcher remains premium without a
            # wallpaper dependency. Optional texture_path is layered later.
            nova = QtGui.QLinearGradient(0, 0, self.width(), self.height())
            nova.setColorAt(0.0, theme_color("background.nova_black", "#070708"))
            nova.setColorAt(0.34, theme_color("background.nova_graphite", "#202024"))
            nova.setColorAt(0.62, theme_color("background.nova_smoke", "#151517"))
            nova.setColorAt(1.0, theme_color("background.nova_deep", "#09090A"))
            painter.fillRect(self.rect(), nova)

            import math
            phase = self._ambient_phase * math.tau
            smoke_a = QtGui.QRadialGradient(
                self.width() * (0.38 + math.sin(phase) * 0.025),
                self.height() * (0.28 + math.cos(phase) * 0.018),
                max(self.width(), self.height()) * 0.62,
            )
            smoke_a.setColorAt(0.0, theme_color("background.nova_smoke_glow", "rgba(255,255,255,18)"))
            smoke_a.setColorAt(0.58, theme_color("background.nova_smoke_mid", "rgba(120,120,126,8)"))
            smoke_a.setColorAt(1.0, QtGui.QColor(0,0,0,0))
            painter.fillRect(self.rect(), smoke_a)

            smoke_b = QtGui.QRadialGradient(
                self.width() * (0.82 - math.cos(phase) * 0.02),
                self.height() * (0.68 + math.sin(phase) * 0.015),
                max(self.width(), self.height()) * 0.48,
            )
            smoke_b.setColorAt(0.0, theme_color("background.nova_shadow_glow", "rgba(0,0,0,105)"))
            smoke_b.setColorAt(1.0, QtGui.QColor(0,0,0,0))
            painter.fillRect(self.rect(), smoke_b)

            # EDC-inspired contrast: a hard-but-faint white key light catches
            # graphite edges while deep diagonal shadows keep the shell black.
            key = QtGui.QLinearGradient(0, 0, self.width() * 0.72, self.height() * 0.55)
            key.setColorAt(0.0, theme_color("background.nova_key_light_edge", "rgba(255,255,255,20)"))
            key.setColorAt(0.18, theme_color("background.nova_key_light", "rgba(255,255,255,8)"))
            key.setColorAt(0.52, QtGui.QColor(255,255,255,0))
            key.setColorAt(1.0, QtGui.QColor(255,255,255,0))
            painter.fillRect(self.rect(), key)

            shadow_cut = QtGui.QLinearGradient(self.width() * 0.28, 0, self.width(), self.height())
            shadow_cut.setColorAt(0.0, QtGui.QColor(0,0,0,0))
            shadow_cut.setColorAt(0.58, theme_color("background.nova_shadow_cut", "rgba(0,0,0,32)"))
            shadow_cut.setColorAt(1.0, theme_color("background.nova_shadow_cut_deep", "rgba(0,0,0,100)"))
            painter.fillRect(self.rect(), shadow_cut)

            rim = QtGui.QLinearGradient(0, 0, 0, min(160, max(40, self.height() // 4)))
            rim.setColorAt(0.0, theme_color("background.nova_top_rim", "rgba(255,255,255,18)"))
            rim.setColorAt(0.12, theme_color("background.nova_top_rim_soft", "rgba(255,255,255,5)"))
            rim.setColorAt(1.0, QtGui.QColor(255,255,255,0))
            painter.fillRect(self.rect(), rim)

        elif mode == "paper_cut":
            paper = QtGui.QLinearGradient(0, 0, self.width(), self.height())
            paper.setColorAt(0.0, QtGui.QColor(244,243,239,255))
            paper.setColorAt(0.58, QtGui.QColor(232,231,227,255))
            paper.setColorAt(1.0, QtGui.QColor(216,214,209,255))
            painter.fillRect(self.rect(), paper)
            cut = QtGui.QLinearGradient(0, self.height()*.62, self.width(), self.height()*.28)
            cut.setColorAt(0.0, QtGui.QColor(0,0,0,0))
            cut.setColorAt(0.50, QtGui.QColor(0,0,0,9))
            cut.setColorAt(0.51, QtGui.QColor(255,255,255,85))
            cut.setColorAt(1.0, QtGui.QColor(255,255,255,0))
            painter.fillRect(self.rect(), cut)
        # native_glass deliberately paints no synthetic backdrop here.

        painter.fillRect(self.rect(), theme_color("background.base_overlay", "rgba(0,0,0,104)"))
        painter.fillRect(self.rect(), theme_color("background.cold_tint", "rgba(21,58,75,24)"))

        vertical = QtGui.QLinearGradient(0, 0, 0, max(1, self.height()))
        vertical.setColorAt(0.0, theme_color("background.top_overlay", "rgba(3,6,9,185)"))
        vertical.setColorAt(0.52, theme_color("background.middle_overlay", "rgba(3,7,10,118)"))
        vertical.setColorAt(1.0, theme_color("background.bottom_overlay", "rgba(2,4,7,194)"))
        painter.fillRect(self.rect(), vertical)

        # Slow ambient motion gives the static banner depth without turning the
        # launcher into an animated wallpaper. The movement is intentionally only
        # a few percent of the window width and runs at a low refresh rate.
        import math
        wave = math.sin(self._ambient_phase * math.tau)
        wave2 = math.cos(self._ambient_phase * math.tau)
        cx = self.width() * (0.56 + wave * 0.025)
        cy = self.height() * (0.34 + wave2 * 0.018)
        glow = QtGui.QRadialGradient(cx, cy, max(self.width(), self.height()) * 0.58)
        glow.setColorAt(0.0, theme_color("background.accent_glow", "rgba(124,207,224,21)"))
        glow.setColorAt(0.42, theme_color("colors.brand.primary_05", "rgba(124,207,224,13)"))
        glow.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), glow)

        # A nearly invisible crimson echo keeps the Quake identity in the cold
        # palette. It should be felt in dark surfaces, not read as a red theme.
        red = QtGui.QRadialGradient(
            self.width() * (0.75 - wave2 * 0.018),
            self.height() * (0.70 + wave * 0.015),
            max(self.width(), self.height()) * 0.40,
        )
        red.setColorAt(0.0, theme_color("background.crimson_glow", "rgba(216,50,50,11)"))
        red.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), red)

        # V11 signature reflection. The gradient itself always uses legal
        # 0..1 stops; animation moves its paint rectangle instead. This avoids
        # QGradient::setColorAt warnings on Windows/Qt builds.
        sweep_color = theme_color("effects.reflection_sweep", "rgba(255,255,255,0)")
        if sweep_color.alpha() > 0 and theme_bool("background.ambient_motion", True):
            sweep_fraction = max(0.06, min(0.35, theme_float("effects.reflection_sweep_width", 0.16)))
            sweep_px = max(80.0, self.width() * sweep_fraction)
            travel = self.width() + sweep_px * 2.0
            left_px = -sweep_px + ((self._ambient_phase * 1.35) % 1.0) * travel
            sweep_rect = QtCore.QRectF(left_px, 0.0, sweep_px, float(self.height()))
            if sweep_rect.right() >= 0.0 and sweep_rect.left() <= self.width():
                grad = QtGui.QLinearGradient(sweep_rect.left(), self.height(), sweep_rect.right(), 0.0)
                grad.setColorAt(0.0, QtGui.QColor(255,255,255,0))
                grad.setColorAt(0.50, sweep_color)
                grad.setColorAt(1.0, QtGui.QColor(255,255,255,0))
                painter.fillRect(sweep_rect, grad)

        # Optional user background texture. V15 ships with texture_path empty;
        # the controls stay tokenized so a future PNG/JPG can be dropped in
        # without touching Python.
        texture_opacity = max(0.0, min(1.0, theme_float("background.texture_opacity", 0.0)))
        if texture_opacity > 0.0 and not self._texture.isNull():
            painter.save()
            painter.setOpacity(texture_opacity)
            texture_scale = max(0.1, theme_float("background.texture_scale", 1.0))
            texture_mode = str(theme_value("background.texture_mode", "cover") or "cover").casefold()
            if texture_mode == "tile":
                target = self._texture.scaled(
                    max(1, round(self._texture.width() * texture_scale)),
                    max(1, round(self._texture.height() * texture_scale)),
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
                painter.drawTiledPixmap(self.rect(), target)
            else:
                scaled_size = QtCore.QSize(
                    max(1, round(self.width() / texture_scale)),
                    max(1, round(self.height() / texture_scale)),
                )
                texture = self._cover(self._texture, scaled_size)
                painter.drawPixmap(self.rect(), texture)
            painter.restore()
        elif theme_bool("background.procedural_texture", False) and texture_opacity > 0.0:
            painter.save()
            painter.setOpacity(texture_opacity)
            painter.setPen(QtGui.QPen(QtGui.QColor(255,255,255,8), 1))
            step = max(18, theme_int("background.procedural_texture_step", 46))
            for y in range(17, self.height(), step):
                offset = (y * 13) % 83
                painter.drawLine(offset, y, min(self.width(), offset + 190), y)
            painter.setPen(QtGui.QPen(QtGui.QColor(255,255,255,10), 1))
            for i in range(34):
                x = (i * 97 + 31) % max(1, self.width())
                y = (i * 53 + 19) % max(1, self.height())
                painter.drawPoint(x, y)
            painter.restore()

        if mode != "obsidian":
            vignette = QtGui.QRadialGradient(
                self.width() * 0.5, self.height() * 0.48, max(self.width(), self.height()) * 0.80
            )
            vignette.setColorAt(0.0, QtGui.QColor(0, 0, 0, 0))
            vignette.setColorAt(1.0, theme_color("background.edge_vignette", "rgba(0,0,0,132)"))
            painter.fillRect(self.rect(), vignette)

        # The window border uses the exact same rounded geometry family as the
        # shell clip.  V4 also asked DWM for native rounding, which meant two
        # slightly different corner radii could overlap.  V5 relies on one
        # anti-aliased Qt curve only, so the hairline cannot drift by 1–2 px.
        painter.setClipping(False)
        border_inset = max(0.5, theme_float("effects.window_border_inset", 0.85))
        border_width = max(0.75, theme_float("effects.window_border_width", 1.0))
        border_rect = rect.adjusted(border_inset, border_inset, -border_inset, -border_inset)
        border_radius = max(1.0, radius - border_inset)
        border_path = QtGui.QPainterPath()
        border_path.addRoundedRect(border_rect, border_radius, border_radius)
        if OBSIDIAN_MATERIAL.enabled(ui_theme):
            # High-contrast shell rims are the place where normal 1 px Qt
            # curves show staircase pixels most clearly. Reuse V17's 3x
            # supersampled material rim for the complete launcher frame.
            if theme_bool("performance.cache_material_surfaces", True):
                painter.drawPixmap(0, 0, self._cached_obsidian_rims(border_rect, border_radius))
            else:
                OBSIDIAN_MATERIAL._paint_imperfect_edge(
                    painter, border_rect, border_radius, ui_theme,
                    seed=17017, intensity=theme_float("material.edge.shell_intensity", 1.22),
                )
                inner_rect = border_rect.adjusted(2.25, 2.25, -2.25, -2.25)
                inner_radius = max(1.0, border_radius - 2.25)
                OBSIDIAN_MATERIAL._paint_imperfect_edge(
                    painter, inner_rect, inner_radius, ui_theme,
                    seed=17029, intensity=theme_float("material.edge.inner_intensity", 0.34),
                )
        elif theme_bool("effects.window_border_enabled", True):
            border_grad = QtGui.QLinearGradient(0, 0, self.width(), self.height())
            border_grad.setColorAt(0.0, theme_color("effects.edge_reflection", "rgba(255,255,255,24)"))
            border_grad.setColorAt(0.16, theme_color("colors.border.strong", "#56585C"))
            border_grad.setColorAt(0.62, theme_color("colors.border.normal", "rgba(255,255,255,42)"))
            border_grad.setColorAt(1.0, theme_color("colors.border.subtle", "#26333D"))
            border_pen = QtGui.QPen(QtGui.QBrush(border_grad), border_width)
            border_pen.setCosmetic(False)
            border_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            border_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            painter.setPen(border_pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPath(border_path)
            inner_rim = theme_color("effects.window_inner_rim", "rgba(255,255,255,0)")
            if inner_rim.alpha() > 0:
                inner_rect = border_rect.adjusted(2.0, 2.0, -2.0, -2.0)
                inner_radius = max(1.0, border_radius - 2.0)
                rim_pen = QtGui.QPen(inner_rim, max(0.5, theme_float("effects.window_inner_rim_width", 0.65)))
                rim_pen.setCosmetic(False)
                rim_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
                rim_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
                painter.setPen(rim_pen)
                painter.drawRoundedRect(inner_rect, inner_radius, inner_radius)
        painter.end()
        # Obsidian already painted the complete shell. Avoid a second QSS/style
        # background pass after the expensive material draw.
        if mode != "obsidian":
            super().paintEvent(event)


class GlassFrame(QtWidgets.QFrame):
    """Low-cost backdrop glass sampling the shell's pre-blurred artwork."""
    def paintEvent(self, event):
        shell = self.window().findChild(GothicShell, "shell")
        if shell is not None and not shell._blurred.isNull():
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            radius = float(theme_value("radius.lg", 14))
            path = QtGui.QPainterPath()
            if self.objectName() == "sidebar":
                # The rail touches the native left edge. V1/V2 left it square,
                # so its blurred backdrop leaked through the shell's rounded
                # top-left/bottom-left pixels. Extend the rounded rectangle past
                # the right edge: left corners stay rounded, right edge stays flat.
                shell_radius = 0.0 if (self.window().isMaximized() or self.window().isFullScreen()) else float(theme_value("radius.window", 15))
                path.addRoundedRect(
                    QtCore.QRectF(0.0, 0.0, self.width() + shell_radius, self.height()),
                    shell_radius, shell_radius,
                )
            else:
                path.addRoundedRect(QtCore.QRectF(self.rect()), radius, radius)
            painter.setClipPath(path)
            top_left = self.mapTo(shell, QtCore.QPoint(0, 0))
            sx = max(0, int(top_left.x() / max(1, shell.width()) * shell._blurred.width()))
            sy = max(0, int(top_left.y() / max(1, shell.height()) * shell._blurred.height()))
            sw = max(1, int(self.width() / max(1, shell.width()) * shell._blurred.width()))
            sh = max(1, int(self.height() / max(1, shell.height()) * shell._blurred.height()))
            src = QtCore.QRect(sx, sy, sw, sh).intersected(shell._blurred.rect())
            if src.isValid():
                painter.drawPixmap(self.rect(), shell._blurred, src)
            overlay_key = "effects.sidebar_glass_overlay" if self.objectName() == "sidebar" else "effects.glass_overlay"
            painter.fillRect(self.rect(), theme_color(overlay_key, "rgba(3,3,4,176)"))
            painter.end()
        super().paintEvent(event)


class SurfaceCard(QtWidgets.QFrame):
    """Stable dark material card.

    V5 inherited GlassFrame, so every scrolling card re-sampled the shell blur.
    That looked like the background was duplicating/scratching while scrolling.
    V6 lets the static shell own the artwork and keeps cards as translucent
    Noctra-style materials painted in one coordinate space.
    """
    def paintEvent(self, event):
        # Obsidian fully owns this frame's background/border. Calling QFrame's
        # stylesheet paint first means rendering a complete surface only to
        # cover it immediately with the material renderer. Skip that redundant
        # pass; non-material themes retain the normal Qt/QSS frame paint.
        material_enabled = OBSIDIAN_MATERIAL.enabled(ui_theme)
        if not material_enabled:
            super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        prop_radius = self.property("materialRadius")
        radius = float(prop_radius) if prop_radius not in (None, "") else float(theme_value("radius.lg", 14))
        rect = QtCore.QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)

        if material_enabled:
            # Matchmaking's nested cards use a tighter radius.
            if self.objectName() in {"matchmakingConditions", "matchmakingServerChoices"}:
                radius = float(theme_value("radius.sm", 7))
            object_name = self.objectName()
            seed = sum(ord(ch) for ch in (object_name or self.__class__.__name__)) + self.width() * 3 + self.height()
            live_material = {
                "matchmakingIntroCard": ("material.matchmaking.card_texture_path", "material.matchmaking.card_texture_opacity"),
                "matchmakingRuleCard": ("material.matchmaking.card_texture_path", "material.matchmaking.card_texture_opacity"),
                "matchmakingConditions": ("material.matchmaking.inset_texture_path", "material.matchmaking.inset_texture_opacity"),
                "matchmakingServerChoices": ("material.matchmaking.inset_texture_path", "material.matchmaking.inset_texture_opacity"),
                "changelogWebBackdrop": ("material.web.texture_path", "material.web.texture_opacity"),
            }
            live_keys = live_material.get(object_name)
            if live_keys is not None:
                tex_key, opacity_key = live_keys
                tex_opacity = theme_float(opacity_key, 0.0)
            else:
                tex_key = self.property("materialTextureKey") or "material.card_texture"
                tex_key = str(tex_key)
                raw_opacity = self.property("materialTextureOpacity")
                try:
                    tex_opacity = float(raw_opacity if raw_opacity not in (None, "") else theme_float("material.card_texture_opacity", 0.15))
                except Exception:
                    tex_opacity = theme_float("material.card_texture_opacity", 0.15)

            def render_card(qp, qr):
                local = qr.adjusted(1.0, 1.0, -1.0, -1.0)
                # QuickLinkCard already draws a cheap animated hover overlay.
                # Keep the expensive physical panel static while it animates.
                OBSIDIAN_MATERIAL.paint_panel(qp, local, radius, ui_theme, seed=seed, hover=0.0)
                if tex_opacity > 0.0:
                    tex = OBSIDIAN_MATERIAL._texture_pixmap(ui_theme, tex_key)
                    if not tex.isNull():
                        path = QtGui.QPainterPath(); path.addRoundedRect(local, radius, radius)
                        target = OBSIDIAN_MATERIAL._cover(tex, local.size().toSize())
                        qp.save(); qp.setClipPath(path)
                        qp.setOpacity(max(0.0, min(1.0, tex_opacity)))
                        qp.drawPixmap(local, target, QtCore.QRectF(target.rect()))
                        qp.restore()

            if theme_bool("performance.cache_material_surfaces", True):
                key = (object_name, round(radius, 2), seed, str(tex_key), round(float(tex_opacity), 4))
                painter.drawPixmap(0, 0, _render_cached_surface("obsidian-card", key, self.size(), render_card))
            else:
                render_card(painter, QtCore.QRectF(self.rect()))
            painter.end()
            return

        # Non-Obsidian themes retain the established V14 card treatment.
        sheen = QtGui.QLinearGradient(rect.topLeft(), rect.topRight())
        sheen.setColorAt(0.0, theme_color("effects.card_inner_highlight", "rgba(230,247,251,18)"))
        sheen.setColorAt(0.50, QtGui.QColor(255, 255, 255, 5))
        sheen.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        painter.setPen(QtGui.QPen(QtGui.QBrush(sheen), 1.0))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, max(2.0, radius - 1.0), max(2.0, radius - 1.0))
        painter.setPen(QtGui.QPen(theme_color("effects.card_bottom_shadow", "rgba(0,0,0,80)"), 1.0))
        painter.drawLine(
            QtCore.QPointF(rect.left() + radius, rect.bottom()),
            QtCore.QPointF(rect.right() - radius, rect.bottom()),
        )
        painter.end()



_SMOOTH_ICON_PIXMAP_CACHE = {}

def _paint_smooth_qta_icon(painter, icon_names, rect, color, *, oversample=None):
    """Render QtAwesome vectors above target resolution then downsample.

    The downsampled result is cached because navigation/material widgets repaint
    frequently while their icon name, color and target size usually do not.

    This avoids the dark fringe / inner-edge speckles that become very visible
    when a tiny black glyph sits on the Obsidian silver material.
    """
    if qta is None:
        return False
    if isinstance(icon_names, str):
        icon_names = (icon_names,)
    names = [str(x) for x in icon_names if x]
    if not names:
        return False
    target = QtCore.QRectF(rect)
    w = max(1, int(round(target.width())))
    h = max(1, int(round(target.height())))
    ss = int(oversample or theme_int("icons.smooth_oversample", 3))
    ss = max(1, min(4, ss))
    revision = getattr(ui_theme, "revision", 0) if ui_theme is not None else 0
    for name in names:
        try:
            cache_key = (revision, name, str(color), w, h, ss)
            low = _SMOOTH_ICON_PIXMAP_CACHE.get(cache_key)
            if low is None or low.isNull():
                icon = qta.icon(name, color=str(color))
                pm = icon.pixmap(QtCore.QSize(w * ss, h * ss))
                if pm.isNull():
                    continue
                low = pm.scaled(w, h, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                QtCore.Qt.TransformationMode.SmoothTransformation)
                _SMOOTH_ICON_PIXMAP_CACHE[cache_key] = low
            x = target.center().x() - low.width() / 2.0
            y = target.center().y() - low.height() / 2.0
            painter.save()
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(QtCore.QPointF(x, y), low)
            painter.restore()
            return True
        except Exception:
            continue
    return False


class MetallicLabel(QtWidgets.QLabel):
    """Single-line label rendered as satin silver in Obsidian.

    It deliberately falls back to QLabel for rich text, wrapping and pixmaps so
    body copy stays crisp/readable while headings and numeric values can catch
    the same light as the physical controls.
    """
    def paintEvent(self, event):
        text = self.text()
        rich = self.textFormat() == QtCore.Qt.TextFormat.RichText or ("<" in text and ">" in text)
        try:
            pm = self.pixmap()
            has_pixmap = pm is not None and not pm.isNull()
        except Exception:
            has_pixmap = False
        if (not OBSIDIAN_MATERIAL.enabled(ui_theme) or self.wordWrap() or rich or has_pixmap):
            return super().paintEvent(event)
        key = (text, self.font().toString(), int(self.alignment()), bool(self.isEnabled()), self.contentsMargins().left(), self.contentsMargins().right())
        def render(qp, _qr):
            rect = QtCore.QRectF(self.contentsRect())
            OBSIDIAN_MATERIAL.paint_metallic_text(
                qp, rect, text, self.font(), ui_theme,
                align=self.alignment(), enabled=self.isEnabled(),
            )
        painter = QtGui.QPainter(self)
        if theme_bool("performance.cache_material_text", True):
            painter.drawPixmap(0, 0, _render_cached_surface("obsidian-metal-text", key, self.size(), render))
        else:
            render(painter, QtCore.QRectF(self.rect()))
        painter.end()


class SilverIconTile(QtWidgets.QLabel):
    """Small icon plate using the same satin-silver material as selected rail items."""
    def __init__(self, icon_name="", object_name="silverIconTile", box=40, icon_px=20, parent=None):
        super().__init__(parent)
        self._theme_icon_name = str(icon_name or "")
        self._icon_px = int(icon_px)
        self.setObjectName(str(object_name))
        self.setProperty("theme_icon_name", self._theme_icon_name)
        self.setFixedSize(int(box), int(box))
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.refresh_theme_icon()

    def refresh_theme_icon(self):
        if OBSIDIAN_MATERIAL.enabled(ui_theme):
            self.setPixmap(QtGui.QPixmap())
            self.update()
            return
        if qta is not None and self._theme_icon_name:
            color = theme_value("icons.tile_icon_color", theme_value("colors.text.primary", "#111111"))
            try:
                self.setPixmap(qta.icon(self._theme_icon_name, color=color).pixmap(self._icon_px, self._icon_px))
                self.setText("")
                return
            except Exception:
                pass
        self.setText("•")

    def paintEvent(self, event):
        if not OBSIDIAN_MATERIAL.enabled(ui_theme):
            return super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QtCore.QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = float(theme_value("radius.md", 10))
        hover = bool(self.parentWidget() is not None and self.parentWidget().underMouse())
        seed = (sum(ord(c) for c in self._theme_icon_name) + self.width()*13)
        intensity = theme_float("material.icon_tile.hover_rim_intensity", 1.12) if hover else theme_float("material.icon_tile.rim_intensity", 0.82)
        def render_tile(qp, qr):
            local = qr.adjusted(1.0, 1.0, -1.0, -1.0)
            path = QtGui.QPainterPath(); path.addRoundedRect(local, radius, radius)
            OBSIDIAN_MATERIAL.paint_silver_surface(qp, path, local, ui_theme)
            OBSIDIAN_MATERIAL._paint_imperfect_edge(
                qp, local.adjusted(0.55,0.55,-0.55,-0.55), radius, ui_theme,
                seed=seed, intensity=intensity, occlusion=False,
            )
        if theme_bool("performance.cache_material_surfaces", True):
            painter.drawPixmap(0, 0, _render_cached_surface("obsidian-silver-tile", (seed, hover, round(radius,2)), self.size(), render_tile))
        else:
            render_tile(painter, QtCore.QRectF(self.rect()))
        if qta is not None and self._theme_icon_name:
            sz = self._icon_px
            r = QtCore.QRectF((self.width()-sz)/2.0, (self.height()-sz)/2.0, sz, sz)
            _paint_smooth_qta_icon(
                painter, self._theme_icon_name, r,
                theme_value("icons.tile_icon_color", theme_value("colors.text.inverse", "#15171A")),
            )
        elif self.text():
            painter.setPen(theme_color("colors.text.inverse", "#080809"))
            painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, self.text())
        painter.end()



class SilverNumberBadge(QtWidgets.QLabel):
    """Compact material number badge used by Matchmaking workflow steps."""
    def __init__(self, text="", parent=None):
        super().__init__(str(text), parent)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(32, 32)

    def paintEvent(self, event):
        if not OBSIDIAN_MATERIAL.enabled(ui_theme):
            return super().paintEvent(event)
        text = self.text()
        seed = sum(ord(c) for c in text) + 191
        intensity = theme_float("material.matchmaking.badge_rim_intensity", 0.9)

        def render_badge(qp, qr):
            qp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            rect = qr.adjusted(1.0, 1.0, -1.0, -1.0)
            path = QtGui.QPainterPath(); path.addEllipse(rect)
            OBSIDIAN_MATERIAL.paint_silver_surface(qp, path, rect, ui_theme)
            OBSIDIAN_MATERIAL._paint_imperfect_edge(
                qp, rect, rect.width()/2.0, ui_theme,
                seed=seed, intensity=intensity, occlusion=False,
            )
            qp.setPen(theme_color("colors.text.inverse", "#15171A"))
            qp.setFont(self.font())
            qp.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, text)

        key = (text, seed, round(float(intensity), 3), self.font().toString())
        pm = _render_cached_surface("obsidian-matchmaking-badge", key, self.size(), render_badge)
        painter = QtGui.QPainter(self)
        painter.drawPixmap(0, 0, pm)
        painter.end()


class MaterialCheckBox(QtWidgets.QCheckBox):
    """Texture-aware check box for Obsidian; cached during scroll/hover."""
    def paintEvent(self, event):
        if not OBSIDIAN_MATERIAL.enabled(ui_theme):
            return super().paintEvent(event)

        checked = self.isChecked()
        hovered = self.underMouse() and not _scroll_perf_active(self)
        pressed = self.isDown()
        enabled = self.isEnabled()
        text = self.text()
        radius = theme_float("material.checkbox.radius", 5.0)
        seed = (sum(ord(c) for c in text) + self.width()*5) & 0xffff
        checked_silver = bool(theme_value("material.checkbox.checked_silver", True))

        def render_checkbox(qp, qr):
            qp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            qp.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            d = 18.0
            y = (qr.height() - d) / 2.0
            box = QtCore.QRectF(0.75, y, d, d)
            if checked and checked_silver:
                path = QtGui.QPainterPath(); path.addRoundedRect(box, radius, radius)
                OBSIDIAN_MATERIAL.paint_silver_surface(qp, path, box, ui_theme)
                OBSIDIAN_MATERIAL._paint_imperfect_edge(
                    qp, box, radius, ui_theme, seed=seed,
                    intensity=1.08 if hovered else 0.82,
                )
                pen = QtGui.QPen(theme_color("colors.text.inverse", "#080809"), 1.8)
                pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
                qp.setPen(pen)
                qp.drawLine(QtCore.QLineF(box.left()+4.2, box.center().y(), box.left()+7.4, box.bottom()-4.5))
                qp.drawLine(QtCore.QLineF(box.left()+7.4, box.bottom()-4.5, box.right()-3.4, box.top()+4.3))
            else:
                OBSIDIAN_MATERIAL.paint_button_surface(
                    qp, box, radius, ui_theme, hovered=hovered, pressed=pressed,
                    checked=False, enabled=enabled, primary=False, seed=seed,
                )
            qp.setFont(self.font())
            qp.setPen(theme_color("colors.text.secondary" if enabled else "colors.text.disabled", "#A8A8AD"))
            text_rect = QtCore.QRectF(28, 0, max(0.0, qr.width()-28.0), qr.height())
            qp.drawText(text_rect, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, text)

        state_key = (
            text, checked, hovered, pressed, enabled, checked_silver,
            round(float(radius), 2), seed, self.font().toString(),
        )
        pm = _render_cached_surface("obsidian-checkbox", state_key, self.size(), render_checkbox)
        painter = QtGui.QPainter(self)
        painter.drawPixmap(0, 0, pm)
        painter.end()


class MaterialHeaderView(QtWidgets.QHeaderView):
    """Obsidian header painted as one surface, including all labels in one pass.

    QHeaderView normally repaints individual sections on hover.  Painting each
    section independently caused two problems here: the graphite texture could
    restart at column boundaries and some labels were not present until that
    section received its first hover repaint.  In Obsidian we therefore paint
    the complete visible header in paintEvent().  Other themes keep Qt/QSS.
    """
    def paintSection(self, painter, rect, logicalIndex):
        if not OBSIDIAN_MATERIAL.enabled(ui_theme):
            return super().paintSection(painter, rect, logicalIndex)
        # Obsidian sections are deliberately painted by paintEvent() as one
        # atomic surface.  Do not let per-section hover updates overwrite it.
        return

    def paintEvent(self, event):
        if not OBSIDIAN_MATERIAL.enabled(ui_theme):
            return super().paintEvent(event)

        visible = [i for i in range(self.count()) if not self.isSectionHidden(i)]
        if not visible:
            return

        painter = QtGui.QPainter(self.viewport())
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)

        # sectionViewportPosition() is already offset-aware, so the material
        # stays continuous while the header is horizontally scrolled.
        left = min(self.sectionViewportPosition(i) for i in visible)
        right = max(self.sectionViewportPosition(i) + self.sectionSize(i) for i in visible)
        surface = QtCore.QRectF(float(left), 0.0, float(max(1, right-left)), float(self.height()))

        # Header strips are very wide and shallow.  The generic material helper
        # uses "cover" scaling, which massively zooms a square texture here.
        # Paint the header locally instead and TILE its dedicated texture across
        # the complete header surface.
        header_radius = theme_float("material.header.radius", 9.0)
        header_path = QtGui.QPainterPath()
        header_path.addRoundedRect(surface, header_radius, header_radius)

        grad = QtGui.QLinearGradient(
            surface.left(), surface.top(), surface.left(), surface.bottom()
        )
        grad.setColorAt(0.0, theme_color("material.header.top", "#18181B"))
        grad.setColorAt(1.0, theme_color("material.header.bottom", "#0A0A0C"))
        painter.fillPath(header_path, grad)

        header_tex = OBSIDIAN_MATERIAL._texture_pixmap(
            ui_theme, "material.header.texture_path"
        )
        if not header_tex.isNull():
            texture_scale = max(
                0.10,
                min(
                    8.0,
                    theme_float("material.header.texture_scale", 1.0),
                ),
            )
            if abs(texture_scale - 1.0) > 0.001:
                header_tex = header_tex.scaled(
                    max(1, int(header_tex.width() * texture_scale)),
                    max(1, int(header_tex.height() * texture_scale)),
                    QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )

            painter.save()
            painter.setClipPath(header_path)
            painter.setOpacity(
                max(
                    0.0,
                    min(
                        1.0,
                        theme_float("material.header.texture_opacity", 0.18),
                    ),
                )
            )

            # One origin for the entire header.  The texture no longer restarts
            # for WEAPON / ACCURACY / HITS / SHOTS / FRAGS.
            painter.drawTiledPixmap(
                surface,
                header_tex,
                QtCore.QPointF(surface.left(), surface.top()),
            )
            painter.restore()

        # Subtle satin light along the top edge.
        top_light = QtGui.QLinearGradient(
            surface.left(), surface.top(), surface.right(), surface.top()
        )
        top_light.setColorAt(
            0.0,
            theme_color(
                "material.header.top_light",
                "rgba(255,255,255,26)",
            ),
        )
        top_light.setColorAt(0.55, QtGui.QColor(255, 255, 255, 5))
        top_light.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        top_pen = QtGui.QPen(QtGui.QBrush(top_light), 0.8)
        top_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(top_pen)
        painter.drawLine(
            QtCore.QLineF(
                surface.left() + header_radius,
                surface.top() + 0.6,
                surface.right() - header_radius,
                surface.top() + 0.6,
            )
        )

        # One outer rim; column dividers are still painted below with the text.
        painter.setPen(
            QtGui.QPen(
                theme_color(
                    "material.header.rim",
                    "rgba(255,255,255,35)",
                ),
                0.8,
            )
        )
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            surface.adjusted(0.4, 0.4, -0.4, -0.4),
            header_radius,
            header_radius,
        )

        header_font = QtGui.QFont(self.font())
        header_font.setPixelSize(theme_int("typography.size.micro", 11))
        try:
            header_font.setWeight(
                QtGui.QFont.Weight(max(100, min(900, theme_int("typography.weight.semibold", 600))))
            )
        except ValueError:
            header_font.setWeight(QtGui.QFont.Weight.DemiBold)
        if ui_theme is not None:
            ui_theme._polish_font(header_font)
        painter.setFont(header_font)

        model = self.model()
        # A dedicated dark-header colour avoids palette/QSS state changes on
        # hover.  It intentionally does not affect White Paper (branch above).
        painter.setPen(theme_color("material.header.text", "#D6D2C9"))

        ordered = sorted(visible, key=self.visualIndex)
        for pos, logical in enumerate(ordered):
            x = self.sectionViewportPosition(logical)
            w = self.sectionSize(logical)
            r = QtCore.QRectF(float(x), 0.0, float(w), float(self.height()))
            if not r.intersects(QtCore.QRectF(self.viewport().rect())):
                continue

            text = model.headerData(
                logical, self.orientation(), QtCore.Qt.ItemDataRole.DisplayRole
            ) if model else ""
            align = model.headerData(
                logical, self.orientation(), QtCore.Qt.ItemDataRole.TextAlignmentRole
            ) if model else None
            try:
                align = QtCore.Qt.AlignmentFlag(int(align)) if align is not None else QtCore.Qt.AlignmentFlag.AlignCenter
            except Exception:
                align = QtCore.Qt.AlignmentFlag.AlignCenter

            painter.drawText(
                r.adjusted(9.0, 0.0, -9.0, 0.0),
                align | QtCore.Qt.AlignmentFlag.AlignVCenter,
                str(text or ""),
            )

            if pos != len(ordered)-1:
                divider = theme_color("colors.border.hairline", "rgba(255,255,255,13)")
                painter.setPen(QtGui.QPen(divider, 1.0))
                dx = r.right() - 0.5
                painter.drawLine(QtCore.QPointF(dx, 5.0), QtCore.QPointF(dx, r.bottom()-5.0))
                painter.setPen(theme_color("material.header.text", "#D6D2C9"))
        painter.end()


class ToggleSwitch(QtWidgets.QAbstractButton):
    """Compact animated switch; stable Obsidian states are cached for scrolling."""
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setText(str(text))
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(30)
        self._position = 0.0
        self._hover_amount = 0.0
        self._anim = QtCore.QPropertyAnimation(self, b"position", self)
        self._anim.setDuration(theme_int("motion.fast", 70))
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._hover_anim = QtCore.QPropertyAnimation(self, b"hoverAmount", self)
        self._hover_anim.setDuration(theme_int("motion.fast", 70))
        self._hover_anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate_state)

    def getPosition(self):
        return self._position

    def setPosition(self, value):
        self._position = max(0.0, min(1.0, float(value)))
        self.update()

    position = QtCore.pyqtProperty(float, fget=getPosition, fset=setPosition)

    def getHoverAmount(self):
        return self._hover_amount

    def setHoverAmount(self, value):
        self._hover_amount = max(0.0, min(1.0, float(value)))
        self.update()

    hoverAmount = QtCore.pyqtProperty(float, fget=getHoverAmount, fset=setHoverAmount)

    def enterEvent(self, event):
        if _scroll_perf_active(self):
            self._hover_anim.stop()
            self._hover_amount = 0.0
            self.update()
        else:
            self._hover_anim.stop()
            self._hover_anim.setStartValue(self._hover_amount)
            self._hover_anim.setEndValue(1.0)
            self._hover_anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_amount)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()
        super().leaveEvent(event)

    def _animate_state(self, checked):
        self._anim.stop()
        self._anim.setDuration(theme_int("motion.fast", 70))
        self._anim.setStartValue(self._position)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def setChecked(self, checked):
        super().setChecked(bool(checked))
        if not self._anim.state() == QtCore.QAbstractAnimation.State.Running:
            self._position = 1.0 if checked else 0.0
            self.update()

    def sizeHint(self):
        fm = self.fontMetrics()
        return QtCore.QSize(48 + 12 + fm.horizontalAdvance(self.text()), max(30, fm.height() + 8))

    @staticmethod
    def _mix_color(a, b, t):
        return QtGui.QColor(
            round(a.red() + (b.red() - a.red()) * t),
            round(a.green() + (b.green() - a.green()) * t),
            round(a.blue() + (b.blue() - a.blue()) * t),
            round(a.alpha() + (b.alpha() - a.alpha()) * t),
        )

    def _paint_switch(self, painter, bounds, position, hover_amount):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        track_w, track_h = 38.0, 20.0
        top = (bounds.height() - track_h) / 2.0
        track = QtCore.QRectF(0.5, top, track_w, track_h)
        enabled = self.isEnabled()
        off = theme_color("toggles.off", "#17191C")
        on = theme_color("toggles.on", "#72D3E6")
        hover_off = theme_color("toggles.hover_off", theme_value("toggles.off", "#17191C"))
        hover_on = theme_color("toggles.hover_on", theme_value("toggles.on", "#72D3E6"))
        if not enabled:
            off = theme_color("toggles.disabled", "#3B3E43")
            on = theme_color("toggles.disabled", "#3B3E43")
            hover_off = off
            hover_on = on
        base_track = self._mix_color(off, on, position)
        hover_track = self._mix_color(hover_off, hover_on, position)
        track_color = self._mix_color(base_track, hover_track, hover_amount)
        border_normal = theme_color("toggles.border", "rgba(255,255,255,45)")
        border_hover = theme_color("toggles.hover_border", theme_value("toggles.border", "rgba(255,255,255,45)"))
        painter.setPen(QtGui.QPen(self._mix_color(border_normal, border_hover, hover_amount), 1.0))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, track_h / 2, track_h / 2)

        if OBSIDIAN_MATERIAL.enabled(ui_theme):
            tex = OBSIDIAN_MATERIAL._texture_pixmap(ui_theme, "material.button_texture")
            if not tex.isNull():
                path = QtGui.QPainterPath(); path.addRoundedRect(track, track_h / 2, track_h / 2)
                target = OBSIDIAN_MATERIAL._cover(tex, track.size().toSize())
                painter.save(); painter.setClipPath(path)
                op = theme_float("material.toggle_texture_opacity", 0.11) + hover_amount * theme_float("material.toggle_hover_texture_boost", 0.035)
                if not enabled:
                    op *= 0.50
                painter.setOpacity(max(0.0, min(1.0, op)))
                painter.drawPixmap(track, target, QtCore.QRectF(target.rect()))
                painter.restore()

        knob_d = 14.0
        x = 3.5 + (track_w - knob_d - 7.0) * position
        knob = QtCore.QRectF(x, top + (track_h - knob_d) / 2, knob_d, knob_d)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        if OBSIDIAN_MATERIAL.enabled(ui_theme):
            kp = QtGui.QPainterPath(); kp.addEllipse(knob)
            if enabled and self.isChecked():
                painter.save(); painter.setClipPath(kp)
                painter.fillPath(kp, theme_color("toggles.knob_on", "#15171A"))
                tex = OBSIDIAN_MATERIAL._texture_pixmap(ui_theme, "material.button_texture")
                if not tex.isNull():
                    target = OBSIDIAN_MATERIAL._cover(tex, knob.size().toSize())
                    painter.setOpacity(theme_float("material.toggle_knob_on_texture_opacity", 0.16))
                    painter.drawPixmap(knob, target, QtCore.QRectF(target.rect()))
                painter.restore()
                rim = QtGui.QPen(theme_color("toggles.knob_on_border", "rgba(255,255,255,90)"), 0.8)
                rim.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
                painter.setPen(rim); painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawEllipse(knob.adjusted(0.35, 0.35, -0.35, -0.35))
            else:
                knob_opacity = 0.96 if enabled else theme_float("material.toggle_knob_disabled_opacity", 0.55)
                OBSIDIAN_MATERIAL.paint_silver_surface(painter, kp, knob, ui_theme, opacity=knob_opacity)
                OBSIDIAN_MATERIAL._paint_imperfect_edge(
                    painter, knob.adjusted(0.35, 0.35, -0.35, -0.35), knob_d / 2.0, ui_theme,
                    seed=7919, intensity=0.55 if enabled else 0.28, occlusion=False,
                )
        else:
            knob_key = "toggles.knob_disabled" if not enabled else ("toggles.knob_on" if self.isChecked() else "toggles.knob_off")
            painter.setBrush(theme_color(knob_key, theme_value("toggles.knob", "#F5F6F7")))
            painter.drawEllipse(knob)

        painter.setPen(theme_color("colors.text.secondary" if enabled else "colors.text.disabled", "#C1CBD2"))
        painter.setFont(self.font())
        painter.drawText(
            QtCore.QRectF(50, 0, max(0.0, bounds.width() - 50), bounds.height()),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )

    def paintEvent(self, event):
        material = OBSIDIAN_MATERIAL.enabled(ui_theme)
        scroll_active = _scroll_perf_active(self)
        position = self._position
        hover_amount = 0.0 if scroll_active else self._hover_amount
        animating = (
            self._anim.state() == QtCore.QAbstractAnimation.State.Running
            or (self._hover_anim.state() == QtCore.QAbstractAnimation.State.Running and not scroll_active)
        )

        painter = QtGui.QPainter(self)
        if material and theme_bool("performance.cache_toggle_surfaces", True) and not animating:
            # Stable switches are among the most frequently repainted controls in
            # Settings/Matchmaking. Cache the complete surface; during scrolling
            # this turns each repaint into a single pixmap blit.
            pos_key = round(float(position), 3)
            hover_key = round(float(hover_amount), 3)
            key = (
                self.text(), bool(self.isChecked()), bool(self.isEnabled()),
                pos_key, hover_key, self.font().toString(),
            )
            pm = _render_cached_surface(
                "obsidian-toggle", key, self.size(),
                lambda qp, qr: self._paint_switch(qp, qr, position, hover_amount),
            )
            painter.drawPixmap(0, 0, pm)
        else:
            self._paint_switch(painter, QtCore.QRectF(self.rect()), position, hover_amount)
        painter.end()


class QuickLinkCard(SurfaceCard):
    """Noctra-style home shortcut with a fast material hover transition."""
    clicked = QtCore.pyqtSignal(str)

    def __init__(self, title, subtitle, page_name, icon_name=None, parent=None):
        super().__init__(parent)
        self.setObjectName("quickLinkCard")
        self.page_name = str(page_name)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(theme_int("home.quick_card_height", 100))
        self._hover_amount = 0.0
        self._hover_anim = QtCore.QPropertyAnimation(self, b"hoverAmount", self)
        self._hover_anim.setDuration(theme_int("motion.fast", 70))
        self._hover_anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(16, 14, 14, 14)
        lay.setSpacing(13)

        self._icon_name = icon_name
        self.iconLabel = SilverIconTile(icon_name or "", "quickLinkIcon", 40, 20, self)
        lay.addWidget(self.iconLabel)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        text.addStretch(1)
        title_label = MetallicLabel(str(title))
        title_label.setObjectName("quickLinkTitle")
        text.addWidget(title_label)
        sub_label = QtWidgets.QLabel(str(subtitle))
        sub_label.setObjectName("quickLinkText")
        text.addWidget(sub_label)
        text.addStretch(1)
        lay.addLayout(text, 1)

        self.arrow = QtWidgets.QLabel("↗")
        self.arrow.setObjectName("quickLinkArrow")
        self.arrow.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.arrow.setFixedWidth(22)
        lay.addWidget(self.arrow)

    def _refresh_icon(self):
        try:
            self.iconLabel._theme_icon_name = str(self._icon_name or "")
            self.iconLabel.setProperty("theme_icon_name", self.iconLabel._theme_icon_name)
            self.iconLabel.refresh_theme_icon()
        except Exception:
            pass


    def getHoverAmount(self):
        return self._hover_amount

    def setHoverAmount(self, value):
        self._hover_amount = max(0.0, min(1.0, float(value)))
        self.update()
        try:
            self.iconLabel.update()
        except Exception:
            pass

    hoverAmount = QtCore.pyqtProperty(float, fget=getHoverAmount, fset=setHoverAmount)

    def _animate_hover(self, target):
        self._hover_anim.stop()
        self._hover_anim.setDuration(theme_int("motion.fast", 70))
        self._hover_anim.setStartValue(self._hover_amount)
        self._hover_anim.setEndValue(float(target))
        self._hover_anim.start()

    def enterEvent(self, event):
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.page_name)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._hover_amount <= 0.001:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        radius = float(theme_value("radius.lg", 13))
        rect = QtCore.QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        c = theme_color("effects.card_hover_glow", "rgba(255,255,255,12)")
        c.setAlpha(round(c.alpha() * self._hover_amount))
        gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, c)
        gradient.setColorAt(0.65, QtGui.QColor(0, 0, 0, 0))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, radius - 1.0, radius - 1.0)
        accent = theme_color("colors.brand.primary", "#D9DDE1")
        accent.setAlpha(round(150 * self._hover_amount))
        painter.setPen(QtGui.QPen(accent, 2.0))
        painter.drawLine(QtCore.QPointF(rect.left() + 1.0, rect.top() + 13),
                         QtCore.QPointF(rect.left() + 1.0, rect.bottom() - 13))
        painter.end()


class FullRowHoverDelegate(QtWidgets.QStyledItemDelegate):
    """Paint one continuous hover material behind a table row, including gaps."""
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.hover_row = -1
        view.setMouseTracking(True)
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        # A theme switch can rebuild ServerCards while queued mouse events still
        # target the old viewport. Never dereference a deleted Qt C++ object.
        try:
            viewport = self.view.viewport()
        except RuntimeError:
            return False
        if obj is viewport:
            if event.type() == QtCore.QEvent.Type.MouseMove:
                try:
                    idx = self.view.indexAt(event.position().toPoint())
                except RuntimeError:
                    return False
                row = idx.row() if idx.isValid() else -1
                if row != self.hover_row:
                    self.hover_row = row
                    viewport.update()
            elif event.type() == QtCore.QEvent.Type.Leave:
                if self.hover_row != -1:
                    self.hover_row = -1
                    viewport.update()
        return super().eventFilter(obj, event)

    def paint(self, painter, option, index):
        if index.column() == 0:
            is_selected = False
            try:
                is_selected = self.view.selectionModel().isRowSelected(index.row(), QtCore.QModelIndex())
            except Exception:
                pass
            row_rect = QtCore.QRect(0, option.rect.top(), self.view.viewport().width(), option.rect.height())
            hovered = index.row() == self.hover_row
            if OBSIDIAN_MATERIAL.enabled(ui_theme):
                painter.save()
                seed = index.row() + 17
                if theme_bool("performance.cache_table_rows", True):
                    size = QtCore.QSize(max(1, row_rect.width()), max(1, row_rect.height()))
                    key = (seed, bool(is_selected), bool(hovered))
                    pm = _render_cached_surface(
                        "obsidian-table-row", key, size,
                        lambda qp, qr: OBSIDIAN_MATERIAL.paint_row(
                            qp, qr, ui_theme, selected=is_selected, hovered=hovered, seed=seed
                        ),
                    )
                    painter.drawPixmap(row_rect.topLeft(), pm)
                else:
                    OBSIDIAN_MATERIAL.paint_row(
                        painter, QtCore.QRectF(row_rect), ui_theme,
                        selected=is_selected, hovered=hovered, seed=seed,
                    )
                painter.restore()
            elif hovered or is_selected:
                color = theme_color(
                    "effects.row_selected" if is_selected else "effects.row_hover",
                    "rgba(255,255,255,22)" if is_selected else "rgba(255,255,255,10)",
                )
                painter.save(); painter.fillRect(row_rect, color); painter.restore()
        clean = QtWidgets.QStyleOptionViewItem(option)
        # The delegate owns whole-row hover/selection painting.  Removing both
        # native focus and selected flags prevents Qt/Windows from drawing
        # per-cell coloured focus strips on top of the unified row material.
        clean.state &= ~QtWidgets.QStyle.StateFlag.State_HasFocus
        clean.state &= ~QtWidgets.QStyle.StateFlag.State_Selected
        clean.state &= ~QtWidgets.QStyle.StateFlag.State_MouseOver
        super().paint(painter, clean, index)


class NavRailIndicator(QtWidgets.QFrame):
    """Animated selection material behind the compact Noctra-style rail."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navIndicator")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        fx = QtWidgets.QGraphicsDropShadowEffect(self)
        fx.setOffset(0, 0)
        fx.setBlurRadius(theme_int("effects.nav_glow_radius", 24))
        fx.setColor(theme_color("colors.brand.primary_26", "rgba(255,255,255,66)"))
        self.setGraphicsEffect(fx)
        self.hide()


class MaterialPushButton(QtWidgets.QPushButton):
    """QPushButton with an Obsidian-painted physical surface.

    White Paper and other themes use Qt/QSS normally. Obsidian bypasses the
    stylesheet's flat panel painting and draws the material below the label,
    so texture opacity, contact shadows and edge reflections work on every
    launcher-created push button instead of only the hero controls.
    """
    _NO_MATERIAL = {"windowButton", "closeButton", "youtubePlayOverlay", "telegramPlayOverlay"}
    _PRIMARY = {
        "playButton", "applyButton", "serverConnectButton", "mapPlayButton",
        "demoPlayButton", "saveSettingsButton", "homeMainButton",
    }

    def _obsidian_material_enabled(self):
        return OBSIDIAN_MATERIAL.enabled(ui_theme) and self.objectName() not in self._NO_MATERIAL

    def paintEvent(self, event):
        if not self._obsidian_material_enabled():
            return super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.45, 0.45, -0.45, -0.45)
        # Navigation is intentionally a true circle instead of the old square
        # QSS plate. Other controls use the theme radius.
        if self.objectName() == "navButton":
            radius = min(rect.width(), rect.height()) * 0.5
        else:
            radius = float(theme_value("radius.md", 10))
        seed = (sum(ord(c) for c in (self.objectName() or self.text() or self.toolTip() or "button")) + self.width() * 7 + self.height() * 11) & 0x7fffffff
        hovered = bool(self.underMouse()) and not _scroll_perf_active(self)
        pressed = bool(self.isDown())
        checked = bool(self.isChecked())
        enabled = bool(self.isEnabled())
        primary = self.objectName() in self._PRIMARY

        def render_surface(qp, qr):
            local = qr.adjusted(0.45, 0.45, -0.45, -0.45)
            if self.objectName() == "navButton" and checked:
                orb = QtGui.QPainterPath(); orb.addEllipse(local)
                OBSIDIAN_MATERIAL.paint_silver_surface(qp, orb, local, ui_theme)
                OBSIDIAN_MATERIAL._paint_imperfect_edge(
                    qp, local.adjusted(0.65,0.65,-0.65,-0.65), radius, ui_theme,
                    seed=seed + 991, intensity=1.18, occlusion=False,
                )
            else:
                OBSIDIAN_MATERIAL.paint_button_surface(
                    qp, local, radius, ui_theme, hovered=hovered, pressed=pressed,
                    checked=checked, enabled=enabled, primary=primary, seed=seed,
                    edge_occlusion=(self.objectName() != "navButton"),
                )

        if theme_bool("performance.cache_material_buttons", True):
            state_key = (self.objectName(), seed, round(radius,2), hovered, pressed, checked, enabled, primary)
            painter.drawPixmap(0, 0, _render_cached_surface("obsidian-button", state_key, self.size(), render_surface))
        else:
            render_surface(painter, QtCore.QRectF(self.rect()))

        # Do not delegate Obsidian labels back to QSS: :hover rules from old
        # component layers could override the palette and produce black text on
        # a black material. V17 owns icon+label painting completely.
        content = QtCore.QRectF(rect).adjusted(8.0, 0.0, -8.0, 0.0)
        if self.isDown():
            content.translate(0.0, 1.0)
        icon = self.icon()
        text = self.text()
        icon_size = self.iconSize()
        custom_nav_icon = False
        if self.objectName() == "navButton" and qta is not None:
            sz = max(1, theme_int("icons.size_navigation", icon_size.width() or 22))
            ir = QtCore.QRectF(content.center().x()-sz/2.0, content.center().y()-sz/2.0, sz, sz)
            nav_color = theme_value("icons.nav_active_color", "#15171A") if self.isChecked() else theme_value("icons.nav_idle_color", "#E8E8EA")
            custom_nav_icon = _paint_smooth_qta_icon(
                painter,
                (getattr(self, "_q3_icon_preferred", None), getattr(self, "_q3_icon_fallback", None)),
                ir, nav_color,
            )
        has_icon = (not custom_nav_icon) and (not icon.isNull()) and icon_size.width() > 0 and icon_size.height() > 0
        fm = QtGui.QFontMetricsF(self.font())
        text_w = fm.horizontalAdvance(text) if text else 0.0
        gap = 7.0 if has_icon and text else 0.0
        total = (float(icon_size.width()) if has_icon else 0.0) + gap + text_w
        x = content.center().x() - total / 2.0
        if has_icon:
            iy = content.center().y() - icon_size.height() / 2.0
            mode = QtGui.QIcon.Mode.Normal if self.isEnabled() else QtGui.QIcon.Mode.Disabled
            state = QtGui.QIcon.State.On if self.isChecked() else QtGui.QIcon.State.Off
            icon.paint(
                painter,
                QtCore.QRect(int(round(x)), int(round(iy)), icon_size.width(), icon_size.height()),
                QtCore.Qt.AlignmentFlag.AlignCenter, mode, state,
            )
            x += icon_size.width() + gap
        if text:
            tr = QtCore.QRectF(x, content.top(), max(0.0, content.right() - x), content.height())
            if self.objectName() == "navButton" and self.isChecked():
                painter.setPen(theme_color("colors.text.inverse", "#080809"))
                painter.setFont(self.font())
                painter.drawText(tr, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, text)
            else:
                align = QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
                if theme_bool("performance.cache_material_text", True):
                    text_size = QtCore.QSize(max(1, int(round(tr.width()))), max(1, int(round(tr.height()))))
                    text_key = (text, self.font().toString(), bool(self.isEnabled()), int(align))
                    text_pm = _render_cached_surface(
                        "obsidian-button-text", text_key, text_size,
                        lambda qp, qr: OBSIDIAN_MATERIAL.paint_metallic_text(
                            qp, qr, text, self.font(), ui_theme, align=align, enabled=self.isEnabled()
                        ),
                    )
                    painter.drawPixmap(QtCore.QPointF(tr.left(), tr.top()), text_pm)
                else:
                    OBSIDIAN_MATERIAL.paint_metallic_text(
                        painter, tr, text, self.font(), ui_theme, align=align, enabled=self.isEnabled(),
                    )
        painter.end()


class MaterialScrollBar(QtWidgets.QScrollBar):
    """Cached Obsidian scrollbar: original material look without per-tick rerendering.

    The track and handle are rasterized independently. Scrolling only moves the
    cached handle pixmap, so Carbon/metal texture, gradient and imperfect rim do
    not get regenerated for every scrollbar value change.
    """
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setMouseTracking(True)
        self._last_handle_hover = False

    def _handle_rect(self):
        opt = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_ScrollBar,
            opt,
            QtWidgets.QStyle.SubControl.SC_ScrollBarSlider,
            self,
        )

    @staticmethod
    def _material_texture(token_path, size):
        try:
            tex = OBSIDIAN_MATERIAL._texture_pixmap(ui_theme, token_path)
            if tex is None or tex.isNull():
                return QtGui.QPixmap()
            return OBSIDIAN_MATERIAL._cover(tex, size)
        except Exception:
            return QtGui.QPixmap()

    def _cached_track(self):
        orientation = int(self.orientation().value)
        radius = theme_float("material.scrollbar.radius", 7.0)

        def render(qp, qr):
            r = qr.adjusted(0.5, 0.5, -0.5, -0.5)
            path = QtGui.QPainterPath()
            path.addRoundedRect(r, radius, radius)
            qp.fillPath(path, theme_color("material.scrollbar.track", "rgba(0,0,0,150)"))

            opacity = max(0.0, min(1.0, theme_float("material.scrollbar.track_texture_opacity", 0.07)))
            if opacity > 0.0:
                tex = self._material_texture("material.card_texture", r.size().toSize())
                if not tex.isNull():
                    qp.save()
                    qp.setClipPath(path)
                    qp.setOpacity(opacity)
                    qp.drawPixmap(r.toRect(), tex)
                    qp.restore()

            qp.save()
            qp.setPen(QtGui.QPen(theme_color("colors.border.hairline", "rgba(255,255,255,13)"), 1.0))
            qp.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            qp.drawPath(path)
            qp.restore()

        return _render_cached_surface(
            "obsidian-scrollbar-track",
            (orientation, round(radius, 2)),
            self.size(),
            render,
        )

    def _cached_handle(self, handle_size, hovered):
        orientation = int(self.orientation().value)
        radius = theme_float("material.scrollbar.handle_radius", 6.0)
        top_token = "material.scrollbar.handle_top_hover" if hovered else "material.scrollbar.handle_top"
        bottom_token = "material.scrollbar.handle_bottom_hover" if hovered else "material.scrollbar.handle_bottom"
        top_default = "#55555B" if hovered else "#35353A"
        bottom_default = "#202024" if hovered else "#121214"

        def render(qp, qr):
            r = qr.adjusted(0.6, 0.6, -0.6, -0.6)
            path = QtGui.QPainterPath()
            path.addRoundedRect(r, radius, radius)

            # The old material used a light-to-deep graphite gradient. Keep the
            # lighting vertical even for horizontal bars so both orientations
            # feel like the same physical coated part.
            grad = QtGui.QLinearGradient(0.0, r.top(), 0.0, r.bottom())
            grad.setColorAt(0.0, theme_color(top_token, top_default))
            grad.setColorAt(0.46, theme_color(top_token, top_default).darker(108))
            grad.setColorAt(1.0, theme_color(bottom_token, bottom_default))
            qp.fillPath(path, grad)

            opacity = max(0.0, min(1.0, theme_float("material.scrollbar.handle_texture_opacity", 0.18)))
            if opacity > 0.0:
                tex = self._material_texture("material.button_texture", r.size().toSize())
                if not tex.isNull():
                    qp.save()
                    qp.setClipPath(path)
                    qp.setOpacity(opacity)
                    qp.drawPixmap(r.toRect(), tex)
                    qp.restore()

            # Expensive irregular rim is now paid only once per size/state.
            try:
                OBSIDIAN_MATERIAL._paint_imperfect_edge(
                    qp, r, radius, ui_theme,
                    seed=42017 + orientation,
                    intensity=0.92 if hovered else 0.72,
                )
            except Exception:
                qp.save()
                qp.setPen(QtGui.QPen(theme_color("colors.border.normal", "rgba(255,255,255,62)"), 1.0))
                qp.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                qp.drawPath(path)
                qp.restore()

        return _render_cached_surface(
            "obsidian-scrollbar-handle",
            (orientation, bool(hovered), round(radius, 2)),
            handle_size,
            render,
        )

    def paintEvent(self, event):
        if not OBSIDIAN_MATERIAL.enabled(ui_theme):
            return super().paintEvent(event)

        handle = self._handle_rect()
        pos = self.mapFromGlobal(QtGui.QCursor.pos())
        hovered = bool(handle.contains(pos))
        self._last_handle_hover = hovered

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(0, 0, self._cached_track())
        if handle.width() > 0 and handle.height() > 0:
            hp = self._cached_handle(handle.size(), hovered)
            painter.drawPixmap(handle.topLeft(), hp)
        painter.end()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        handle = self._handle_rect()
        hovered = handle.contains(event.position().toPoint())
        if hovered != self._last_handle_hover:
            self._last_handle_hover = hovered
            self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        self._last_handle_hover = False
        super().leaveEvent(event)
        self.update()


def _install_obsidian_scrollbars(root):
    """Install cached material scrollbars for Obsidian, native bars elsewhere."""
    if theme_bool("performance.native_scrollbars", False):
        return
    if not OBSIDIAN_MATERIAL.enabled(ui_theme):
        return
    for area in root.findChildren(QtWidgets.QAbstractScrollArea):
        try:
            old = area.verticalScrollBar()
            if old is not None and not isinstance(old, MaterialScrollBar):
                bar = MaterialScrollBar(QtCore.Qt.Orientation.Vertical, area)
                bar.setRange(old.minimum(), old.maximum())
                bar.setValue(old.value())
                bar.setSingleStep(old.singleStep())
                bar.setPageStep(old.pageStep())
                area.setVerticalScrollBar(bar)
            oldh = area.horizontalScrollBar()
            if oldh is not None and not isinstance(oldh, MaterialScrollBar):
                barh = MaterialScrollBar(QtCore.Qt.Orientation.Horizontal, area)
                barh.setRange(oldh.minimum(), oldh.maximum())
                barh.setValue(oldh.value())
                barh.setSingleStep(oldh.singleStep())
                barh.setPageStep(oldh.pageStep())
                area.setHorizontalScrollBar(barh)
        except (RuntimeError, TypeError):
            continue


class GlowButton(MaterialPushButton):
    """Small interaction halo. Checked navigation keeps a restrained active glow."""
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        fx = QtWidgets.QGraphicsDropShadowEffect(self)
        fx.setOffset(0, 0)
        fx.setBlurRadius(0)
        fx.setColor(theme_color("colors.brand.primary", "#D9DDE1"))
        # A QGraphicsEffect can force offscreen composition even at blur=0.
        # Keep it completely disabled until a real glow is requested.
        fx.setEnabled(False)
        self.setGraphicsEffect(fx)
        self._glow = fx
        self._anim = QtCore.QPropertyAnimation(fx, b"blurRadius", self)
        self._anim.setDuration(theme_int("motion.normal", 165))
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)

    def paintEvent(self, event):
        # MaterialPushButton owns the full Obsidian surface and edge.
        # Painting a second edge here caused doubled/jagged highlights.
        return super().paintEvent(event)

    def _to(self, value):
        self._anim.stop()
        value = max(0.0, float(value))
        theme_has_glow = max(theme_int("effects.hover_glow_radius", 0), theme_int("effects.active_glow_radius", 0)) > 0
        if not theme_has_glow:
            self._glow.setBlurRadius(0)
            self._glow.setEnabled(False)
            return
        if value <= 0.0 and self._glow.blurRadius() <= 0.01:
            self._glow.setBlurRadius(0)
            self._glow.setEnabled(False)
            return
        self._glow.setEnabled(True)
        self._anim.setDuration(theme_int("motion.normal", 165))
        self._glow.setColor(theme_color("colors.brand.primary", "#D9DDE1"))
        self._anim.setStartValue(self._glow.blurRadius())
        self._anim.setEndValue(value)
        if value <= 0.0:
            try:
                self._anim.finished.disconnect(self._disable_glow_if_idle)
            except (TypeError, RuntimeError):
                pass
            self._anim.finished.connect(self._disable_glow_if_idle)
        self._anim.start()

    def _disable_glow_if_idle(self):
        if self._glow.blurRadius() <= 0.01:
            self._glow.setEnabled(False)
        try:
            self._anim.finished.disconnect(self._disable_glow_if_idle)
        except (TypeError, RuntimeError):
            pass

    def refresh_theme_effect(self):
        enabled = max(theme_int("effects.hover_glow_radius", 0), theme_int("effects.active_glow_radius", 0)) > 0
        self._glow.setColor(theme_color("colors.brand.primary", "#D9DDE1"))
        active = enabled and self._glow.blurRadius() > 0.01
        self._glow.setEnabled(active)
        if not enabled:
            self._anim.stop()
            self._glow.setBlurRadius(0)

    def refresh_active_glow(self):
        target = theme_int("effects.active_glow_radius", 19) if self.isCheckable() and self.isChecked() else 0
        self._to(target)

    def enterEvent(self, event):
        self._to(theme_int("effects.hover_glow_radius", 15))
        super().enterEvent(event)

    def leaveEvent(self, event):
        target = theme_int("effects.active_glow_radius", 19) if self.isCheckable() and self.isChecked() else 0
        self._to(target)
        super().leaveEvent(event)


class ThemedIconButton(MaterialPushButton):
    """QPushButton whose vector icon has separate normal/hover theme colors."""
    def __init__(self, icon_name="", normal_path="icons.action_icon_color", hover_path="icons.action_icon_hover_color", parent=None):
        super().__init__(parent)
        self._theme_icon_name = str(icon_name or "")
        self._normal_path = str(normal_path)
        self._hover_path = str(hover_path)
        self._hovered = False
        self._refresh_theme_icon()

    def _refresh_theme_icon(self):
        if qta is None or not self._theme_icon_name:
            return
        path = self._hover_path if self._hovered else self._normal_path
        fallback = theme_value(self._normal_path, theme_value("colors.text.secondary", "#D2D2D2"))
        color = theme_value(path, fallback)
        try:
            self.setIcon(qta.icon(self._theme_icon_name, color=color))
        except Exception:
            pass

    def enterEvent(self, event):
        self._hovered = True
        self._refresh_theme_icon()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._refresh_theme_icon()
        super().leaveEvent(event)


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
            pm=_load_pixmap_file(path)
            if not pm.isNull(): self.setPixmap(pm.scaled(size,size,QtCore.Qt.AspectRatioMode.KeepAspectRatio,QtCore.Qt.TransformationMode.SmoothTransformation)); return
        self.setText('⛧'); self.setStyleSheet('color:#a00000;font-size:22px;background:transparent;')



class MaterialDialog(QtWidgets.QDialog):
    """Top-level dialog using the same Obsidian card material as the launcher."""
    def paintEvent(self, event):
        super().paintEvent(event)
        if not OBSIDIAN_MATERIAL.enabled(ui_theme):
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(1.0,1.0,-1.0,-1.0)
        OBSIDIAN_MATERIAL.paint_panel(
            painter, rect, float(theme_value("radius.lg", 14)), ui_theme,
            seed=(id(self) & 0xffff) + self.width() * 3 + self.height(), hover=0.0,
        )
        painter.end()


class ConfigEditorDialog(MaterialDialog):
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
        frame = SurfaceCard()
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
        find_prev = MaterialPushButton("↑")
        find_next = MaterialPushButton("↓")
        find_close = MaterialPushButton("×")
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
    QWebEnginePage = None
    TelegramExternalPage = None


class TelegramTextView(QWebEngineView if QWebEngineView is not None else QtWidgets.QWidget):
    """Telegram's real renderer, preserving Telegram custom/animated emoji.

    The view loads the official Telegram post surface and only prunes unrelated
    chrome after Telegram has rendered the message DOM.  Unlike the older
    transparent Chromium path, this widget is intentionally opaque so Windows
    does not have to recompose the launcher's translucent top-level HWND when a
    post becomes visible.
    """
    mediaDetected = QtCore.pyqtSignal(object)
    ready = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)
    def __init__(self, url, parent=None, preloaded_page=None, preloaded_ready=True):
        if QWebEngineView is None:
            super().__init__(parent)
            return

        super().__init__(parent)
        self._source_url = str(url or "").strip()
        self._endpoint_urls = self._candidate_urls(self._source_url)
        self._endpoint_index = 0
        self._dom_retry = 0
        self._ready_emitted = False
        self.setObjectName("telegramTextView")
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.NoContextMenu)
        self._using_preloaded_page = preloaded_page is not None
        if preloaded_page is not None:
            # Reuse the exact page that already navigated to Telegram during
            # low-priority warm-up. This avoids a second HTTP/navigation pass.
            try:
                preloaded_page.setParent(self)
            except Exception:
                pass
            self.setPage(preloaded_page)
        else:
            self.setPage(TelegramExternalPage(self))
        self.setMinimumHeight(40)
        self.setMaximumHeight(900)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        # IMPORTANT: QWebEngineView inside a translucent frameless top-level
        # window must own every pixel of its rectangle. A transparent Chromium
        # surface makes DWM recompose the entire launcher and looks like a full
        # restart/redraw.  Paint the WebEngine viewport with the same changelog
        # material base instead; the inner Telegram DOM still gets its rounded
        # texture/rim from the injected CSS below.
        web_bg = theme_color("changelog.web_bg", "#050505")
        web_bg.setAlpha(255)
        self.page().setBackgroundColor(web_bg)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        palette = self.palette()
        palette.setColor(QtGui.QPalette.ColorRole.Window, web_bg)
        palette.setColor(QtGui.QPalette.ColorRole.Base, web_bg)
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.loadFinished.connect(self._telegram_loaded)
        self.titleChanged.connect(self._telegram_title_changed)

        # Stage Chromium while hidden. Collapsed cards already followed this
        # lifecycle and rendered correctly; visible startup/reload navigation
        # could punch transparent WebEngine pixels through the translucent
        # Windows top-level surface.
        self.hide()
        self.setFixedHeight(40)
        if self._using_preloaded_page:
            if preloaded_ready:
                # loadFinished already fired before this view existed. Process
                # the retained Telegram DOM without another navigation.
                QtCore.QTimer.singleShot(0, lambda: self._telegram_loaded(True))
            # Otherwise the retained page is still navigating; this view's
            # loadFinished signal will receive that same in-flight navigation.
        else:
            self._load_current_endpoint()

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

    @staticmethod
    def _candidate_urls(url):
        value = str(url or "").strip()
        m = re.search(
            r"https?://t\.me/(?:s/)?([^/?#]+)/(?:(?:s)/)?(\d+)",
            value, flags=re.I,
        )
        if not m:
            return [value] if value else []
        channel, post_id = m.group(1), m.group(2)
        # Telegram's embed document is the closest thing to the official Post
        # Widget while remaining a top-level document we can theme/prune. It
        # preserves custom emoji animation. Public-channel URLs are fallbacks.
        return [
            f"https://t.me/{channel}/{post_id}?embed=1&mode=tme",
            f"https://t.me/s/{channel}/{post_id}",
            f"https://t.me/{channel}/{post_id}?embed=1",
        ]

    def _load_current_endpoint(self):
        if not self._endpoint_urls:
            self.failed.emit("Invalid Telegram post URL")
            return
        self._endpoint_index = max(0, min(self._endpoint_index, len(self._endpoint_urls) - 1))
        self._dom_retry = 0
        self.hide()
        self.setFixedHeight(40)
        self.setUrl(QtCore.QUrl(self._endpoint_urls[self._endpoint_index]))

    def _retry_endpoint_or_fail(self, reason):
        if self._endpoint_index + 1 < len(self._endpoint_urls):
            self._endpoint_index += 1
            QtCore.QTimer.singleShot(120, self._load_current_endpoint)
            return
        self.hide()
        self.failed.emit(str(reason or "Telegram post could not be rendered"))

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
        # Retry from the preferred official embed endpoint.
        self._endpoint_index = 0
        self._ready_emitted = False
        self._load_current_endpoint()

    def _telegram_loaded(self, ok):
        if not ok:
            self._retry_endpoint_or_fail("Telegram endpoint failed to load")
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
                    width: 100% !important;
                    min-width: 100% !important;
                    max-width: 100% !important;
                    box-sizing: border-box !important;
                    /* Keep Chromium fully opaque. This avoids DWM compositor
                       churn in the launcher's translucent top-level window. */
                    background-color: __Q3_BG__ !important;
                    background-image: none !important;
                    overflow: hidden !important;
                    color: __Q3_TEXT__ !important;
                }
                body {
                    display: block !important;
                    width: 100vw !important;
                    min-width: 100vw !important;
                    max-width: 100vw !important;
                }
                body, .tgme_widget_message_text {
                    font-family: "Segoe UI", Arial, sans-serif !important;
                    font-size: 13px !important;
                    line-height: 1.48 !important;
                    color: __Q3_TEXT__ !important;
                    margin: 0 !important;
                }
                body {
                    background-color: __Q3_BG__ !important;
                    background-image: none !important;
                    padding: 0 !important;
                }
                .tgme_widget_message_text, .tgme_widget_message_text * {
                    background-color: transparent !important;
                }
                .tgme_widget_message_text blockquote,
                .tgme_widget_message_text [class*="quote"],
                .tgme_widget_message_text pre,
                .tgme_widget_message_text code {
                    background-color: __Q3_QUOTE__ !important;
                    background-image: __Q3_QUOTE_IMAGE__ !important;
                    background-size: cover !important;
                    background-position: center !important;
                    color: __Q3_TEXT__ !important;
                    border: 1px solid __Q3_QUOTE_BORDER__ !important;
                    border-radius: __Q3_QUOTE_RADIUS__px !important;
                    box-sizing: border-box !important;
                    box-shadow:
                        inset 1px 1px 0 rgba(255,255,255,0.12),
                        inset -1px -1px 0 rgba(0,0,0,0.48) !important;
                }
                .tgme_widget_message_text blockquote *,
                .tgme_widget_message_text [class*="quote"] *,
                .tgme_widget_message_text pre *,
                .tgme_widget_message_text code * {
                    color: __Q3_TEXT__ !important;
                    background-color: transparent !important;
                }
                .tgme_widget_message_text {
                    display: block !important;
                    width: 100vw !important;
                    min-width: 100vw !important;
                    max-width: 100vw !important;
                    min-height: 100% !important;
                    flex: none !important;
                    align-self: stretch !important;
                    position: relative !important;
                    isolation: isolate !important;
                    background-color: __Q3_BG__ !important;
                    background-image: __Q3_BG_IMAGE__ !important;
                    background-size: cover !important;
                    background-position: center !important;
                    border: 0 !important;
                    border-radius: __Q3_WEB_INNER_RADIUS__px !important;
                    padding: __Q3_WEB_PADDING__px !important;
                    box-sizing: border-box !important;
                    background-clip: padding-box !important;
                    /* Only a tiny inner bevel remains in Chromium. The real
                       directional/specular rim is painted natively by the
                       SurfaceCard underneath, exactly like Matchmaking. */
                    box-shadow:
                        inset 1px 1px 0 rgba(255,255,255,0.035),
                        inset -1px -1px 0 rgba(0,0,0,0.22) !important;
                    overflow: hidden !important;
                }
                a { color: __Q3_LINK__ !important; }
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

            return Math.ceil(text.getBoundingClientRect().height);
        })();
        """
        web_bg_color = theme_color("changelog.web_bg", "#050505")
        web_bg_css = web_bg_color.name()
        web_text_css = theme_color("changelog.web_text", "#E3E3E3").name()
        web_link_css = theme_color("changelog.web_link", "#C7C7C7").name()
        quote_color = theme_color("changelog.quote_bg", theme_value("changelog.web_bg", "#050505"))
        quote_css = quote_color.name()
        quote_border_css = css_color(theme_value("material.web.quote_border", "rgba(255,255,255,46)"))

        # Telegram content is Chromium-rendered, so Qt's SurfaceCard edge pass
        # cannot reach it. Feed the same Obsidian edge language into the DOM.
        tg_border_css = css_color(theme_value(
            "material.web.telegram_content.border_base",
            theme_value("material.web.border_base", theme_value("material.edge.base", "rgba(255,255,255,22)")),
        ))
        tg_border_width = theme_float(
            "material.web.telegram_content.border_width",
            theme_float("material.web.border_width", theme_float("material.edge.base_width", 1.0)),
        )
        tg_specular_css = css_color(theme_value(
            "material.web.telegram_content.specular",
            theme_value("material.web.specular", theme_value("material.edge.specular", "rgba(255,255,255,92)")),
        ))
        tg_specular_width = theme_float(
            "material.web.telegram_content.specular_width",
            theme_float("material.web.specular_width", theme_float("material.edge.specular_width", 0.95)),
        )
        tg_occlusion_css = css_color(theme_value(
            "material.web.telegram_content.occlusion",
            theme_value("material.web.occlusion", theme_value("material.edge.occlusion", "rgba(0,0,0,190)")),
        ))
        tg_occlusion_width = theme_float(
            "material.web.telegram_content.occlusion_width",
            theme_float("material.web.occlusion_width", theme_float("material.edge.occlusion_width", 1.15)),
        )
        tg_inner_rim_css = css_color(theme_value(
            "material.web.telegram_content.inner_rim",
            theme_value("effects.window_inner_rim", "rgba(255,255,255,28)"),
        ))
        tg_specular_soft_css = css_color(theme_value(
            "material.web.telegram_content.specular_soft",
            "rgba(255,255,255,40)",
        ))
        tg_specular_low_css = css_color(theme_value(
            "material.web.telegram_content.specular_low",
            "rgba(255,255,255,20)",
        ))

        bg_uri = OBSIDIAN_MATERIAL.texture_data_uri(
            ui_theme, "material.web.texture_path",
            opacity=theme_float("material.web.texture_opacity", 0.16),
            base_color=web_bg_color, max_size=640,
        ) if OBSIDIAN_MATERIAL.enabled(ui_theme) else ""
        quote_uri = OBSIDIAN_MATERIAL.texture_data_uri(
            ui_theme, "material.card_texture",
            opacity=theme_float("material.web.quote_texture_opacity", 0.10),
            base_color=quote_color, max_size=420,
        ) if OBSIDIAN_MATERIAL.enabled(ui_theme) else ""
        bg_image_css = f'url("{bg_uri}")' if bg_uri else "none"
        quote_image_css = f'url("{quote_uri}")' if quote_uri else "none"
        web_radius = int(theme_float("material.web.radius", 12.0)) if OBSIDIAN_MATERIAL.enabled(ui_theme) else int(theme_float("radius.lg", 13.0))
        web_padding = int(theme_float("material.web.padding", 12.0)) if OBSIDIAN_MATERIAL.enabled(ui_theme) else 10
        web_inner_radius = max(4, web_radius - 2)
        quote_radius = max(4, web_radius - 3)
        js = (js.replace("__Q3_BG__", web_bg_css)
                .replace("__Q3_TEXT__", web_text_css)
                .replace("__Q3_LINK__", web_link_css)
                .replace("__Q3_QUOTE__", quote_css)
                .replace("__Q3_BG_IMAGE__", bg_image_css)
                .replace("__Q3_QUOTE_IMAGE__", quote_image_css)
                .replace("__Q3_QUOTE_BORDER__", quote_border_css)
                .replace("__Q3_WEB_RADIUS__", str(web_radius))
                .replace("__Q3_WEB_INNER_RADIUS__", str(web_inner_radius))
                .replace("__Q3_WEB_PADDING__", str(web_padding))
                .replace("__Q3_WEB_BORDER__", tg_border_css)
                .replace("__Q3_WEB_BORDER_WIDTH__", f"{tg_border_width:.2f}")
                .replace("__Q3_WEB_SPECULAR__", tg_specular_css)
                .replace("__Q3_WEB_SPECULAR_WIDTH__", f"{tg_specular_width:.2f}")
                .replace("__Q3_WEB_OCCLUSION__", tg_occlusion_css)
                .replace("__Q3_WEB_OCCLUSION_WIDTH__", f"{tg_occlusion_width:.2f}")
                .replace("__Q3_WEB_INNER_RIM__", tg_inner_rim_css)
                .replace("__Q3_WEB_SPECULAR_SOFT__", tg_specular_soft_css)
                .replace("__Q3_WEB_SPECULAR_LOW__", tg_specular_low_css)
                .replace("__Q3_QUOTE_RADIUS__", str(quote_radius)))
        self.page().runJavaScript(js, self._apply_loaded_height)
        # Animated/custom emoji and web fonts can settle after loadFinished.
        # Re-measure the retained text shortly afterwards so first load has the
        # same compact geometry as a manual Reload.
        QtCore.QTimer.singleShot(180, self._remeasure_height)
        QtCore.QTimer.singleShot(650, self._remeasure_height)

    def apply_theme_palette(self):
        """Recolour/retexture the already-pruned Telegram DOM after ThemeHub switches."""
        if QWebEngineView is None:
            return
        bg = theme_color("changelog.web_bg", "#050505")
        fg = theme_color("changelog.web_text", "#E3E3E3")
        link = theme_color("changelog.web_link", "#C7C7C7")
        quote = theme_color("changelog.quote_bg", theme_value("changelog.web_bg", "#050505"))
        try:
            opaque_bg = QtGui.QColor(bg); opaque_bg.setAlpha(255)
            self.page().setBackgroundColor(opaque_bg)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
            palette = self.palette()
            palette.setColor(QtGui.QPalette.ColorRole.Window, opaque_bg)
            palette.setColor(QtGui.QPalette.ColorRole.Base, opaque_bg)
            self.setPalette(palette); self.setAutoFillBackground(True)
            bg_uri = OBSIDIAN_MATERIAL.texture_data_uri(
                ui_theme, "material.web.texture_path",
                opacity=theme_float("material.web.texture_opacity", 0.16),
                base_color=bg, max_size=640,
            ) if OBSIDIAN_MATERIAL.enabled(ui_theme) else ""
            quote_uri = OBSIDIAN_MATERIAL.texture_data_uri(
                ui_theme, "material.web.quote_texture_path",
                opacity=theme_float("material.web.quote_texture_opacity", 0.10),
                base_color=quote, max_size=420,
            ) if OBSIDIAN_MATERIAL.enabled(ui_theme) else ""
            bg_image = f'url("{bg_uri}")' if bg_uri else 'none'
            quote_image = f'url("{quote_uri}")' if quote_uri else 'none'
            quote_border = css_color(theme_value("material.web.quote_border", "rgba(255,255,255,46)"))
            web_radius = int(theme_float("material.web.radius", 12.0)) if OBSIDIAN_MATERIAL.enabled(ui_theme) else int(theme_float("radius.lg", 13.0))
            web_padding = int(theme_float("material.web.padding", 12.0)) if OBSIDIAN_MATERIAL.enabled(ui_theme) else 10
            web_inner_radius = max(4, web_radius - 2)
            quote_radius = max(4, web_radius - 3)

            tg_border = css_color(theme_value(
                "material.web.telegram_content.border_base",
                theme_value("material.web.border_base", theme_value("material.edge.base", "rgba(255,255,255,22)")),
            ))
            tg_border_width = theme_float(
                "material.web.telegram_content.border_width",
                theme_float("material.web.border_width", theme_float("material.edge.base_width", 1.0)),
            )
            tg_specular = css_color(theme_value(
                "material.web.telegram_content.specular",
                theme_value("material.web.specular", theme_value("material.edge.specular", "rgba(255,255,255,92)")),
            ))
            tg_specular_width = theme_float(
                "material.web.telegram_content.specular_width",
                theme_float("material.web.specular_width", theme_float("material.edge.specular_width", 0.95)),
            )
            tg_occlusion = css_color(theme_value(
                "material.web.telegram_content.occlusion",
                theme_value("material.web.occlusion", theme_value("material.edge.occlusion", "rgba(0,0,0,190)")),
            ))
            tg_occlusion_width = theme_float(
                "material.web.telegram_content.occlusion_width",
                theme_float("material.web.occlusion_width", theme_float("material.edge.occlusion_width", 1.15)),
            )
            tg_inner_rim = css_color(theme_value(
                "material.web.telegram_content.inner_rim",
                theme_value("effects.window_inner_rim", "rgba(255,255,255,28)"),
            ))
            tg_specular_soft = css_color(theme_value(
                "material.web.telegram_content.specular_soft",
                "rgba(255,255,255,40)",
            ))
            tg_specular_low = css_color(theme_value(
                "material.web.telegram_content.specular_low",
                "rgba(255,255,255,20)",
            ))

            css = (
                f"html,body{{background-color:{bg.name()}!important;background-image:none!important;color:{fg.name()}!important;margin:0!important;padding:0!important;width:100%!important;min-width:100%!important;max-width:100%!important;box-sizing:border-box!important;}}"
                f"body{{display:block!important;width:100vw!important;min-width:100vw!important;max-width:100vw!important;}}"
                f".tgme_widget_message_text{{display:block!important;width:100vw!important;min-width:100vw!important;max-width:100vw!important;min-height:100%!important;flex:none!important;align-self:stretch!important;position:relative!important;isolation:isolate!important;background-color:{bg.name()}!important;background-image:{bg_image}!important;background-size:cover!important;background-position:center!important;color:{fg.name()}!important;margin:0!important;padding:{web_padding}px!important;border:0!important;border-radius:{web_inner_radius}px!important;box-sizing:border-box!important;background-clip:padding-box!important;box-shadow:inset 1px 1px 0 rgba(255,255,255,0.035),inset -1px -1px 0 rgba(0,0,0,0.22)!important;overflow:hidden!important;}}"
                f".tgme_widget_message_text,.tgme_widget_message_text *{{color:{fg.name()}!important;}}"
                ".tgme_widget_message_text *{background-color:transparent!important;}"
                f".tgme_widget_message_text blockquote,.tgme_widget_message_text [class*=quote],.tgme_widget_message_text pre,.tgme_widget_message_text code{{background-color:{quote.name()}!important;background-image:{quote_image}!important;background-size:cover!important;background-position:center!important;color:{fg.name()}!important;border:1px solid {quote_border}!important;border-radius:{quote_radius}px!important;box-sizing:border-box!important;box-shadow:inset 1px 1px 0 rgba(255,255,255,0.12),inset -1px -1px 0 rgba(0,0,0,0.48)!important;}}"
                f".tgme_widget_message_text blockquote *,.tgme_widget_message_text [class*=quote] *,.tgme_widget_message_text pre *,.tgme_widget_message_text code *{{color:{fg.name()}!important;background-color:transparent!important;}}"
                f"a{{color:{link.name()}!important;}}"
            )
            js = (
                "(() => {"
                "let st=document.getElementById('q3elite-theme-override');"
                "if(!st){st=document.createElement('style');st.id='q3elite-theme-override';document.head.appendChild(st);}"
                f"st.textContent={json.dumps(css)};"
                "return true;})();"
            )
            self.page().runJavaScript(js)
        except RuntimeError:
            pass

    def _apply_loaded_height(self, height):
        """Reveal Chromium only after Telegram's styled message DOM is ready."""
        try:
            resolved = int(height or 0)
        except (TypeError, ValueError):
            resolved = 0
        if resolved <= 0:
            # Telegram frequently completes its widget/custom-emoji DOM shortly
            # after loadFinished. Retry the DOM pass before changing endpoints.
            self._dom_retry += 1
            if self._dom_retry <= 4:
                QtCore.QTimer.singleShot(280, lambda: self._telegram_loaded(True))
            else:
                self._retry_endpoint_or_fail("Telegram post DOM was empty")
            return

        self._dom_retry = 0
        self._apply_height(resolved)
        try:
            self.show()
            if getattr(self, "_q3_detached_renderer", False):
                self.move(-30000, -30000)
            else:
                self.raise_()
            host = self.parentWidget()
            if host is not None:
                host.update()
                host.updateGeometry()
            if not self._ready_emitted:
                self._ready_emitted = True
                self.ready.emit()
            QtCore.QTimer.singleShot(0, self._remeasure_height)
            QtCore.QTimer.singleShot(120, self._remeasure_height)
        except RuntimeError:
            pass

    def _remeasure_height(self):
        if QWebEngineView is None or not self.isVisible():
            return
        self.page().runJavaScript(
            """(() => {
                const text = document.querySelector('.tgme_widget_message_text');
                return text ? Math.ceil(text.getBoundingClientRect().height) : 0;
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



class TelegramSnapshotView(QtWidgets.QLabel):
    """Qt-only presentation of a detached Telegram Chromium renderer.

    Chromium never becomes a child of the translucent launcher HWND.  A
    top-level off-screen TelegramTextView owns Telegram's real DOM/custom emoji
    animation; this label periodically grabs that renderer into a normal QPixmap.
    The launcher therefore never gains/removes a Chromium child surface when the
    Changelog page is selected, avoiding the Windows DWM full-window flash.
    """
    mediaDetected = QtCore.pyqtSignal(object)
    ready = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)

    def __init__(self, url, renderer, parent=None):
        super().__init__(parent)
        self._source_url = str(url or "").strip()
        self._renderer = renderer
        self._ready_emitted = False
        self._last_width = 0
        self._frame_pixmap = QtGui.QPixmap()
        self._loading_text = "Loading Telegram post…"
        self.setObjectName("telegramSnapshotView")
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        # Do not let QLabel derive horizontal geometry from the captured pixmap.
        # The card/layout is authoritative; Chromium is resized to match it.
        self.setMinimumWidth(0)
        self.setMinimumHeight(40)
        self.setMaximumHeight(900)
        self.setText("")
        self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        self._frame_timer = QtCore.QTimer(self)
        self._frame_timer.setTimerType(QtCore.Qt.TimerType.CoarseTimer)
        self._frame_timer.setInterval(max(90, theme_int("performance.telegram_snapshot_interval_ms", 120)))
        self._frame_timer.timeout.connect(self._capture_frame)

        if self._renderer is None:
            QtCore.QTimer.singleShot(0, lambda: self.failed.emit("Telegram renderer unavailable"))
            return

        try:
            self._renderer.mediaDetected.connect(self.mediaDetected.emit)
            self._renderer.ready.connect(self._renderer_ready)
            self._renderer.failed.connect(self.failed.emit)
        except RuntimeError:
            QtCore.QTimer.singleShot(0, lambda: self.failed.emit("Telegram renderer unavailable"))
            return

        if getattr(self._renderer, "_ready_emitted", False):
            QtCore.QTimer.singleShot(0, self._renderer_ready)
        else:
            # Keep the detached renderer active until its first styled DOM frame
            # has been produced, even though this Changelog page may still be hidden.
            self._set_renderer_lifecycle(active=True)

    def _set_renderer_lifecycle(self, active):
        renderer = self._renderer
        if renderer is None or QWebEnginePage is None:
            return
        try:
            state = (
                QWebEnginePage.LifecycleState.Active
                if active else QWebEnginePage.LifecycleState.Frozen
            )
            renderer.page().setLifecycleState(state)
        except Exception:
            pass

    def _renderer_ready(self):
        if self._renderer is None:
            return
        self._sync_renderer_width(force=True)
        self._capture_frame()
        self._loading_text = ""
        self.setFixedHeight(max(40, min(int(self._renderer.height()), 900)))
        # Run once more after the card's layouts have consumed the new height.
        # This catches the final available width instead of retaining the small
        # off-screen/prewarm width as the visible snapshot width.
        QtCore.QTimer.singleShot(0, lambda: self._sync_renderer_width(force=True))
        QtCore.QTimer.singleShot(90, lambda: self._sync_renderer_width(force=True))
        if self.isVisible():
            self._set_renderer_lifecycle(active=True)
            if not self._frame_timer.isActive():
                self._frame_timer.start()
        else:
            # One frame is enough while the Changelog is hidden. Preserve the
            # loaded DOM but stop animation/CPU work until the page is exposed.
            self._set_renderer_lifecycle(active=False)
        if not self._ready_emitted:
            self._ready_emitted = True
            self.ready.emit()

    def _target_renderer_width(self):
        """Return the real card content width, independent of snapshot sizeHint."""
        candidates = [int(self.width() or 0)]
        host = self.parentWidget()
        if host is not None:
            host_width = int(host.contentsRect().width() or host.width() or 0)
            layout = host.layout()
            if layout is not None:
                margins = layout.contentsMargins()
                host_width -= margins.left() + margins.right()
            candidates.append(host_width)
        return max(320, max(candidates or [0]))

    def sizeHint(self):
        # Never let a stale capture dictate the horizontal layout.
        return QtCore.QSize(0, max(40, int(self.height() or 40)))

    def minimumSizeHint(self):
        return QtCore.QSize(0, 40)

    def _sync_renderer_width(self, force=False):
        renderer = self._renderer
        if renderer is None:
            return
        width = self._target_renderer_width()
        if not force and abs(width - self._last_width) < 3:
            return
        self._last_width = width
        try:
            renderer.setFixedWidth(width)
            renderer.move(-30000, -30000)
            # Width changes alter Telegram line wrapping. Re-measure after
            # Chromium has laid out the retained DOM at the new width.
            QtCore.QTimer.singleShot(35, renderer._remeasure_height)
            QtCore.QTimer.singleShot(110, self._capture_frame)
            QtCore.QTimer.singleShot(240, self._capture_frame)
        except RuntimeError:
            pass

    def _capture_frame(self):
        renderer = self._renderer
        if renderer is None or not getattr(renderer, "_ready_emitted", False):
            return
        try:
            # Make the detached Chromium viewport follow the final Qt card width
            # before grabbing it. This prevents a narrow prewarm frame from being
            # painted into a later, wider Changelog card.
            target_width = self._target_renderer_width()
            if abs(int(renderer.width()) - target_width) >= 3:
                self._sync_renderer_width(force=True)
                return
            pm = renderer.grab()
            if pm.isNull():
                return
            self._frame_pixmap = pm
            resolved_h = max(40, min(int(renderer.height()), 900))
            if self.height() != resolved_h:
                self.setFixedHeight(resolved_h)
                self.updateGeometry()
            self.update()
        except RuntimeError:
            pass

    def paintEvent(self, event):
        # QLabel's stock pixmap painter does not clip to the rounded material
        # container. Paint the snapshot ourselves through an antialiased rounded
        # path so Chromium's square black corners can never escape the card.
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect())
        outer_radius = theme_float("material.web.radius", theme_float("radius.md", 10.0))
        radius = max(0.0, theme_float("material.web.telegram_content.radius", max(0.0, outer_radius - 2.0)))
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)

        if not self._frame_pixmap.isNull():
            pm = self._frame_pixmap
            # Normally widths are identical. If Qt resized the card between two
            # 120 ms snapshots, fill the transient frame to the current width
            # rather than exposing a square/empty strip at the right edge.
            if pm.width() != self.width() and pm.width() > 0:
                source = QtCore.QRectF(0, 0, pm.width(), pm.height())
                target = QtCore.QRectF(0, 0, self.width(), pm.height())
                painter.drawPixmap(target, pm, source)
            else:
                painter.drawPixmap(0, 0, pm)
        else:
            bg = theme_color("changelog.web_bg", "#050505")
            bg.setAlpha(255)
            painter.fillPath(path, bg)
            if self._loading_text:
                painter.setPen(theme_color("changelog.web_text", "#E3E3E3"))
                painter.drawText(
                    self.rect().adjusted(12, 8, -12, -8),
                    int(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter),
                    self._loading_text,
                )
        painter.end()

    def showEvent(self, event):
        super().showEvent(event)
        if self._renderer is None:
            return
        self._set_renderer_lifecycle(active=True)
        self._sync_renderer_width(force=True)
        self._capture_frame()
        if getattr(self._renderer, "_ready_emitted", False) and not self._frame_timer.isActive():
            self._frame_timer.start()

    def hideEvent(self, event):
        self._frame_timer.stop()
        self._set_renderer_lifecycle(active=False)
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_renderer_width(force=False)

    def reload_post(self):
        if self._renderer is None:
            return
        self._frame_timer.stop()
        self._set_renderer_lifecycle(active=True)
        try:
            self._renderer.reload_post()
        except RuntimeError:
            pass

    def emit_media_now(self):
        if self._renderer is None:
            return
        try:
            self._renderer.emit_media_now()
        except RuntimeError:
            pass

    def refresh_after_expand(self):
        if self._renderer is None:
            return
        self._set_renderer_lifecycle(active=True)
        self._sync_renderer_width(force=True)
        try:
            self._renderer.refresh_after_expand()
        except RuntimeError:
            pass
        QtCore.QTimer.singleShot(60, self._capture_frame)
        QtCore.QTimer.singleShot(220, self._capture_frame)

    def apply_theme_palette(self):
        if self._renderer is None:
            return
        try:
            self._renderer.apply_theme_palette()
            QtCore.QTimer.singleShot(80, self._capture_frame)
        except RuntimeError:
            pass



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
        cache = CACHE_DIR / "Changelog"
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

        self.prevButton = MaterialPushButton("", self.stage)
        self.prevButton.setObjectName("changelogArrow")
        self.prevButton.setFixedSize(38, 58)
        self.prevButton.move(12, (405 - 58) // 2)
        if qta is not None:
            self.prevButton.setIcon(qta.icon("fa5s.chevron-left", color=theme_value("icons.action_icon_color", theme_value("colors.text.secondary", "#C3C5C8"))))
        else:
            self.prevButton.setText("‹")
        self.prevButton.clicked.connect(lambda: self.change(-1))

        self.nextButton = MaterialPushButton("", self.stage)
        self.nextButton.setObjectName("changelogArrow")
        self.nextButton.setFixedSize(38, 58)
        self.nextButton.move(720 - 50, (405 - 58) // 2)
        if qta is not None:
            self.nextButton.setIcon(qta.icon("fa5s.chevron-right", color=theme_value("icons.action_icon_color", theme_value("colors.text.secondary", "#C3C5C8"))))
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




class PlayOverlayButton(MaterialPushButton):
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


class ChangelogCard(SurfaceCard):
    def __init__(self, release, parent=None, expanded=True, defer_telegram=False, telegram_fetcher=None, auto_fetch_telegram=False, telegram_web_only=False, telegram_page_provider=None):
        super().__init__(parent)
        self._telegram_fetcher = telegram_fetcher
        self._telegram_page_provider = telegram_page_provider
        self._telegram_web_only = bool(telegram_web_only)
        self._telegram_auto_fetch = bool(auto_fetch_telegram) and not self._telegram_web_only
        self._telegram_polling = False
        self.setObjectName("changelogCard")
        release = dict(release)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(9)

        source_url = str(release.get("telegram", release.get("text_source", ""))).strip()
        self.telegram_url = source_url
        self.telegram_video_reupload = ""
        # Deferred Changelog cards are GUI-only consumers. Network work is
        # performed by the low-priority preload worker; never block Qt here.
        if source_url and not self._telegram_web_only:
            tg = (_telegram_cached_post(source_url, allow_stale=True) or {}) if defer_telegram else _telegram_public_post_data(source_url)
        else:
            # Web-only cards deliberately do not parse/fetch Telegram natively.
            # Their one retained WebEngine page is the authoritative content.
            tg = {}

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
            f'<span style="color:{_html_escape(str(theme_value("changelog.version_color", theme_value("colors.text.primary", "#F3F3F3"))))};">'
            f'v{_html_escape(release.get("version", "?"))}</span>'
        )
        header.addWidget(title)
        header.addStretch(1)

        date = str(release.get("date", release.get("release_date", "")))
        if date:
            date_label = QtWidgets.QLabel(date)
            date_label.setObjectName("changelogDate")
            header.addWidget(date_label)

        self.toggleButton = MaterialPushButton("")
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
        self.toggleButton.installEventFilter(self)
        self.bodyWidget.setVisible(self._expanded)
        self._toggle_hovered = False
        self._update_toggle_icon()

        # Telegram text is visually rendered by Chromium, but Chromium is never
        # inserted into this translucent launcher window. Changelog cards receive
        # Qt pixmap snapshots from detached renderers instead.
        explicit_text = str(release.get("text", "") or "")
        use_telegram_text = bool(source_url and not explicit_text and TELEGRAM_WEBENGINE_AVAILABLE)
        self._native_uses_telegram = bool(source_url and not explicit_text and not self._telegram_web_only)
        self._native_explicit_text = explicit_text

        self.telegramView = None
        self.telegramBackdrop = None
        self._telegram_layout = None
        self._telegram_source_url = source_url if use_telegram_text else ""
        self._telegram_activation_scheduled = False
        self._telegram_live_button = None

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
        if not native_body and source_url:
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

        # Keep the release/change portion so Telegram text can be filled from the
        # background preload cache without rebuilding the whole card.
        self._native_tail_html = "".join(native_parts[1:] if native_body else native_parts)
        self.nativeTextView = None
        if native_parts or self._native_uses_telegram or self._telegram_web_only:
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
            initial_html = "".join(native_parts)
            if not initial_html and (self._native_uses_telegram or self._telegram_web_only):
                initial_html = '<div class="body">Loading Telegram post…</div>'
            native.setHtml(initial_html)
            native.document().documentLayout().documentSizeChanged.connect(
                lambda size, w=native: w.setFixedHeight(max(28, int(size.height()) + 8))
            )
            native.setFixedHeight(max(28, int(native.document().size().height()) + 8))
            self.nativeTextView = native
            lay.addWidget(native)
            if (self._native_uses_telegram
                    and not str(tg.get("text", "") or "").strip()
                    and (self._telegram_auto_fetch or self._expanded)):
                self._telegram_polling = True
                QtCore.QTimer.singleShot(250, lambda: self._poll_telegram_cache(0))

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
                # Same rule as text: Changelog construction must never touch
                # the network when defer_telegram=True.
                media_data = ((_telegram_cached_post(value) or {}) if defer_telegram
                              else _telegram_public_post_data(value))
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
                watch.setIcon(qta.icon("fa5s.play", color=theme_value("icons.action_icon_color", theme_value("colors.text.secondary", "#C3C5C8"))))
            watch.clicked.connect(
                lambda checked=False, u=video:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(u))
            )
            buttons.addWidget(watch)

        # Embedded Chromium inside a translucent frameless HWND forces a
        # top-level compositor rebuild on Windows (the visible "restart" flash).
        # Native Telegram content is the production path; WebEngine remains an
        # explicit compatibility/debug option only.
        if (source_url and TELEGRAM_WEBENGINE_AVAILABLE
                and theme_bool("performance.changelog_live_webengine", False)):
            live_post = GlowButton("LIVE POST")
            live_post.setObjectName("changelogLinkButton")
            if qta is not None:
                live_post.setIcon(qta.icon("fa5s.bolt", color=theme_value("icons.action_icon_color", theme_value("colors.text.secondary", "#C3C5C8"))))
            live_post.setToolTip("Load Telegram's live Chromium renderer for this post")
            live_post.clicked.connect(self._telegram_live_action)
            self._telegram_live_button = live_post
            buttons.addWidget(live_post)

        if source_link:
            source = GlowButton("OPEN SOURCE")
            source.setObjectName("changelogLinkButton")
            if qta is not None:
                source.setIcon(qta.icon("fa5s.external-link-alt", color=theme_value("icons.action_icon_color", theme_value("colors.text.secondary", "#C3C5C8"))))
            source.clicked.connect(
                lambda checked=False, u=source_link:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(u))
            )
            buttons.addWidget(source)

        buttons.addStretch(1)
        # Native actions live outside the colored Telegram/Q3News body.
        self.layout().addLayout(buttons)


    def _set_native_telegram_text(self, text):
        if not self._native_uses_telegram or self.nativeTextView is None:
            return
        text = str(text or "").strip()
        if not text:
            return
        body = '<div class="body">' + _html_escape(text).replace("\n", "<br>") + '</div>'
        self.nativeTextView.setHtml(body + str(getattr(self, "_native_tail_html", "") or ""))
        self.nativeTextView.setFixedHeight(
            max(28, int(self.nativeTextView.document().size().height()) + 8)
        )
        self.nativeTextView.updateGeometry()

    def _poll_telegram_cache(self, attempt=0):
        """Consume worker-populated Telegram cache without blocking the GUI."""
        if not self._native_uses_telegram or not self.telegram_url:
            return
        try:
            data = _telegram_cached_post(self.telegram_url)
            if isinstance(data, dict) and data:
                self._set_native_telegram_text(data.get("text", ""))
                normalized = {
                    "images": list(data.get("images", []) or []),
                    "video": "__telegram_post__" if data.get("has_video") else str(data.get("video", "") or ""),
                    "poster": str(data.get("video_poster", data.get("poster", "")) or ""),
                    "reupload": str(data.get("video_reupload", data.get("reupload", "")) or ""),
                }
                self._apply_telegram_media(normalized)
                self._telegram_polling = False
                return
            # If startup preload missed/failed, let the card request another
            # background attempt.  This callback never performs network I/O on
            # the Qt GUI thread.
            fetcher = getattr(self, "_telegram_fetcher", None)
            if callable(fetcher) and int(attempt) in (0, 8, 20):
                fetcher(self.telegram_url)
        except RuntimeError:
            return
        except Exception:
            pass
        if int(attempt) < 40:
            QtCore.QTimer.singleShot(400, lambda a=int(attempt)+1: self._poll_telegram_cache(a))
        else:
            self._telegram_polling = False

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

    def _telegram_live_action(self, checked=False):
        if self.telegramView is None:
            self.activate_telegram_renderer()
            return
        try:
            self.telegramView.reload_post()
        except RuntimeError:
            pass

    def _ensure_telegram_backdrop(self):
        if self.telegramBackdrop is not None and self._telegram_layout is not None:
            return True
        if not self._telegram_source_url:
            return False
        try:
            backdrop = SurfaceCard(self.bodyWidget)
            backdrop.setObjectName("changelogWebBackdrop")
            backdrop.setProperty("materialRadius", theme_float("material.web.radius", 14.0))
            backdrop.setProperty("materialTextureKey", "material.web.texture_path")
            backdrop.setProperty("materialTextureOpacity", theme_float("material.web.texture_opacity", 0.18))
            backdrop.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
            backdrop.setAutoFillBackground(False)
            web_l = QtWidgets.QVBoxLayout(backdrop)
            web_margin = max(2, theme_int("material.web.outer_margin", 0)) if OBSIDIAN_MATERIAL.enabled(ui_theme) else 0
            web_l.setContentsMargins(web_margin, web_margin, web_margin, web_margin)
            web_l.setSpacing(0)
            backdrop.setMinimumHeight(40)
            body_layout = self.bodyWidget.layout()
            if body_layout is None:
                return False
            body_layout.insertWidget(0, backdrop)
            self.telegramBackdrop = backdrop
            self._telegram_layout = web_l
            return True
        except Exception as error:
            print(f"[changelog] Telegram backdrop activation failed: {error}")
            return False

    def activate_telegram_renderer(self):
        """Attach a Qt snapshot fed by a detached Telegram Chromium renderer.

        No QWebEngineView is parented into the launcher. This is deliberate:
        adding Chromium to the translucent frameless HWND makes Windows/DWM
        recompose the whole launcher and creates the visible 0.2 s "restart".
        """
        if self.telegramView is not None:
            return self.telegramView
        if not self._telegram_source_url or QWebEngineView is None:
            return None
        if not self._ensure_telegram_backdrop():
            return None
        try:
            renderer = None
            provider = getattr(self, "_telegram_page_provider", None)
            if callable(provider):
                try:
                    warm = provider(self._telegram_source_url)
                    if isinstance(warm, tuple):
                        renderer = warm[0]
                    else:
                        renderer = warm
                except Exception as error:
                    print(f"[changelog] Telegram detached-renderer handoff failed: {error}")
            if renderer is None:
                print("[changelog] Telegram detached renderer unavailable")
                return None

            view = TelegramSnapshotView(
                self._telegram_source_url,
                renderer,
                self.telegramBackdrop,
            )
            view.mediaDetected.connect(self._apply_telegram_media)
            view.ready.connect(self._telegram_web_ready)
            view.failed.connect(self._telegram_web_failed)
            self._telegram_layout.addWidget(view)
            self.telegramView = view
            self.telegramBackdrop.setMinimumHeight(0)
            self.telegramBackdrop.updateGeometry()

            if self._telegram_live_button is not None:
                self._telegram_live_button.setText("RELOAD")
                if qta is not None:
                    try:
                        self._telegram_live_button.setIcon(qta.icon(
                            "fa5s.sync-alt",
                            color=theme_value(
                                "icons.action_icon_color",
                                theme_value("colors.text.secondary", "#C3C5C8"),
                            ),
                        ))
                    except Exception:
                        pass
            self.bodyWidget.updateGeometry()
            if self._expanded:
                QtCore.QTimer.singleShot(40, view.refresh_after_expand)
            return view
        except Exception as error:
            print(f"[changelog] Telegram detached renderer activation failed: {error}")
            if self.nativeTextView is not None:
                self.nativeTextView.show()
            return None

    def _telegram_web_ready(self):
        if self.nativeTextView is not None:
            self.nativeTextView.hide()
        if self.telegramBackdrop is not None:
            self.telegramBackdrop.show()
            self.telegramBackdrop.updateGeometry()
        self.bodyWidget.updateGeometry()

    def _telegram_web_failed(self, reason):
        # Preserve the native fallback and give the old low-priority parser one
        # last chance, but never block the UI waiting for it.
        if self.nativeTextView is not None:
            self.nativeTextView.show()
        fetcher = getattr(self, "_telegram_fetcher", None)
        if callable(fetcher) and self.telegram_url:
            fetcher(self.telegram_url)
        print(f"[changelog] Telegram WebEngine fallback: {reason}")

    def eventFilter(self, obj, event):
        if obj is self.toggleButton:
            if event.type() == QtCore.QEvent.Type.Enter:
                self._toggle_hovered = True
                self._update_toggle_icon()
            elif event.type() == QtCore.QEvent.Type.Leave:
                self._toggle_hovered = False
                self._update_toggle_icon()

        if hasattr(self, "serversScroll") and event.type() == QtCore.QEvent.Type.Wheel:
            # Fallback for child widgets such as the levelshot QLabel.
            widget = obj if isinstance(obj, QtWidgets.QWidget) else None
            while widget is not None:
                if widget in getattr(self, "serverCards", []):
                    delta = event.angleDelta().y() or event.angleDelta().x()
                    if delta and len(self.serverCards) > max(1, int(getattr(self, "_serverVisibleCount", 3))):
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
            token = "changelog.toggle_icon_hover" if getattr(self, "_toggle_hovered", False) else "changelog.toggle_icon"
            color = theme_value(token, theme_value("icons.action_icon_color", theme_value("colors.text.secondary", "#C3C5C8")))
            self.toggleButton.setIcon(qta.icon(name, color=color))
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
            if self._native_uses_telegram and self.telegram_url:
                data = _telegram_cached_post(self.telegram_url)
                if not data:
                    fetcher = getattr(self, "_telegram_fetcher", None)
                    if callable(fetcher):
                        fetcher(self.telegram_url)
                    if not getattr(self, "_telegram_polling", False):
                        self._telegram_polling = True
                        QtCore.QTimer.singleShot(120, lambda: self._poll_telegram_cache(0))

            # Native content expands without constructing Chromium. If a live
            # Telegram renderer was explicitly enabled earlier, only re-measure it.
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


def _q3_plain_ascii(text):
    """Strip Quake ^ color escapes and hide non-ASCII/custom glyphs."""
    text = re.sub(r"\^[0-9A-Za-z]", "", str(text or ""))
    return "".join(ch for ch in text if 32 <= ord(ch) <= 126).strip()


def _q3_segments(text):
    """Return (plain ASCII text, color) segments for common Quake ^0..^7 colors."""
    colors = {
        "0": "#777777", "1": "#ff4040", "2": "#55ff55", "3": "#ffff55",
        "4": "#6699ff", "5": "#55ffff", "6": "#ff66ff", "7": "#eeeeee",
    }
    s = str(text or "")
    segments, buf, color = [], "", "#eeeeee"
    i = 0
    while i < len(s):
        if i + 1 < len(s) and s[i] == "^":
            code = s[i + 1]
            if code in colors:
                if buf:
                    segments.append((buf, color))
                    buf = ""
                color = colors[code]
                i += 2
                continue
            # Hide unsupported/custom Quake escape as well.
            if code.isalnum():
                i += 2
                continue
        ch = s[i]
        if 32 <= ord(ch) <= 126:
            buf += ch
        i += 1
    if buf:
        segments.append((buf, color))
    return segments


class Q3NameDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.hover_row = -1
        view.setMouseTracking(True)
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        # A theme switch can rebuild ServerCards while queued mouse events still
        # target the old viewport. Never dereference a deleted Qt C++ object.
        try:
            viewport = self.view.viewport()
        except RuntimeError:
            return False
        if obj is viewport:
            if event.type() == QtCore.QEvent.Type.MouseMove:
                try:
                    idx = self.view.indexAt(event.position().toPoint())
                except RuntimeError:
                    return False
                row = idx.row() if idx.isValid() else -1
                if row != self.hover_row:
                    self.hover_row = row
                    viewport.update()
            elif event.type() == QtCore.QEvent.Type.Leave:
                if self.hover_row != -1:
                    self.hover_row = -1
                    viewport.update()
        return super().eventFilter(obj, event)

    def paint(self, painter, option, index):
        if index.column() == 0 and index.row() == self.hover_row:
            painter.save()
            painter.fillRect(
                QtCore.QRect(0, option.rect.top(), self.view.viewport().width(), option.rect.height()),
                theme_color("effects.row_hover", "rgba(255,255,255,10)"),
            )
            painter.restore()
        if index.column() != 0:
            return super().paint(painter, option, index)
        painter.save()
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        raw = index.data(QtCore.Qt.ItemDataRole.UserRole)
        if raw is None:
            raw = index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
        opt.text = ""
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        x = option.rect.left() + 9
        baseline = option.rect.center().y() + painter.fontMetrics().ascent() // 2 - 1
        raw_text = str(raw or "")
        if index.data(QtCore.Qt.ItemDataRole.UserRole) is not None:
            painter.setPen(theme_color("colors.text.quiet", "#64727D"))
            painter.drawText(x, baseline, "•")
            x += painter.fontMetrics().horizontalAdvance("•") + 7
        for text_part, color in _q3_segments(raw_text):
            if str(color).casefold() == "#eeeeee":
                pen = theme_color("servers.player_default", theme_value("colors.text.primary", "#EEEEEE"))
            elif str(color).casefold() == "#777777":
                pen = theme_color("servers.player_neutral", theme_value("colors.text.muted", "#777777"))
            else:
                pen = QtGui.QColor(color)
            painter.setPen(pen)
            painter.drawText(x, baseline, text_part)
            x += painter.fontMetrics().horizontalAdvance(text_part)
        painter.restore()


_PIXMAP_FILE_CACHE = _PixmapLRUCache("performance.pixmap_cache_mb", 192)

def _load_pixmap_file(path):
    """Load common image files without asking Qt to decode legacy TGA files.

    Decoded pixmaps are retained by path/mtime/size. The launcher revisits the
    same levelshots and screenshot previews far more often than those files
    change, so repeated image decoding is pure navigation overhead.

    Some Quake levelshots are old/non-TrueVision-2.0 TGA variants. Qt's TGA
    plugin prints a warning for every attempted read. Pillow handles more of
    these files; when available, V15 converts them once to PNG under AppData.
    If Pillow is absent or the TGA is broken, return an empty pixmap silently.
    """
    if path is None:
        return QtGui.QPixmap()
    path = Path(path)
    if not path.is_file():
        return QtGui.QPixmap()
    try:
        stat = path.stat()
        cache_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return QtGui.QPixmap()
    cached_pm = _PIXMAP_FILE_CACHE.get(cache_key)
    if cached_pm is not None and not cached_pm.isNull():
        return QtGui.QPixmap(cached_pm)
    if path.suffix.casefold() != ".tga":
        pm = QtGui.QPixmap(str(path))
        if not pm.isNull():
            _PIXMAP_FILE_CACHE[cache_key] = QtGui.QPixmap(pm)
        return pm
    if PILImage is None:
        return QtGui.QPixmap()
    try:
        digest = hashlib.blake2b(
            f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8", "ignore"),
            digest_size=8,
        ).hexdigest()
        TGA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached = TGA_CACHE_DIR / f"{path.stem}_{digest}.png"
        if not cached.is_file():
            with PILImage.open(path) as image:
                image.convert("RGBA").save(cached, format="PNG", optimize=True)
        pm = QtGui.QPixmap(str(cached))
        if not pm.isNull():
            _PIXMAP_FILE_CACHE[cache_key] = QtGui.QPixmap(pm)
        return pm
    except Exception:
        return QtGui.QPixmap()


def _resolve_levelshot_path(mapname, primary_dir=None):
    wanted = str(mapname or "").strip().casefold()
    supported = {".png", ".jpg", ".jpeg", ".webp", ".tga"}
    dirs = []
    if primary_dir is not None:
        dirs.append(Path(primary_dir))
    for d in (MAP_LEVELSHOTS_DIR, MAP_DISCOVERED_LEVELSHOTS_DIR):
        if d not in dirs:
            dirs.append(d)
    priority, normal = [], []
    for directory in dirs:
        if not directory.is_dir() or not wanted:
            continue
        try:
            for path in directory.iterdir():
                if not path.is_file() or path.suffix.casefold() not in supported:
                    continue
                stem = path.stem
                important = stem.startswith("!")
                if important:
                    stem = stem[1:]
                aliases = [part.strip().casefold() for part in stem.split("&") if part.strip()]
                if wanted in aliases:
                    (priority if important else normal).append(path)
        except OSError:
            pass
    priority.sort(key=lambda p: p.name.casefold())
    normal.sort(key=lambda p: p.name.casefold())
    fallbacks = [
        MAP_LEVELSHOTS_DIR / "unknownmap.png",
        MAP_UNKNOWN_LEVELSHOT,
        ASSETS_DIR / "servers" / "levelshots" / "unknownmap.png",
        ASSETS_DIR / "servers" / "levelshots" / "unknown.png",
    ]
    return next((path for path in priority + normal + fallbacks if path.is_file()), None)


def _blur_pixmap(pixmap, radius=18.0, sample=QtCore.QSize(720, 405)):
    if pixmap is None or pixmap.isNull():
        return QtGui.QPixmap()
    small = pixmap.scaled(sample, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          QtCore.Qt.TransformationMode.SmoothTransformation)
    scene = QtWidgets.QGraphicsScene()
    item = QtWidgets.QGraphicsPixmapItem(small)
    effect = QtWidgets.QGraphicsBlurEffect()
    effect.setBlurRadius(float(radius))
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    out = QtGui.QPixmap(small.size())
    out.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(out)
    scene.render(painter, QtCore.QRectF(out.rect()), QtCore.QRectF(small.rect()))
    painter.end()
    return out


class ElidedTextLabel(QtWidgets.QLabel):
    """One-line middle-elided label whose sizeHint never expands a side panel."""
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._sync_elide()

    def _sync_elide(self):
        if not hasattr(self, "_full_text"):
            return
        width = max(20, self.contentsRect().width())
        shown = self.fontMetrics().elidedText(self._full_text, QtCore.Qt.TextElideMode.ElideMiddle, width)
        QtWidgets.QLabel.setText(self, shown)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_elide()


def _rounded_pixmap(pixmap, radius=10.0):
    """Return an antialiased rounded copy of a pixmap."""
    if pixmap is None or pixmap.isNull():
        return QtGui.QPixmap()
    out = QtGui.QPixmap(pixmap.size())
    out.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(out)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    path = QtGui.QPainterPath()
    r = max(0.0, float(radius))
    path.addRoundedRect(QtCore.QRectF(out.rect()).adjusted(0.25, 0.25, -0.25, -0.25), r, r)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return out


class AspectPixmapLabel(QtWidgets.QLabel):
    """Stable 16:9-style preview label with a real rounded pixel mask."""
    def __init__(self, aspect_w=16, aspect_h=9, parent=None):
        super().__init__(parent)
        self.aspect_w = max(1, int(aspect_w))
        self.aspect_h = max(1, int(aspect_h))
        self._source = QtGui.QPixmap()
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def setSourcePixmap(self, pixmap):
        self._source = QtGui.QPixmap(pixmap) if pixmap is not None else QtGui.QPixmap()
        self._sync_pixmap()

    def _sync_height(self):
        if self.width() > 8:
            self.setFixedHeight(max(90, round(self.width() * self.aspect_h / self.aspect_w)))

    def _sync_pixmap(self):
        self._sync_height()
        if self._source.isNull() or self.width() < 8 or self.height() < 8:
            QtWidgets.QLabel.setPixmap(self, QtGui.QPixmap())
            return
        scaled = self._source.scaled(self.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                     QtCore.Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - self.width()) // 2)
        y = max(0, (scaled.height() - self.height()) // 2)
        cropped = scaled.copy(x, y, self.width(), self.height())
        QtWidgets.QLabel.setPixmap(self, _rounded_pixmap(cropped, theme_float("media.preview_radius", 11.0)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_pixmap()


class ServerPlayerTable(QtWidgets.QTreeWidget):
    wheelRequested = QtCore.pyqtSignal(int)

    def wheelEvent(self, event):
        # Wheel inside PLAYER section scrolls the player list. The surrounding
        # card keeps carousel scrolling everywhere else.
        super().wheelEvent(event)


class ServerCard(QtWidgets.QFrame):
    connectRequested = QtCore.pyqtSignal(str)
    copyRequested = QtCore.pyqtSignal(str)
    removeRequested = QtCore.pyqtSignal(str)
    moveRequested = QtCore.pyqtSignal(str, int)
    aliasRequested = QtCore.pyqtSignal(str)
    wheelRequested = QtCore.pyqtSignal(int)

    def __init__(self, server, levelshots_dir, parent=None):
        super().__init__(parent)
        self.server = dict(server)
        self.levelshots_dir = Path(levelshots_dir)
        self.setObjectName("serverCard")
        self.setFixedWidth(theme_int("servers.card_min_width", 330))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.presentation = str(theme_value("servers.presentation", "cinematic") or "cinematic").casefold()
        self._bg_source = QtGui.QPixmap()
        self._bg_blurred = QtGui.QPixmap()
        self._bg_phase = 0.0
        self._bg_timer = QtCore.QTimer(self)
        self._bg_timer.setInterval(max(50, theme_int("servers.card_background_motion_ms", 90)))
        self._bg_timer.timeout.connect(self._advance_background)
        self._bg_timer.start()

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(13, 13, 13, 13)
        lay.setSpacing(9)

        top = QtWidgets.QHBoxLayout(); top.setSpacing(8)
        text = QtWidgets.QVBoxLayout(); text.setSpacing(1)
        self.nameLabel = MetallicLabel(server.get("name") or server.get("address", "Server")); self.nameLabel.setObjectName("serverName"); text.addWidget(self.nameLabel)
        self.addressLabel = QtWidgets.QLabel(server.get("address", "")); self.addressLabel.setObjectName("serverAddress"); text.addWidget(self.addressLabel)
        top.addLayout(text, 1)
        self.stateLabel = QtWidgets.QLabel(""); self.stateLabel.setObjectName("serverState")
        self.stateLabel.setFixedSize(12, 12)
        self.stateLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.stateLabel.setFixedSize(14, 14)
        self.stateLabel.setToolTip("Checking server…")
        top.addWidget(self.stateLabel, 0, QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignRight)
        lay.addLayout(top)

        self.levelshotLabel = None
        if self.presentation == "thumbnail":
            self.levelshotLabel = QtWidgets.QLabel()
            self.levelshotLabel.setObjectName("serverLevelshot")
            self.levelshotLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.levelshotLabel.setMinimumHeight(theme_int("servers.preview_height", 124))
            self.levelshotLabel.setMaximumHeight(theme_int("servers.preview_height", 124))
            self.levelshotLabel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            lay.addWidget(self.levelshotLabel)
        else:
            # Cinematic Noctra card exposes the slowly moving levelshot behind content.
            lay.addSpacing(78)

        telemetry = QtWidgets.QHBoxLayout(); telemetry.setSpacing(6)
        self.mapLabel = QtWidgets.QLabel("MAP  —"); self.mapLabel.setObjectName("serverMetric"); telemetry.addWidget(self.mapLabel, 1)
        self.playersLabel = QtWidgets.QLabel("PLAYERS  — / —"); self.playersLabel.setObjectName("serverMetric"); telemetry.addWidget(self.playersLabel)
        self.pingLabel = QtWidgets.QLabel("PING  —"); self.pingLabel.setObjectName("serverMetric"); telemetry.addWidget(self.pingLabel)
        lay.addLayout(telemetry)

        self.playerList = ServerPlayerTable(); self.playerList.setObjectName("serverPlayerList")
        self.playerList.setHeader(MaterialHeaderView(QtCore.Qt.Orientation.Horizontal, self.playerList))
        self.playerList.setItemDelegate(Q3NameDelegate(self.playerList)); self.playerList.setColumnCount(3); self.playerList.setHeaderLabels(["PLAYER", "SCORE", "PING"])
        self.playerList.setRootIsDecorated(False); self.playerList.setIndentation(0); self.playerList.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection); self.playerList.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.playerList.setUniformRowHeights(True); self.playerList.setAlternatingRowColors(False); self.playerList.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.playerList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch); self.playerList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed); self.playerList.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.playerList.setColumnWidth(1, 58); self.playerList.setColumnWidth(2, 72)
        self.playerList.headerItem().setTextAlignment(1, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)); self.playerList.headerItem().setTextAlignment(2, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
        self.playerList.setMinimumHeight(180); self.playerList.wheelRequested.connect(self.wheelRequested.emit); lay.addWidget(self.playerList, 1)

        self.editActions = QtWidgets.QWidget(); self.editActions.setObjectName("serverEditActions")
        edit_l = QtWidgets.QHBoxLayout(self.editActions); edit_l.setContentsMargins(0,0,0,0); edit_l.setSpacing(6)
        left_btn = MaterialPushButton("◀"); left_btn.setObjectName("serverEditButton"); left_btn.setToolTip("Move left"); left_btn.clicked.connect(lambda: self.moveRequested.emit(self.server.get("address", ""), -1)); edit_l.addWidget(left_btn)
        right_btn = MaterialPushButton("▶"); right_btn.setObjectName("serverEditButton"); right_btn.setToolTip("Move right"); right_btn.clicked.connect(lambda: self.moveRequested.emit(self.server.get("address", ""), 1)); edit_l.addWidget(right_btn)
        alias_btn = MaterialPushButton("ALIAS"); alias_btn.setObjectName("serverEditButton"); alias_btn.clicked.connect(lambda: self.aliasRequested.emit(self.server.get("address", ""))); edit_l.addWidget(alias_btn)
        edit_l.addStretch(1)
        remove_btn = MaterialPushButton("REMOVE"); remove_btn.setObjectName("serverRemoveButton"); remove_btn.clicked.connect(lambda: self.removeRequested.emit(self.server.get("address", ""))); edit_l.addWidget(remove_btn)
        self.editActions.hide(); lay.addWidget(self.editActions)

        actions = QtWidgets.QHBoxLayout(); actions.setSpacing(7)
        copy_btn = MaterialPushButton(""); copy_btn.setObjectName("iconButton"); copy_btn.setFixedSize(40,40); copy_btn.setToolTip("Copy server address")
        if qta is not None:
            try: copy_btn.setIcon(qta.icon("ph.copy", color=theme_value("colors.text.secondary", "#C3C5C8")))
            except Exception: copy_btn.setText("⧉")
        else: copy_btn.setText("⧉")
        copy_btn.clicked.connect(lambda: self.copyRequested.emit(self.server.get("address", ""))); actions.addWidget(copy_btn)
        self.connectButton = MaterialPushButton("CONNECT"); self.connectButton.setObjectName("serverConnectButton"); self.connectButton.setEnabled(q3elite_is_installed()); self.connectButton.clicked.connect(lambda: self.connectRequested.emit(self.server.get("address", ""))); actions.addWidget(self.connectButton, 1)
        lay.addLayout(actions)
        self._set_levelshot(str(server.get("mapname", "") or ""))

    def _advance_background(self):
        self._bg_phase = (self._bg_phase + 0.0032) % 1.0
        if self.isVisible(): self.update()

    @staticmethod
    def _cover_source_rect(pm, target_size, zoom=1.0, pan_x=0.0, pan_y=0.0):
        pw, ph = max(1, pm.width()), max(1, pm.height()); tw, th = max(1, target_size.width()), max(1, target_size.height())
        target_ratio = tw / th; source_ratio = pw / ph
        if source_ratio > target_ratio: base_h = ph; base_w = ph * target_ratio
        else: base_w = pw; base_h = pw / target_ratio
        zoom = max(1.0, float(zoom)); sw, sh = base_w / zoom, base_h / zoom
        max_dx = max(0.0, (pw-sw)/2.0); max_dy = max(0.0, (ph-sh)/2.0)
        cx = pw/2.0 + max(-1.0,min(1.0,pan_x))*max_dx*0.55; cy = ph/2.0 + max(-1.0,min(1.0,pan_y))*max_dy*0.40
        return QtCore.QRectF(cx-sw/2.0, cy-sh/2.0, sw, sh)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self); painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True); painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5,0.5,-0.5,-0.5); radius = float(theme_value("radius.lg",13)); path = QtGui.QPainterPath(); path.addRoundedRect(rect,radius,radius); painter.setClipPath(path)
        painter.fillPath(path, theme_color("colors.surface.panel", "#050505"))
        if self.presentation != "thumbnail":
            if not self._bg_blurred.isNull():
                import math
                wave = math.sin(self._bg_phase * math.tau); wave2 = math.cos(self._bg_phase * math.tau); zoom = theme_float("servers.card_background_zoom",1.10) + 0.018*wave
                src = self._cover_source_rect(self._bg_blurred,self.size(),zoom,wave2,wave); painter.drawPixmap(rect,self._bg_blurred,src)
                if not self._bg_source.isNull():
                    painter.save(); painter.setOpacity(0.11); painter.drawPixmap(rect,self._bg_source,self._cover_source_rect(self._bg_source,self.size(),zoom*1.01,wave2,wave)); painter.restore()
            overlay = QtGui.QLinearGradient(0,0,0,self.height()); overlay.setColorAt(0.0,theme_color("servers.card_overlay","rgba(0,0,0,155)")); overlay.setColorAt(0.36,QtGui.QColor(0,0,0,125)); overlay.setColorAt(1.0,theme_color("servers.card_overlay_bottom","rgba(0,0,0,210)")); painter.fillPath(path,overlay)
        painter.setClipping(False); painter.setPen(QtGui.QPen(theme_color("colors.border.soft","rgba(255,255,255,25)"),1.0)); painter.setBrush(QtCore.Qt.BrushStyle.NoBrush); painter.drawRoundedRect(rect,radius,radius)
        if OBSIDIAN_MATERIAL.enabled(ui_theme):
            OBSIDIAN_MATERIAL.paint_button_edge(
                painter, rect, radius, ui_theme, hovered=self.underMouse(), pressed=False,
                seed=sum(ord(c) for c in str(self.server.get("address", "server"))),
            )
        painter.end()

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta: self.wheelRequested.emit(1 if delta < 0 else -1); event.accept(); return
        super().wheelEvent(event)

    def set_edit_mode(self, enabled): self.editActions.setVisible(bool(enabled))
    def update_install_state(self): self.connectButton.setEnabled(q3elite_is_installed())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.levelshotLabel is not None:
            available = max(220, self.width() - 26)
            target_h = max(118, min(210, round(available * 9 / 16)))
            self.levelshotLabel.setFixedHeight(target_h)
            self._update_levelshot_label()

    def _update_levelshot_label(self):
        if self.levelshotLabel is None:
            return
        if self._bg_source.isNull():
            self.levelshotLabel.clear()
            return
        target = self.levelshotLabel.size()
        if target.width() < 8 or target.height() < 8:
            target = QtCore.QSize(max(240, self.width() - 26), theme_int("servers.preview_height", 124))
        fit_mode = str(theme_value("servers.preview_fit", "cover") or "cover").casefold()
        if fit_mode == "stretch":
            cropped = self._bg_source.scaled(
                target, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation
            )
        elif fit_mode == "contain":
            scaled = self._bg_source.scaled(target, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            canvas = QtGui.QPixmap(target)
            canvas.fill(theme_color("servers.preview_background", theme_value("colors.surface.deep", "#111111")))
            painter = QtGui.QPainter(canvas)
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap((target.width()-scaled.width())//2, (target.height()-scaled.height())//2, scaled)
            painter.end()
            cropped = canvas
        else:
            scaled = self._bg_source.scaled(target, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding, QtCore.Qt.TransformationMode.SmoothTransformation)
            x = max(0, (scaled.width() - target.width()) // 2)
            y = max(0, (scaled.height() - target.height()) // 2)
            cropped = scaled.copy(x, y, target.width(), target.height())
        self.levelshotLabel.setPixmap(_rounded_pixmap(cropped, theme_float("servers.preview_radius", 12.0)))

    def _set_levelshot(self, mapname):
        path = _resolve_levelshot_path(str(mapname or "").strip(), self.levelshots_dir)
        if not path:
            self._bg_source = QtGui.QPixmap(); self._bg_blurred = QtGui.QPixmap(); self._update_levelshot_label(); self.update(); return
        pix = _load_pixmap_file(path)
        if pix.isNull(): return
        self._bg_source = pix.scaled(900,506,QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,QtCore.Qt.TransformationMode.SmoothTransformation)
        if self.presentation == "thumbnail":
            self._bg_blurred = QtGui.QPixmap()
            self._update_levelshot_label()
        else:
            self._bg_blurred = _blur_pixmap(self._bg_source,radius=theme_float("servers.blur_radius",8.0),sample=QtCore.QSize(900,506))
        self.update()

    def apply_result(self, result):
        self.server.update(result); online = bool(result.get("online")); self.stateLabel.setText(""); self.stateLabel.setToolTip("Online" if online else "Offline"); self.stateLabel.setProperty("online",online); self.stateLabel.style().unpolish(self.stateLabel); self.stateLabel.style().polish(self.stateLabel)
        if not online:
            self.pingLabel.setText("PING  —"); self.mapLabel.setText("MAP  —"); self.playersLabel.setText("PLAYERS  0 / —"); self.playerList.clear(); self.playerList.addTopLevelItem(QtWidgets.QTreeWidgetItem(["Server did not respond.","",""])); self._set_levelshot(""); return
        hostname = _q3_plain_ascii(result.get("hostname","")); alias = _q3_plain_ascii(self.server.get("name","")); self.nameLabel.setText(alias or hostname or self.nameLabel.text())
        mapname = str(result.get("mapname","")).strip(); self.mapLabel.setText(f"MAP  {mapname or '—'}"); self.pingLabel.setText(f"PING  {result.get('ping','—')} ms"); self.playersLabel.setText(f"PLAYERS  {result.get('clients',0)} / {result.get('maxclients',0) or '—'}")
        self.playerList.clear(); players = result.get("players",[])
        if players:
            for p in players:
                raw_name = str(p.get("name","")); item = QtWidgets.QTreeWidgetItem([_q3_plain_ascii(raw_name),str(p.get("score",0)),f'{p.get("ping",0)} ms']); item.setData(0,QtCore.Qt.ItemDataRole.UserRole,raw_name); item.setTextAlignment(1,int(QtCore.Qt.AlignmentFlag.AlignRight|QtCore.Qt.AlignmentFlag.AlignVCenter)); item.setTextAlignment(2,int(QtCore.Qt.AlignmentFlag.AlignRight|QtCore.Qt.AlignmentFlag.AlignVCenter)); self.playerList.addTopLevelItem(item)
        else: self.playerList.addTopLevelItem(QtWidgets.QTreeWidgetItem(["Server is empty.","",""]))
        self._set_levelshot(mapname)


class DemoRenameFilter(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape:
            self.window._renameDemoCancelled = True
            self.window._cancel_demo_rename()
            return True
        return False


class DemoWheelFilter(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Type.Wheel:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                self.window._step_demo(1 if delta < 0 else -1)
                event.accept()
                return True
        return False


class ScreenshotPreviewLabel(QtWidgets.QLabel):
    resized = QtCore.pyqtSignal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class AspectRatioHost(QtWidgets.QWidget):
    """Centers one child inside a strict aspect-ratio rectangle."""
    def __init__(self, child, aspect_w=16, aspect_h=9, parent=None):
        super().__init__(parent)
        self.child = child
        self.aspect_w = max(1, int(aspect_w))
        self.aspect_h = max(1, int(aspect_h))
        self.child.setParent(self)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        available = self.contentsRect()
        if available.width() <= 1 or available.height() <= 1:
            return
        w = available.width()
        h = round(w * self.aspect_h / self.aspect_w)
        if h > available.height():
            h = available.height()
            w = round(h * self.aspect_w / self.aspect_h)
        x = available.x() + max(0, (available.width() - w) // 2)
        y = available.y() + max(0, (available.height() - h) // 2)
        self.child.setGeometry(x, y, max(1, w), max(1, h))


class ScreenshotSearchFilter(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Type.KeyPress:
            if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter,
                               QtCore.Qt.Key.Key_Escape):
                if self.window.pages.currentWidget() is getattr(self.window, "demosPage", None):
                    self.window.demoList.setFocus()
                else:
                    self.window.screenshotList.setFocus()
                event.accept()
                return True
        return False


class ScreenshotWheelFilter(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Type.Wheel:
            w = self.window
            if hasattr(w, "screenshotsPage") and w.pages.currentWidget() is w.screenshotsPage:
                pos = QtGui.QCursor.pos()
                list_top = w.screenshotList.mapToGlobal(QtCore.QPoint(0, 0))
                list_rect = QtCore.QRect(list_top, w.screenshotList.size())
                # V15 keeps the screenshot browser as a cyclic carousel.
                # Wheel over either preview or list moves selection and wraps
                # first <-> last instead of stopping at a scrollbar boundary.
                delta = event.angleDelta().y() or event.angleDelta().x()
                if delta:
                    w._step_screenshot(1 if delta < 0 else -1)
                    event.accept()
                    return True
        return False


class FullscreenScreenshotWheelFilter(QtCore.QObject):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Type.Wheel and self.dialog.isVisible():
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                self.dialog.step(1 if delta < 0 else -1)
                event.accept()
                return True
        return False



# ============================================================================
# LOCAL MAP CATALOG
# ============================================================================

def _map_pk3_candidates():
    """Return map PK3s from supported Q3Elite/preinstalled/downloaded locations."""
    baseq3 = GAME_ROOT / "baseq3"
    found = set()
    patterns = (
        "maps/*/*.pk3",                 # legacy/preinstalled layout
        "maps/*/*/*.pk3",               # legacy/preinstalled layout, 2 levels
        "mods/maps/*/*.pk3",            # preinstalled maps
        "mods/maps/*/*/*.pk3",          # preinstalled maps, 2 levels
        "mods/osp/baseq3/*.pk3",         # maps downloaded by OSP
        "mods/osp/baseq3/*/*.pk3",       # tolerate one grouping folder
        "*.pk3",                         # external PK3s in baseq3 root
        "mods/baseq3/*.pk3",             # external baseq3 mod path
    )
    for pattern in patterns:
        for path in baseq3.glob(pattern):
            if path.is_file():
                try:
                    found.add(path.resolve())
                except OSError:
                    found.add(path)
    return sorted(found, key=lambda p: str(p).casefold())


def _map_location(pk3_path):
    """Map a PK3 path to the browser's location filter."""
    try:
        rel = pk3_path.resolve().relative_to((GAME_ROOT / "baseq3").resolve())
        cf = rel.as_posix().casefold()
    except Exception:
        return "External"
    if cf.startswith("mods/osp/baseq3/") or cf.startswith("mods/baseq3/"):
        return "Downloaded"
    if cf.startswith("mods/maps/") or cf.startswith("maps/"):
        return "Preinstalled"
    return "External"


def _parse_arena_blocks(raw_text):
    """Parse mixed quoted/unquoted Quake 3 arena key/value syntax."""
    raw_text = re.sub(r"//.*?$", "", raw_text, flags=re.MULTILINE)
    arenas = {}
    for block in re.findall(r"\{(.*?)\}", raw_text, flags=re.DOTALL):
        values = {}
        # Keys are commonly unquoted while values are quoted:
        # map "q3dm6" / longname "The Camping Grounds" / type "ffa team tourney".
        pair_re = re.compile(
            r'(?:"([^"\\]*(?:\\.[^"\\]*)*)"|([^\s{}]+))'
            r'\s+'
            r'(?:"([^"\\]*(?:\\.[^"\\]*)*)"|([^\s{}]+))'
        )
        for match in pair_re.finditer(block):
            key = (match.group(1) if match.group(1) is not None else match.group(2) or "").strip().casefold()
            value = (match.group(3) if match.group(3) is not None else match.group(4) or "").strip()
            if key:
                values[key] = value
        map_name = values.get("map", "").strip().casefold()
        if map_name:
            arenas[map_name] = values
    return arenas


def _arena_gametypes(arena):
    if not arena:
        return "Unknown"
    # Arena type can be quoted whitespace text and occasionally comma separated.
    raw = re.split(r"[\s,;/]+", str(arena.get("type", "") or "").casefold())
    labels = []
    for token, label in (("ffa", "FFA"), ("team", "TDM"), ("tourney", "Duel"), ("ctf", "CTF")):
        if token in raw and label not in labels:
            labels.append(label)
    return " / ".join(labels) if labels else "Unknown"


def _safe_levelshot_name(map_name, suffix):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(map_name or "")).strip("._") or "unknown"
    return safe + suffix.lower()


def _map_manifest_key(path_value):
    """Portable POSIX key rooted at GAME_ROOT (legacy absolute keys migrate)."""
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    p = Path(raw)
    try:
        return p.resolve().relative_to(GAME_ROOT.resolve()).as_posix()
    except Exception:
        # A manifest may have been copied together with the game from another
        # drive. Preserve everything from baseq3 onward when possible.
        parts = list(p.parts)
        for index, part in enumerate(parts):
            if str(part).casefold() == "baseq3":
                return Path(*parts[index:]).as_posix()
        return raw.replace("\\", "/")


def _map_manifest_game_path(value):
    raw = str(value or "").strip()
    if not raw:
        return Path()
    p = Path(raw)
    if p.is_absolute():
        if p.exists():
            return p
        # Legacy absolute manifest copied to a new install location.
        key = _map_manifest_key(raw)
        if key and not Path(key).is_absolute():
            return GAME_ROOT / Path(*key.split("/"))
        return p
    return GAME_ROOT / Path(*raw.replace("\\", "/").split("/"))


def _map_manifest_data_rel(path_value):
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    p = Path(raw)
    try:
        return p.resolve().relative_to(LAUNCHER_DATA_DIR.resolve()).as_posix()
    except Exception:
        # Extracted map levelshots always live in Launcher/levelshots.  If an
        # old absolute AppData path is no longer valid, preserve the portable
        # folder/name pair rather than the old Windows username.
        if p.name:
            return (Path("levelshots") / p.name).as_posix()
        return raw.replace("\\", "/")


def _map_manifest_data_path(value):
    raw = str(value or "").strip()
    if not raw:
        return Path()
    p = Path(raw)
    if p.is_absolute():
        if p.exists():
            return p
        candidate = MAP_DISCOVERED_LEVELSHOTS_DIR / p.name
        return candidate if candidate.exists() else p
    return LAUNCHER_DATA_DIR / Path(*raw.replace("\\", "/").split("/"))


def _map_record_from_manifest(item, current_pak=None):
    out = dict(item or {})
    if current_pak is not None:
        out["pak_path"] = str(Path(current_pak))
    elif out.get("pak_path"):
        out["pak_path"] = str(_map_manifest_game_path(out.get("pak_path")))
    if out.get("levelshot"):
        out["levelshot"] = str(_map_manifest_data_path(out.get("levelshot")))
    return out


def _map_record_for_manifest(item):
    out = dict(item or {})
    if out.get("pak_path"):
        out["pak_path"] = _map_manifest_key(out.get("pak_path"))
    if out.get("levelshot"):
        out["levelshot"] = _map_manifest_data_rel(out.get("levelshot"))
    return out


def _scan_map_pk3(pk3_path, extract_levelshots=True):
    """Read BSP/arena metadata from one PK3 and extract matching levelshots."""
    result = []
    MAP_DISCOVERED_LEVELSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pk3_path, "r") as zf:
        names = zf.namelist()
        lower_to_real = {n.casefold(): n for n in names}
        arenas = {}
        for name in names:
            low = name.casefold()
            if low.endswith(".arena") or low.endswith("/arenas.txt"):
                try:
                    arenas.update(_parse_arena_blocks(
                        zf.read(name).decode("utf-8", errors="ignore")
                    ))
                except Exception:
                    pass

        for name in names:
            low = name.casefold()
            if not (low.startswith("maps/") and low.endswith(".bsp")):
                continue
            bsp_name = Path(name).stem
            arena = arenas.get(bsp_name.casefold(), {})
            long_name = str(arena.get("longname", "") or "").strip() or bsp_name
            gametype = _arena_gametypes(arena)

            levelshot_file = ""
            for ext in (".jpg", ".jpeg", ".png", ".webp", ".tga"):
                real = lower_to_real.get(f"levelshots/{bsp_name}{ext}".casefold())
                if not real:
                    continue
                dest = MAP_DISCOVERED_LEVELSHOTS_DIR / _safe_levelshot_name(bsp_name, ext)
                levelshot_file = str(dest)
                if extract_levelshots:
                    try:
                        dest.write_bytes(zf.read(real))
                    except Exception:
                        levelshot_file = ""
                break

            result.append({
                "map": bsp_name,
                "name": long_name,
                "pak": pk3_path.name,
                "pak_path": str(pk3_path),
                "pak_size": int(pk3_path.stat().st_size),
                "location": _map_location(pk3_path),
                "gametype": gametype,
                "levelshot": levelshot_file,
            })
    return result


def build_local_map_catalog():
    """Portable stat-based manifest; only new/changed PK3s are opened as ZIPs.

    Manifest v5 stores GAME_ROOT-relative PK3 paths and LAUNCHER_DATA_DIR-relative
    levelshots. v3/v4 absolute manifests are migrated transparently.
    """
    MAPS_MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    cached = {}
    try:
        manifest = json.loads(MAPS_MANIFEST_FILE.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and int(manifest.get("version", 0) or 0) in (3, 4, 5):
            raw_paks = manifest.get("paks", {})
            if isinstance(raw_paks, dict):
                for raw_key, entry in raw_paks.items():
                    if not isinstance(entry, dict):
                        continue
                    key = _map_manifest_key(raw_key)
                    if not key:
                        continue
                    cached[key] = {
                        "signature": dict(entry.get("signature", {}) or {}),
                        "maps": [dict(x) for x in (entry.get("maps", []) or []) if isinstance(x, dict)],
                    }
    except Exception:
        cached = {}

    runtime_updated = {}
    manifest_updated = {}
    changed_paks = 0
    for path in _map_pk3_candidates():
        try:
            st = path.stat()
        except OSError:
            continue
        key = _map_manifest_key(path)
        signature = {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
        old = cached.get(key, {})
        if old.get("signature") == signature and isinstance(old.get("maps"), list):
            maps = [_map_record_from_manifest(item, current_pak=path) for item in old["maps"]]
        else:
            try:
                maps = _scan_map_pk3(path, extract_levelshots=True)
                changed_paks += 1
            except (OSError, zipfile.BadZipFile) as error:
                print(f"[maps] Could not scan {path.name}: {error}")
                maps = []

        runtime_updated[key] = {"signature": signature, "maps": maps}
        manifest_updated[key] = {
            "signature": signature,
            "maps": [_map_record_for_manifest(item) for item in maps],
        }

    payload = {"version": 5, "paks": manifest_updated}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        existing = MAPS_MANIFEST_FILE.read_text(encoding="utf-8") if MAPS_MANIFEST_FILE.is_file() else ""
    except OSError:
        existing = ""
    if existing != encoded:
        tmp = MAPS_MANIFEST_FILE.with_suffix(".tmp")
        tmp.write_text(encoded, encoding="utf-8")
        tmp.replace(MAPS_MANIFEST_FILE)

    all_maps = []
    for entry in runtime_updated.values():
        all_maps.extend(entry.get("maps", []))
    all_maps.sort(key=lambda m: (
        str(m.get("name", "")).casefold(),
        str(m.get("pak", "")).casefold(),
    ))
    return all_maps, changed_paks


def _human_file_size(value):
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return "—"



def _net_text(url, timeout=15):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        parsed_host = (urllib.parse.urlparse(url).hostname or "").casefold()
        context = None
        if parsed_host == "lvlworld.com" or parsed_host.endswith(".lvlworld.com"):
            # LvLWorld currently serves an expired TLS certificate.
            # Keep this exception strictly scoped to LvLWorld; every other
            # launcher/update/download connection still verifies TLS normally.
            context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="ignore")
    except Exception as error:
        raise RuntimeError(f"{url} -> {type(error).__name__}: {error}") from error

def _plain(html):
    t=re.sub(r"<[^>]+>"," ",html)
    t=t.replace("&amp;","&").replace("&quot;",'"').replace("&#39;","'").replace("&nbsp;"," ")
    return re.sub(r"\s+"," ",t).strip()

def _ensure_worldspawn_index(progress=None):
    """Download DeFRaG Helper's public Worldspawn SQLite index once."""
    ONLINE_MAPS_DIR.mkdir(parents=True, exist_ok=True)
    if ONLINE_MAPS_DB.is_file() and ONLINE_MAPS_DB.stat().st_size > 64 * 1024:
        try:
            with sqlite3.connect(str(ONLINE_MAPS_DB)) as con:
                con.execute("SELECT 1 FROM Maps LIMIT 1").fetchone()
            return ONLINE_MAPS_DB
        except sqlite3.Error:
            try: ONLINE_MAPS_DB.unlink()
            except OSError: pass

    part = ONLINE_MAPS_DB.with_suffix(".db.part")
    if part.exists():
        try: part.unlink()
        except OSError: pass
    req = urllib.request.Request(
        ONLINE_MAPS_DB_URL,
        headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Q3Elite/1.0"}
    )
    with urllib.request.urlopen(req, timeout=45) as response, part.open("wb") as out:
        total=int(response.headers.get("Content-Length","0") or 0); done=0
        while True:
            chunk=response.read(256*1024)
            if not chunk: break
            out.write(chunk); done += len(chunk)
            if progress: progress(done,total)
    if part.stat().st_size < 64*1024:
        raise RuntimeError("Worldspawn map index download is unexpectedly small.")
    part.replace(ONLINE_MAPS_DB)
    with sqlite3.connect(str(ONLINE_MAPS_DB)) as con:
        con.execute("SELECT 1 FROM Maps LIMIT 1").fetchone()
    return ONLINE_MAPS_DB


def _worldspawn_columns(con):
    return {str(r[1]).casefold():str(r[1]) for r in con.execute("PRAGMA table_info(Maps)")}


def _worldspawn_row_to_item(row, cols):
    d={cols[i].casefold():row[i] for i in range(len(cols))}
    def val(*names):
        for name in names:
            v=d.get(name.casefold())
            if v not in (None,""): return str(v)
        return ""
    filename=Path(val("Filename").replace("\\","/")).name
    mapname=val("Mapname")
    if mapname.casefold().endswith(".bsp"): mapname=mapname[:-4]
    # MapData.db contains legacy detail links in some rows. Worldspawn's
    # current public detail route is /map/<bsp-name>/.
    detail=("https://ws.q3df.org/map/" + urllib.parse.quote(mapname, safe="._-") + "/") if mapname else val("LinkDetailpage")
    level=val("Levelshot","Screenshot")
    if level and not level.startswith(("http://","https://")):
        level=urllib.parse.urljoin("https://ws.q3df.org/",level)
    size=val("Size")
    try:
        n=float(size)
        # DeFRaG Helper stores the site's numeric size; retain unit if present,
        # otherwise present a compact MB value only for plausible byte counts.
        if n > 1024*1024: size=f"{n/(1024*1024):.1f} MB"
        elif n > 1024: size=f"{n/1024:.1f} KB"
    except Exception: pass
    # DeFRaG Helper stores the Worldspawn "Modification" value in Maps.Mod.
    # Style and Physics are separate DeFRaG properties, not gametypes.
    gt=val("Mod")
    return {
        "source":"Worldspawn Index",
        "name":val("Name") or mapname or filename,
        "map":mapname or val("Name"),
        "author":val("Author"),
        "pak":filename,
        "size":size,
        "gametype":"",
        "released":val("Releasedate"),
        "detail_url":detail,
        "download_url":("https://dl.defrag.racing/downloads/maps/"+urllib.parse.quote(filename)) if filename else "",
        # Search metadata comes from the local Worldspawn index, but images
        # are served through defrag.racing just like its official launcher.
        "levelshot_url":"",
        "defrag_map_name":mapname,
        "locked":not bool(filename),
    }



def _worldspawn_opener():
    # Worldspawn currently applies request/session checks to map assets.
    # Keep cookies from the detail-page request and reuse them for the image/PK3.
    import http.cookiejar
    jar=http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _worldspawn_headers(referer="https://ws.q3df.org/maps/"):
    return {
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":"en-US,en;q=0.9",
        "Accept-Encoding":"identity",
        "Referer":referer,
        "Connection":"close",
    }


def _worldspawn_fetch_levelshot(detail_url):
    if not detail_url:
        return b""
    if detail_url.startswith("/"):
        detail_url=urllib.parse.urljoin("https://ws.q3df.org/",detail_url)
    opener=_worldspawn_opener()
    req=urllib.request.Request(detail_url,headers=_worldspawn_headers("https://ws.q3df.org/maps/"))
    with opener.open(req,timeout=12) as response:
        html=response.read().decode(response.headers.get_content_charset() or "utf-8",errors="ignore")

    # Same element used by DeFRaG Helper. Keep this deliberately tolerant of
    # attribute order because Worldspawn has changed its markup over time.
    tag=re.search(r'<img\b[^>]*\bid=["\']mapdetails_levelshot["\'][^>]*>',html,re.I|re.S)
    if not tag:
        return b""
    m=re.search(r'\bsrc=["\']([^"\']+)["\']',tag.group(0),re.I)
    if not m:
        return b""
    image_url=urllib.parse.urljoin(detail_url,m.group(1))
    image_req=urllib.request.Request(image_url,headers=_worldspawn_headers(detail_url))
    with opener.open(image_req,timeout=12) as response:
        return response.read()


class WorldspawnLevelshotWorker(QtCore.QThread):
    ready=pyqtSignal(int,object)
    def __init__(self,row,detail_url,parent=None):
        super().__init__(parent); self.row=row; self.detail_url=detail_url
    def run(self):
        try:self.ready.emit(self.row,_worldspawn_fetch_levelshot(self.detail_url))
        except Exception as error:
            print(f"[online maps] levelshot: {self.detail_url} -> {type(error).__name__}: {error}")
            self.ready.emit(self.row,b"")

def _worldspawn_index_search(query="", limit=50, latest=False):
    db=_ensure_worldspawn_index()
    with sqlite3.connect(str(db)) as con:
        con.row_factory=None
        cmap=_worldspawn_columns(con)
        # Only ask for columns that really exist in this database version.
        wanted=["Name","Mapname","Filename","Releasedate","Author","Mod","Size",
                "Physics","LinkDetailpage","Style","Levelshot","Screenshot"]
        cols=[cmap[x.casefold()] for x in wanted if x.casefold() in cmap]
        if not cols: raise RuntimeError("Worldspawn index has no recognized Maps columns.")
        select=", ".join('"' + c.replace('"','""') + '"' for c in cols)
        if latest or not str(query).strip():
            order=cmap.get("releasedate","")
            sql=f"SELECT {select} FROM Maps"
            if order: sql += f' ORDER BY "{order}" DESC'
            sql += " LIMIT ?"
            rows=con.execute(sql,(int(limit),)).fetchall()
            total=con.execute("SELECT COUNT(*) FROM Maps").fetchone()[0]
        else:
            q="%"+str(query).strip()+"%"
            search_cols=[cmap[x] for x in ("name","mapname","filename","author") if x in cmap]
            where=" OR ".join(f'"{c}" LIKE ? COLLATE NOCASE' for c in search_cols)
            sql=f"SELECT {select} FROM Maps WHERE {where} LIMIT ?"
            rows=con.execute(sql, tuple([q]*len(search_cols)+[int(limit)])).fetchall()
            total=con.execute(f"SELECT COUNT(*) FROM Maps WHERE {where}",tuple([q]*len(search_cols))).fetchone()[0]
        return [_worldspawn_row_to_item(r,cols) for r in rows], {"total":int(total)}




class WorldspawnImageWorker(QtCore.QThread):
    ready=pyqtSignal(int,object)
    def __init__(self,row,url,parent=None):
        super().__init__(parent); self.row=row; self.url=url
    def run(self):
        temp=TEMP_DIR/f"ws_levelshot_{self.row}_{abs(hash(self.url))}.jpg"
        try:
            _download_worldspawn_dotnet(self.url,temp)
            self.ready.emit(self.row,temp.read_bytes())
        except Exception as error:
            print(f"[online maps] levelshot fallback: {self.url} -> {type(error).__name__}: {error}")
            self.ready.emit(self.row,b"")
        finally:
            try: temp.unlink()
            except OSError: pass



def _defrag_public_thumbnail(mapname):
    """
    Read the public map page exactly as a browser does. Do NOT send X-Inertia:
    Inertia returns HTTP 409 when the client asset version does not match the
    site's current version. The normal HTML page embeds the same props in the
    #app data-page attribute, including map.thumbnail.
    """
    if not mapname:
        return ""
    candidates=[str(mapname)]
    lower=str(mapname).casefold()
    if lower not in candidates:
        candidates.append(lower)

    for candidate in candidates:
        url="https://defrag.racing/maps/"+urllib.parse.quote(candidate,safe="._-")
        req=urllib.request.Request(url,headers={
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
            "Accept":"text/html,application/xhtml+xml",
            "Accept-Language":"en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req,timeout=15) as response:
                text=response.read().decode(response.headers.get_content_charset() or "utf-8",errors="replace")

            m=re.search(r'\bdata-page=(["\'])(.*?)\1',text,re.I|re.S)
            if not m:
                continue
            payload=json.loads(html.unescape(m.group(2)))
            props=payload.get("props",{}) if isinstance(payload,dict) else {}
            row=props.get("map",{}) if isinstance(props,dict) else {}
            thumb=str(row.get("thumbnail") or "").strip() if isinstance(row,dict) else ""
            if thumb:
                return thumb if thumb.startswith(("http://","https://")) else "https://defrag.racing/storage/"+thumb.lstrip("/")
        except Exception as error:
            print(f"[online maps] defrag thumbnail metadata: {candidate} -> {type(error).__name__}: {error}")
    return ""


class DefragThumbnailWorker(QtCore.QThread):
    ready=pyqtSignal(int,str)
    def __init__(self,row,mapname,parent=None):
        super().__init__(parent); self.row=row; self.mapname=mapname
    def run(self):
        self.ready.emit(self.row,_defrag_public_thumbnail(self.mapname))



def _lvlworld_abs(url):
    return urllib.parse.urljoin("https://lvlworld.com/", html.unescape(str(url or "")).strip())

def _lvlworld_id_from_href(href):
    m=re.search(r'/review/(?:id(?::|%3A))?(\d+)',str(href),re.I)
    return m.group(1) if m else ""

def _lvlworld_parse_map_links(page,limit=15):
    found=[]; seen=set()
    # Search and Latest use ordinary map links. Accept both id:123 and id%3A123.
    pat=r'<a\b([^>]*?)href=["\']([^"\']*/review/(?:id(?::|%3A))?\d+[^"\']*)["\']([^>]*)>(.*?)</a>'
    for m in re.finditer(pat,page,re.I|re.S):
        href=html.unescape(m.group(2)); mid=_lvlworld_id_from_href(href)
        if not mid or mid in seen: continue
        title=_plain(m.group(4)).strip()
        if not title or title.casefold() in {"review","download","comments","votes","media","overview"}:continue
        tail=page[m.end():m.end()+700]
        am=re.search(r'Author:\s*(?:<[^>]+>\s*)*([^<\r\n]+)',tail,re.I)
        author=html.unescape(am.group(1)).strip() if am else ""
        found.append({"source":"LvLWorld","lvl_id":mid,"name":title,"map":"",
            "author":author,"pak":"","size":"","gametype":"","released":"",
            "detail_url":f"https://lvlworld.com/review/id:{mid}",
            "download_page":f"https://lvlworld.com/download/id:{mid}",
            "download_url":"","levelshot_url":"","locked":False})
        seen.add(mid)
        if len(found)>=limit:break
    return found

def _online_map_key(item):
    source=str(item.get("source") or "")
    if source=="LvLWorld":
        return "lvlworld:"+str(item.get("lvl_id") or item.get("map") or "").casefold()
    return source.casefold()+":"+str(item.get("pak") or item.get("map") or "").casefold()

def _online_partial_path(item):
    """Stable transport cache path; never masquerades as an installable PK3."""
    key=_online_map_key(item)
    safe=re.sub(r'[^A-Za-z0-9._-]+',"_",key).strip("._") or "online_map"
    return DOWNLOADED_MAPS_DIR/(safe+".download.part")


def _online_installed_registry():
    try:
        data=json.loads(ONLINE_MAPS_INSTALLED_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except Exception:
        return {}

def _online_installed_files(item):
    names=_online_installed_registry().get(_online_map_key(item),[])
    if not isinstance(names,list): return []
    return [str(x) for x in names if (DOWNLOADED_MAPS_DIR/Path(str(x)).name).is_file()]

def _remember_online_install(item,names):
    data=_online_installed_registry()
    data[_online_map_key(item)]=[Path(str(x)).name for x in names]
    ONLINE_MAPS_INSTALLED_FILE.parent.mkdir(parents=True,exist_ok=True)
    ONLINE_MAPS_INSTALLED_FILE.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")

def _forget_online_install(item):
    data=_online_installed_registry()
    data.pop(_online_map_key(item),None)
    ONLINE_MAPS_INSTALLED_FILE.parent.mkdir(parents=True,exist_ok=True)
    ONLINE_MAPS_INSTALLED_FILE.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")


def _lvlworld_filter_tokens(page):
    # /filter embeds the same two security values pattern used by /download.
    patterns=[
        r'\btk\s*:\s*\[\s*["\']([0-9a-f]{16,})["\']\s*,\s*["\']([0-9a-f]{16,})["\']\s*\]',
        r'\bs\s*:\s*["\']([0-9a-f]{16,})["\'][\s\S]{0,300}?\bh\s*:\s*["\']([0-9a-f]{16,})["\']',
    ]
    for pat in patterns:
        m=re.search(pat,page,re.I)
        if m:return m.group(1),m.group(2)
    raise RuntimeError("LvLWorld /filter did not expose its request tokens.")

def _lvlworld_filter_request(criteria, jar=None, opener=None):
    import uuid
    page_url="https://lvlworld.com/filter"
    browser_ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36"
    if jar is None: jar=http.cookiejar.CookieJar()
    if opener is None:
        ctx=ssl._create_unverified_context()
        opener=urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=ctx),
        )
    req=urllib.request.Request(page_url,headers={
        "User-Agent":browser_ua,"Accept":"text/html,application/xhtml+xml",
        "Accept-Language":"en-US,en;q=0.9",
    })
    with opener.open(req,timeout=20) as response:
        page=response.read().decode(response.headers.get_content_charset() or "utf-8",errors="replace")
    token_s,token_h=_lvlworld_filter_tokens(page)
    boundary="----WebKitFormBoundary"+uuid.uuid4().hex[:16]
    fields={"q":json.dumps(criteria,separators=(",",":")),"s":token_s,"h":token_h}
    body=bytearray()
    for key,value in fields.items():
        body.extend(("--"+boundary+"\r\n").encode())
        body.extend((f'Content-Disposition: form-data; name="{key}"\r\n\r\n').encode())
        body.extend(str(value).encode("utf-8")); body.extend(b"\r\n")
    body.extend(("--"+boundary+"--\r\n").encode())
    req=urllib.request.Request(page_url,data=bytes(body),headers={
        "User-Agent":browser_ua,"Accept":"*/*",
        "Content-Type":"multipart/form-data; boundary="+boundary,
        "Origin":"https://lvlworld.com","Referer":page_url,
    },method="POST")
    with opener.open(req,timeout=20) as response:
        result=json.loads(response.read().decode("utf-8",errors="replace"))
    if not isinstance(result,dict) or not result.get("success"):
        raise RuntimeError("LvLWorld /filter rejected the map request.")
    return result

def _lvlworld_latest(limit=12):
    # LvLWorld returns 50 rows per page. Fetch only as many pages as required,
    # then expose 12/24/36/... rows to the UI.
    limit=max(1,int(limit))
    rows=[]; page_no=0; total=0
    while len(rows)<limit:
        result=_lvlworld_filter_request({
            "m":"Show all","s":"Date","o":"A to Z / Latest / Highest","p":page_no
        })
        total=int(result.get("total") or total)
        batch=result.get("data") or []
        if not batch: break
        for rec in batch:
            if not isinstance(rec,dict): continue
            mid=str(rec.get("i") or "").strip(); title=str(rec.get("t") or "").strip()
            stem=str(rec.get("f") or "").strip(); author=str(rec.get("a") or "").strip()
            if not mid or not title: continue
            qstem=urllib.parse.quote(stem,safe="._-!()+") if stem else ""
            rows.append({
                "source":"LvLWorld","lvl_id":mid,"name":title,"map":stem,
                "author":author,"pak":(stem+".zip") if stem else "",
                "size":"","gametype":"","released":"",
                "detail_url":f"https://lvlworld.com/review/id:{mid}",
                "download_page":f"https://lvlworld.com/download/id:{mid}",
                "download_url":"",
                "levelshot_url":f"https://lvlworld.com/levels/{qstem}/{qstem}320x240.jpg" if qstem else "",
                "locked":False,
            })
            if len(rows)>=limit: break
        page_no+=1
        if total and page_no * len(batch) >= total: break
    return rows,{"total":total or len(rows)}

def _lvlworld_search(query,limit=12):
    q=str(query or "").strip()
    if not q:
        return [],{"total":0}

    # LvLWorld live search endpoint used by the site's own search box.
    # It accepts multipart/form-data with a single "q" field and returns JSON:
    # {"success":1,"q":"...","data":[{"id","title","fileName","author"}, ...]}
    import uuid
    boundary="----WebKitFormBoundary"+uuid.uuid4().hex[:16]
    body=(
        "--"+boundary+"\r\n"
        'Content-Disposition: form-data; name="q"\r\n\r\n'
        +q+"\r\n"
        "--"+boundary+"--\r\n"
    ).encode("utf-8")

    url="https://lvlworld.com/search-query"
    req=urllib.request.Request(url,data=body,headers={
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
        "Accept":"*/*",
        "Accept-Language":"en-US,en;q=0.9",
        "Content-Type":"multipart/form-data; boundary="+boundary,
        "Origin":"https://lvlworld.com",
        "Referer":"https://lvlworld.com/filter",
    },method="POST")

    # lvlworld.com currently has a certificate-chain/expiry problem on some
    # Windows/Python installations; keep the same narrowly scoped workaround
    # already used elsewhere in the launcher for this host.
    ctx=ssl._create_unverified_context()
    with urllib.request.urlopen(req,timeout=20,context=ctx) as response:
        result=json.loads(response.read().decode("utf-8",errors="replace"))

    if not isinstance(result,dict) or not result.get("success"):
        return [],{"total":0}

    rows=[]
    for rec in (result.get("data") or []):
        if not isinstance(rec,dict):
            continue
        mid=str(rec.get("id") or "").strip()
        title=str(rec.get("title") or "").strip()
        stem=str(rec.get("fileName") or "").strip()
        author=str(rec.get("author") or "").strip()
        if not mid or not title:
            continue

        qstem=urllib.parse.quote(stem,safe="._-!()+") if stem else ""
        # Search cards use /levels/<fileName>/<fileName>sm.jpg. The larger
        # 320x240 image is attempted first by the existing LvLWorld image worker.
        shot=f"https://lvlworld.com/levels/{qstem}/{qstem}320x240.jpg" if qstem else ""

        rows.append({
            "source":"LvLWorld",
            "lvl_id":mid,
            "name":title,
            "map":stem,
            "author":author,
            "pak":"",
            "size":"",
            "gametype":"",
            "released":"",
            "detail_url":f"https://lvlworld.com/review/id:{mid}",
            "download_page":f"https://lvlworld.com/download/id:{mid}",
            "download_url":"",
            "levelshot_url":shot,
            "locked":False,
        })
        if len(rows)>=max(1,int(limit)):
            break

    return rows,{"total":len(result.get("data") or [])}


def _lvlworld_resolve_download(item):
    """
    Reproduce LvLWorld's own download-page JavaScript:
      GET /download/id:<id>
      -> z = ZIP basename and dl.tk = [s, h]
      POST /dload with z,s,h,d=lvl
      -> {success,l,p,t,z}
      -> <l>/<p>/<z>?<t>
    Keep one cookie jar/opener for both requests because tokens may be session-bound.
    """
    page_url=item.get("download_page") or ("https://lvlworld.com/download/id:"+str(item.get("lvl_id") or ""))
    jar=http.cookiejar.CookieJar()
    ctx=ssl._create_unverified_context()
    opener=urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx),
    )
    headers={
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
        "Accept":"text/html,application/xhtml+xml,application/json,*/*",
        "Referer":str(page_url),
    }
    req=urllib.request.Request(str(page_url),headers=headers)
    with opener.open(req,timeout=20) as response:
        page=response.read().decode(response.headers.get_content_charset() or "utf-8",errors="replace")

    zm=re.search(r'f\.append\(["\']z["\']\s*,\s*["\']([^"\']+)["\']\)',page,re.I)
    if not zm:
        # Fallback to the visible metadata line: foo.zip, 2.24 MiB
        plain=_plain(page)
        fm=re.search(r'([A-Za-z0-9_.!+()\- ]+)\.zip\b',plain,re.I)
        if not fm: raise RuntimeError("LvLWorld page did not expose the ZIP name.")
        zip_base=fm.group(1).strip()
    else:
        zip_base=html.unescape(zm.group(1)).strip()

    tm=re.search(r'\btk\s*:\s*\[\s*["\']([0-9a-f]+)["\']\s*,\s*["\']([0-9a-f]+)["\']\s*\]',page,re.I)
    if not tm: raise RuntimeError("LvLWorld page did not expose download tokens.")
    token_s,token_h=tm.group(1),tm.group(2)

    import uuid
    boundary="----WebKitFormBoundary"+uuid.uuid4().hex[:16]
    body=bytearray()
    for key,value in {"z":zip_base,"s":token_s,"h":token_h,"d":"lvl"}.items():
        body.extend(("--"+boundary+"\r\n").encode())
        body.extend((f'Content-Disposition: form-data; name="{key}"\r\n\r\n').encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(("--"+boundary+"--\r\n").encode())
    req=urllib.request.Request(
        "https://lvlworld.com/dload",
        data=bytes(body),
        headers={
            "User-Agent":headers["User-Agent"],
            "Accept":"*/*",
            "Content-Type":"multipart/form-data; boundary="+boundary,
            "Origin":"https://lvlworld.com",
            "Referer":str(page_url),
        },
        method="POST",
    )
    with opener.open(req,timeout=20) as response:
        raw=response.read().decode("utf-8",errors="replace")
    try:
        result=json.loads(raw)
    except Exception:
        raise RuntimeError("LvLWorld returned an invalid download-token response.")
    if not isinstance(result,dict) or not result.get("success"):
        raise RuntimeError("LvLWorld was unable to issue a download token.")

    base=str(result.get("l") or "").rstrip("/")
    sub=str(result.get("p") or "").strip("/")
    filename=Path(str(result.get("z") or (zip_base+".zip")).replace("\\","/")).name
    token=str(result.get("t") or "").strip()
    if not base or not filename or not token:
        raise RuntimeError("LvLWorld returned incomplete download information.")
    final=base+"/"+(sub+"/" if sub else "")+urllib.parse.quote(filename,safe="._-!()+") + "?" + urllib.parse.quote(token,safe="")
    return final,filename



def _lvlworld_review_metadata(lvl_id):
    url="https://lvlworld.com/review/id:"+str(lvl_id)
    req=urllib.request.Request(url,headers={
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
        "Accept":"text/html,application/xhtml+xml",
        "Referer":"https://lvlworld.com/filter",
    })
    ctx=ssl._create_unverified_context()
    with urllib.request.urlopen(req,timeout=20,context=ctx) as response:
        page=response.read().decode(response.headers.get_content_charset() or "utf-8",errors="replace")
    plain=_plain(page)

    size=""
    m=re.search(r'\bFile:\s*[^,\n]+,\s*([0-9]+(?:\.[0-9]+)?\s*(?:KiB|MiB|GiB|KB|MB|GB))',plain,re.I)
    if m:size=m.group(1).replace("\xa0"," ")

    released=""
    m=re.search(r'\bAdded:\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})',plain,re.I)
    if m:released=m.group(1)

    levelshot=""
    m=re.search(r'<img[^>]+src=["\']([^"\']+320x240\.(?:jpg|jpeg|png|webp))["\']',page,re.I)
    if m:levelshot=urllib.parse.urljoin(url,html.unescape(m.group(1)))

    pak=""
    try:
        durl="https://lvlworld.com/download/id:"+str(lvl_id)
        dreq=urllib.request.Request(durl,headers={
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
            "Accept":"text/html,application/xhtml+xml","Referer":url,
        })
        with urllib.request.urlopen(dreq,timeout=20,context=ctx) as response:
            dpage=response.read().decode(response.headers.get_content_charset() or "utf-8",errors="replace")
        zm=re.search(r'f\.append\(["\']z["\']\s*,\s*["\']([^"\']+)["\']\)',dpage,re.I)
        if zm:pak=html.unescape(zm.group(1)).strip()+".zip"
    except Exception:
        pass
    return {"size":size,"released":released,"pak":pak,"levelshot_url":levelshot}

class LvLWorldMetadataWorker(QtCore.QThread):
    ready=pyqtSignal(int,object,int)
    def __init__(self,row,lvl_id,generation,parent=None):
        super().__init__(parent); self.row=row; self.lvl_id=lvl_id; self.generation=generation
    def run(self):
        try:self.ready.emit(self.row,_lvlworld_review_metadata(self.lvl_id),self.generation)
        except Exception as error:
            print(f"[online maps] LvLWorld metadata {self.lvl_id}: {type(error).__name__}: {error}")
            self.ready.emit(self.row,{},self.generation)


class LvLWorldImageWorker(QtCore.QThread):
    ready=pyqtSignal(int,object,int)
    def __init__(self,row,url,generation,parent=None):
        super().__init__(parent); self.row=row; self.url=url; self.generation=generation
    def run(self):
        try:
            req=urllib.request.Request(self.url,headers={
                "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
                "Accept":"image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer":"https://lvlworld.com/filter",
            })
            ctx=ssl._create_unverified_context()
            with urllib.request.urlopen(req,timeout=20,context=ctx) as response:
                self.ready.emit(self.row,response.read(),self.generation)
        except Exception as error:
            print(f"[online maps] LvLWorld levelshot: {self.url} -> {type(error).__name__}: {error}")
            self.ready.emit(self.row,b"",self.generation)


class SourceFaviconWorker(QtCore.QThread):
    ready=pyqtSignal(str,object)
    def __init__(self,source,parent=None):
        super().__init__(parent); self.source=source
    def run(self):
        url="https://lvlworld.com/favicon.ico" if self.source=="LvLWorld" else "https://ws.q3df.org/favicon.ico"
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"image/*,*/*;q=0.8"})
            ctx=ssl._create_unverified_context() if self.source=="LvLWorld" else ssl.create_default_context()
            with urllib.request.urlopen(req,timeout=15,context=ctx) as response:
                self.ready.emit(self.source,response.read())
        except Exception as error:
            print(f"[online maps] favicon {self.source}: {type(error).__name__}: {error}")
            self.ready.emit(self.source,b"")


class OnlineMapsWorker(QtCore.QThread):
    ready = pyqtSignal(object, str, object)
    failed = pyqtSignal(str)
    def __init__(self, mode, query="", page=1, parent=None, limit=12):
        super().__init__(parent); self.mode=mode; self.query=query; self.page=page; self.limit=limit
    def run(self):
        try:
            if self.mode=="search":
                ws,wm=_worldspawn_index_search(self.query,15,False)
                try: lv,lm=_lvlworld_search(self.query,12)
                except Exception as error:
                    print(f"[online maps] LvLWorld search: {type(error).__name__}: {error}")
                    lv,lm=[],{"total":0}
                # Interleave sources so neither site monopolizes the first screen.
                data=[]
                for i in range(max(len(ws),len(lv))):
                    if i<len(lv):data.append(lv[i])
                    if i<len(ws):data.append(ws[i])
                self.ready.emit(data,"LvLWorld + Worldspawn",{
                    "total":int((lm or {}).get("total") or len(lv))+int((wm or {}).get("total") or len(ws))
                })
            else:
                data,meta=_lvlworld_latest(self.limit)
                self.ready.emit(data,"LvLWorld",meta)
        except Exception as error:
            self.failed.emit(str(error))



def _download_worldspawn_dotnet(url, target, progress_callback=None):
    """
    Worldspawn accepts the .NET HttpClient used by DeFRaG Helper while rejecting
    Python urllib requests. Use PowerShell/.NET HttpClient for this host.
    """
    target=Path(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    ps = r"""
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http
$url = $env:Q3ELITE_WS_URL
$out = $env:Q3ELITE_WS_OUT
if ([string]::IsNullOrWhiteSpace($url)) { throw "Q3ELITE_WS_URL is empty" }
if ([string]::IsNullOrWhiteSpace($out)) { throw "Q3ELITE_WS_OUT is empty" }
$uri = New-Object System.Uri($url, [System.UriKind]::Absolute)
$handler = New-Object System.Net.Http.HttpClientHandler
$client = New-Object System.Net.Http.HttpClient($handler)
try {
    $response = $client.GetAsync($uri, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
    $response.EnsureSuccessStatusCode() | Out-Null
    $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    $outputStream = [System.IO.File]::Open($out, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $buffer = New-Object byte[] 262144
        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $outputStream.Write($buffer, 0, $read)
        }
    } finally {
        $outputStream.Dispose()
        $inputStream.Dispose()
    }
} finally {
    $client.Dispose()
    $handler.Dispose()
}
"""
    cmd=[
        "powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
        "-Command", ps
    ]
    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)
    child_env=os.environ.copy()
    child_env["Q3ELITE_WS_URL"]=str(url)
    child_env["Q3ELITE_WS_OUT"]=str(target)
    result=subprocess.run(
        cmd,capture_output=True,text=True,creationflags=creationflags,env=child_env
    )
    if result.returncode:
        message=(result.stderr or result.stdout or "PowerShell HttpClient failed").strip()
        raise RuntimeError(message)
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("Worldspawn returned an empty download.")
    return target


class OnlineMapDownloadWorker(QtCore.QThread):
    ready=pyqtSignal(str); failed=pyqtSignal(str); progress=pyqtSignal(int); paused=pyqtSignal()

    def __init__(self,item,parent=None):
        super().__init__(parent)
        self.item=dict(item)
        self._pause_requested=False

    def request_pause(self):
        self._pause_requested=True

    def run(self):
        part=_online_partial_path(self.item)
        try:
            url=self.item.get("download_url","")
            resolved_name=""
            if self.item.get("source")=="LvLWorld":
                url,resolved_name=_lvlworld_resolve_download(self.item)
            if not url:
                raise RuntimeError("No downloadable package is exposed by this source.")

            DOWNLOADED_MAPS_DIR.mkdir(parents=True,exist_ok=True)
            response_name=Path(str(resolved_name or self.item.get("pak") or Path(urllib.parse.urlparse(url).path).name or "online_map").replace("\\","/")).name
            host=(urllib.parse.urlparse(url).hostname or "").casefold()
            context=ssl._create_unverified_context() if (host=="lvlworld.com" or host.endswith(".lvlworld.com")) else None

            existing=part.stat().st_size if part.is_file() else 0
            headers={"User-Agent":"Quake3EliteLauncher/0.1"}
            if existing:
                headers["Range"]=f"bytes={existing}-"
            req=urllib.request.Request(url,headers=headers)

            with urllib.request.urlopen(req,timeout=30,context=context) as r:
                status=int(getattr(r,"status",None) or r.getcode())
                cd=r.headers.get("Content-Disposition","")
                m=re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^;"\']+)',cd,re.I)
                if m:
                    response_name=Path(urllib.parse.unquote(m.group(1).strip())).name
                final_url_name=Path(urllib.parse.urlparse(r.geturl()).path).name
                if final_url_name.casefold().endswith((".pk3",".zip")):
                    response_name=urllib.parse.unquote(final_url_name)

                resumed=bool(existing and status==206)
                if existing and not resumed:
                    existing=0
                content_len=int(r.headers.get("Content-Length","0") or 0)
                total=existing+content_len if content_len else 0
                done=existing

                with part.open("ab" if resumed else "wb") as f:
                    # Emit after the file is open and the request is established,
                    # so the UI definitely switches DOWNLOAD -> PAUSE.
                    if total:self.progress.emit(min(99,int(done*100/total)))
                    else:self.progress.emit(0)
                    while True:
                        if self._pause_requested:
                            f.flush()
                            self.paused.emit()
                            return
                        chunk=r.read(262144)
                        if not chunk:break
                        f.write(chunk); done+=len(chunk)
                        if total:self.progress.emit(min(99,int(done*100/total)))

            if not zipfile.is_zipfile(part):
                raise RuntimeError("Downloaded package is incomplete or is not a valid ZIP/PK3.")

            # Do not trust the URL/name extension. A LvLWorld .zip may contain
            # one or more PK3s; a direct Worldspawn PK3 is itself a ZIP archive.
            with zipfile.ZipFile(part,"r") as z:
                bad=z.testzip()
                if bad:raise RuntimeError(f"Archive verification failed at {bad}.")
                infos=[x for x in z.infolist() if not x.is_dir()]
                nested=[x for x in infos if x.filename.casefold().endswith(".pk3")]
                direct_bsp=any(x.filename.casefold().startswith("maps/") and x.filename.casefold().endswith(".bsp") for x in infos)

            installed=[]
            if nested:
                with zipfile.ZipFile(part,"r") as z:
                    for info in nested:
                        target=DOWNLOADED_MAPS_DIR/Path(info.filename).name
                        with z.open(info) as source,target.open("wb") as dest:
                            shutil.copyfileobj(source,dest)
                        if not zipfile.is_zipfile(target):
                            target.unlink(missing_ok=True)
                            raise RuntimeError(f"Extracted {target.name} is not a valid PK3.")
                        installed.append(target.name)
            elif direct_bsp:
                # Direct PK3: force a .pk3 final name even if the transport URL
                # or metadata supplied an incorrect/ambiguous extension.
                final_name=Path(response_name).name
                if not final_name.casefold().endswith(".pk3"):
                    final_name=Path(str(self.item.get("pak") or self.item.get("map") or "online_map")).stem+".pk3"
                target=DOWNLOADED_MAPS_DIR/final_name
                if target.exists():target.unlink()
                shutil.copy2(part,target)
                installed.append(target.name)
            else:
                raise RuntimeError("Archive contains no PK3 and is not a direct Quake 3 PK3.")

            part.unlink(missing_ok=True)
            _remember_online_install(self.item,installed)
            self.progress.emit(100)
            self.ready.emit(", ".join(installed))
        except Exception as e:
            self.failed.emit(str(e))

class MapCatalogWorker(QtCore.QThread):
    ready = pyqtSignal(object, int)
    failed = pyqtSignal(str)

    def run(self):
        try:
            maps, changed = build_local_map_catalog()
            self.ready.emit(maps, changed)
        except Exception as error:
            self.failed.emit(str(error))


class AspectFillLabel(QtWidgets.QLabel):
    """Fixed-aspect thumbnail that cover-crops source art instead of letterboxing."""
    def __init__(self, aspect_w=16, aspect_h=9, parent=None):
        super().__init__(parent)
        self._source = QtGui.QPixmap()
        self._aspect_w = max(1, int(aspect_w))
        self._aspect_h = max(1, int(aspect_h))
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setScaledContents(False)

    def setSourcePixmap(self, pixmap):
        self._source = QtGui.QPixmap(pixmap) if pixmap is not None else QtGui.QPixmap()
        self._refresh_pixmap()

    def _refresh_pixmap(self):
        if self._source.isNull() or self.width() < 2 or self.height() < 2:
            self.clear()
            return
        target = self.size()
        scaled = self._source.scaled(
            target,
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - target.width()) // 2)
        y = max(0, (scaled.height() - target.height()) // 2)
        cropped = scaled.copy(x, y, target.width(), target.height())
        self.setPixmap(_rounded_pixmap(cropped, theme_float("maps.levelshot_radius", 9.0)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_pixmap()


def _circular_icon_from_pixmap(pixmap, size=18):
    if pixmap is None or pixmap.isNull():
        return QtGui.QIcon()
    size = max(8, int(size))
    src = pixmap.scaled(size, size, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        QtCore.Qt.TransformationMode.SmoothTransformation)
    canvas = QtGui.QPixmap(size, size)
    canvas.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(canvas)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    path = QtGui.QPainterPath()
    path.addEllipse(QtCore.QRectF(0, 0, size, size))
    painter.setClipPath(path)
    x = max(0, (src.width() - size) // 2)
    y = max(0, (src.height() - size) // 2)
    painter.drawPixmap(0, 0, src.copy(x, y, size, size))
    painter.end()
    return QtGui.QIcon(canvas)


class MapRowHoverDelegate(FullRowHoverDelegate):
    """Compatibility name used by Maps; now paints one continuous row."""
    pass


class ModernLauncherWindow(QtWidgets.QMainWindow):
    """Responsive 1366x768+ Q3Elite launcher — V15 White Paper / Obsidian."""

    def __init__(self):
        QtWidgets.QMainWindow.__init__(self)
        self._mapNetwork = QtNetwork.QNetworkAccessManager(self)
        self._worldspawnShotWorkers = []
        self._onlineThumbGeneration = 0

        self.setObjectName("launcherWindow")
        self.setWindowTitle("Quake 3 Elite Launcher")
        self.setMinimumSize(
            theme_int("layout.window.min_width", 1366),
            theme_int("layout.window.min_height", 768),
        )
        max_w = theme_int("layout.window.max_width", 0)
        max_h = theme_int("layout.window.max_height", 0)
        self.setMaximumSize(
            max_w if max_w > 0 else QtWidgets.QWIDGETSIZE_MAX,
            max_h if max_h > 0 else QtWidgets.QWIDGETSIZE_MAX,
        )
        self.resize(
            theme_int("layout.window.default_width", 1366),
            theme_int("layout.window.default_height", 768),
        )
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
        # V9 reserves a transparent outer gutter for a real launcher shadow.
        # The complete application UI lives inside one rounded shell, which also
        # eliminates the child/background corner disagreement from earlier builds.
        outer = 0 if (self.isMaximized() or self.isFullScreen()) else theme_int("layout.window.outer_margin", 13)
        self.shell.setGeometry(outer, outer, max(1, self.width() - outer * 2), max(1, self.height() - outer * 2))

        root = QtWidgets.QHBoxLayout(self.shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # LEFT RAIL ---------------------------------------------------------
        self.sidebar = GlassFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(theme_int("layout.sidebar_width", 112))
        self.sideLayout = QtWidgets.QVBoxLayout(self.sidebar)
        side = self.sideLayout
        rail_pad = theme_int("layout.sidebar_padding", 20)
        side.setContentsMargins(rail_pad, 16, rail_pad, 14)
        side.setSpacing(6)
        side.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)

        self.brandLabel = QtWidgets.QLabel()
        self.brandLabel.setObjectName("brandMark")
        self.brandLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        logo_size = theme_int("layout.sidebar.logo_size", 38)
        self.brandLabel.setFixedSize(58, 52)
        self.brandLabel.setToolTip("Quake 3 Elite")
        svg_logo = ICONS_DIR / "favicon.svg"
        brand_icon_path = svg_logo if svg_logo.is_file() else (APP_ICON_PNG if APP_ICON_PNG.is_file() else APP_ICON_ICO)
        if brand_icon_path.is_file():
            brand_icon = QtGui.QIcon(str(brand_icon_path))
            self.brandLabel.setPixmap(brand_icon.pixmap(QtCore.QSize(logo_size, logo_size)))
        else:
            self.brandLabel.setText("Q3E")
        side.addWidget(self.brandLabel, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        side.addSpacing(8)

        self.homeNav = self._nav_button("HOME", "home", "fa5s.home")
        self.addonsNav = self._nav_button("INSTALL ADDONS", "addons", "fa5s.puzzle-piece")
        self.statisticsNav = self._nav_button("STATISTICS", "statistics", "fa5s.chart-bar")
        self.serversNav = self._nav_button("SERVERS", "servers", "fa5s.server")
        self.mapsNav = self._nav_button("MAPS", "maps", "fa5s.map")
        self.matchmakingNav = self._nav_button("MATCHMAKING", "matchmaking", "fa5s.bell")
        self.screenshotsNav = self._nav_button("SCREENSHOTS", "screenshots", "fa5s.image")
        self.demosNav = self._nav_button("DEMOS", "demos", "fa5s.film")
        self.settingsNav = self._nav_button("SETTINGS", "settings", "fa5s.cog")
        self.changelogNav = self._nav_button("CHANGELOG", "changelog", "fa5s.scroll")

        def separator():
            line = QtWidgets.QFrame(); line.setObjectName("sidebarSeparator"); line.setFixedSize(42, 1); return line

        for button in (self.homeNav, self.addonsNav, self.statisticsNav): side.addWidget(button, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        side.addWidget(separator(), 0, QtCore.Qt.AlignmentFlag.AlignHCenter); side.addSpacing(4)
        for button in (self.serversNav, self.mapsNav, self.matchmakingNav): side.addWidget(button, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        side.addWidget(separator(), 0, QtCore.Qt.AlignmentFlag.AlignHCenter); side.addSpacing(4)
        for button in (self.screenshotsNav, self.demosNav, self.changelogNav): side.addWidget(button, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        self.navIndicator = NavRailIndicator(self.sidebar)
        self.navIndicator.lower()
        self._navIndicatorAnim = QtCore.QPropertyAnimation(self.navIndicator, b"geometry", self)
        self._navIndicatorAnim.setDuration(theme_int("motion.nav", 88))
        self._navIndicatorAnim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        QtCore.QTimer.singleShot(0, lambda: self._move_nav_indicator(self.homeNav, animate=False))

        side.addStretch(1)
        side.addWidget(self.settingsNav, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        side.addSpacing(6)
        self.railStatus = QtWidgets.QLabel("●")
        self.railStatus.setObjectName("railStatus")
        self.railStatus.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.railStatus.setToolTip("Q3Elite Launcher")
        self.railStatus.setStyleSheet(
            f"color:{theme_value('colors.semantic.success', '#89C541')}; background:transparent;"
        )
        # Theme-specific default mouse cursor.  Setting it on the launcher
        # window preserves explicit child cursors (buttons/links can still use
        # PointingHandCursor) unlike QApplication.setOverrideCursor().
        try:
            cursor_enabled = bool(theme_value("cursor.enabled", False))
            cursor_path = theme_asset("cursor.path")
            if (
                cursor_enabled
                and cursor_path is not None
                and Path(cursor_path).is_file()
            ):
                cursor_pm = QtGui.QPixmap(str(cursor_path))
                if not cursor_pm.isNull():
                    # Large source images (for example 256x256) need to be
                    # reducible far below 25%.  The previous 0.25 clamp meant
                    # scale=0.10 still rendered as 64x64.
                    cursor_scale = max(
                        0.02,
                        min(8.0, theme_float("cursor.scale", 1.0)),
                    )

                    # Optional absolute logical size is easier to tune than a
                    # multiplier. If cursor.size > 0 it overrides cursor.scale.
                    cursor_size = theme_int("cursor.size", 0)
                    if cursor_size > 0:
                        target_w = max(1, cursor_size)
                        target_h = max(
                            1,
                            round(
                                cursor_pm.height()
                                * (target_w / max(1, cursor_pm.width()))
                            ),
                        )
                    else:
                        target_w = max(
                            1, round(cursor_pm.width() * cursor_scale)
                        )
                        target_h = max(
                            1, round(cursor_pm.height() * cursor_scale)
                        )

                    if (
                        target_w != cursor_pm.width()
                        or target_h != cursor_pm.height()
                    ):
                        cursor_pm = cursor_pm.scaled(
                            target_w,
                            target_h,
                            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                            QtCore.Qt.TransformationMode.SmoothTransformation,
                        )
                    # Qt only accepts hotspots inside the cursor pixmap;
                    # negative values are otherwise interpreted as an automatic
                    # hotspot.  Expand the pixmap with transparent padding so
                    # arbitrary negative/out-of-bounds theme coordinates become
                    # real visual offsets.
                    hotspot_x = theme_int("cursor.hotspot_x", 0)
                    hotspot_y = theme_int("cursor.hotspot_y", 0)

                    original_w = max(1, cursor_pm.width())
                    original_h = max(1, cursor_pm.height())

                    pad_left = max(0, -hotspot_x)
                    pad_top = max(0, -hotspot_y)
                    pad_right = max(0, hotspot_x - (original_w - 1))
                    pad_bottom = max(0, hotspot_y - (original_h - 1))

                    if pad_left or pad_top or pad_right or pad_bottom:
                        expanded = QtGui.QPixmap(
                            original_w + pad_left + pad_right,
                            original_h + pad_top + pad_bottom,
                        )
                        expanded.fill(QtCore.Qt.GlobalColor.transparent)

                        cp = QtGui.QPainter(expanded)
                        cp.setCompositionMode(
                            QtGui.QPainter.CompositionMode.CompositionMode_Source
                        )
                        cp.drawPixmap(pad_left, pad_top, cursor_pm)
                        cp.end()

                        cursor_pm = expanded
                        hotspot_x += pad_left
                        hotspot_y += pad_top

                    self.setCursor(
                        QtGui.QCursor(cursor_pm, hotspot_x, hotspot_y)
                    )
                else:
                    self.unsetCursor()
            else:
                self.unsetCursor()
        except Exception as error:
            print(f"[ui] Cursor theme error: {error}")
            self.unsetCursor()

        side.addWidget(self.railStatus)
        root.addWidget(self.sidebar)

        # MAIN AREA ---------------------------------------------------------
        body = QtWidgets.QFrame()
        body.setObjectName("body")
        self.bodyLayout = QtWidgets.QVBoxLayout(body)
        body_layout = self.bodyLayout
        body_layout.setContentsMargins(
            theme_int("layout.content_horizontal", 28),
            theme_int("layout.content_vertical", 20),
            theme_int("layout.content_horizontal", 28),
            theme_int("layout.content_vertical", 20),
        )
        body_layout.setSpacing(theme_int("layout.content_gap", 16))

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)
        title = QtWidgets.QLabel("Q3ELITE  /  LAUNCHER")
        title.setObjectName("topMotto")
        top.addWidget(title)
        top.addStretch(1)

        self.windowControls = QtWidgets.QFrame(self.shell)
        self.windowControls.setObjectName("windowControls")
        wc = QtWidgets.QHBoxLayout(self.windowControls)
        wc.setContentsMargins(8, 0, 0, 0); wc.setSpacing(0)
        self.launcherVersion = QtWidgets.QLabel(f"Launcher v{read_launcher_metadata().get('version', '—')}")
        self.launcherVersion.setObjectName("versionLabel"); wc.addWidget(self.launcherVersion); wc.addSpacing(8)
        ctl_w = theme_int("window_controls.button_width", 44); ctl_h = theme_int("window_controls.button_height", 34); ctl_icon = theme_int("window_controls.icon_size", 17)
        self.minButton = MaterialPushButton("—"); self.minButton.setObjectName("windowButton"); self.minButton.setFixedSize(ctl_w,ctl_h); self.minButton.setToolTip("Minimize"); self.minButton.clicked.connect(self.minimize_launcher); wc.addWidget(self.minButton)
        self.maxButton = MaterialPushButton(""); self.maxButton.setObjectName("windowButton"); self.maxButton.setFixedSize(ctl_w,ctl_h); self.maxButton.setToolTip("Maximize / Restore  •  F11 Fullscreen")
        if qta is not None:
            try: self.maxButton.setIcon(qta.icon("ph.square", color=theme_value("colors.text.muted", "#868A90"))); self.maxButton.setIconSize(QtCore.QSize(ctl_icon,ctl_icon))
            except Exception: self.maxButton.setText("□")
        else: self.maxButton.setText("□")
        self.maxButton.clicked.connect(self.toggle_maximize); wc.addWidget(self.maxButton)
        self.closeButton = MaterialPushButton(""); self.closeButton.setObjectName("closeButton"); self.closeButton.setFixedSize(ctl_w,ctl_h)
        if qta is not None:
            try: self.closeButton.setIcon(qta.icon("fa5s.times", color=theme_value("colors.text.muted", "#868A90"))); self.closeButton.setIconSize(QtCore.QSize(ctl_icon,ctl_icon))
            except Exception: self.closeButton.setText("×")
        else: self.closeButton.setText("×")
        self.closeButton.clicked.connect(self.close); wc.addWidget(self.closeButton)
        self.windowControls.adjustSize(); self.windowControls.move(max(0, self.shell.width() - self.windowControls.width()), 0); self.windowControls.raise_()
        body_layout.addLayout(top)

        self.pages = QtWidgets.QStackedWidget()
        self.pages.setObjectName("pages")

        # Build only the pages required by startup synchronously. QWidget
        # creation must remain on Qt's GUI thread, but there is no reason to
        # construct every heavy section before HOME can render. Deferred pages
        # are warmed one-by-one after the first frame while their data/network
        # preparation runs on a small worker pool.
        self._page_attr_names = {
            "home": "homePage",
            "addons": "addonsPage",
            "statistics": "statisticsPage",
            "servers": "serversPage",
            "maps": "mapsPage",
            "matchmaking": "matchmakingPage",
            "screenshots": "screenshotsPage",
            "demos": "demosPage",
            "settings": "settingsPage",
            "changelog": "changelogPage",
        }
        self._page_builders = {
            "statistics": self._build_statistics,
            "servers": self._build_servers,
            "maps": self._build_maps,
            "matchmaking": self._build_matchmaking,
            "screenshots": self._build_screenshots,
            "demos": self._build_demos,
            "changelog": self._build_changelog,
        }
        self._built_pages = {"home", "addons", "settings", "changelog"}
        self._building_pages = set()

        self.homePage = self._build_home()
        self.addonsPage = self._build_addons()
        self.settingsPage = self._build_settings()
        # Changelog is cheap now: it contains only Qt widgets/snapshot placeholders.
        # Build it before the first navigation so clicking CHANGELOG never swaps a
        # lazy placeholder for a real page. Chromium remains detached/off-screen.
        self.changelogPage = self._build_changelog()

        lazy_enabled = theme_bool("performance.lazy_pages", True)
        if lazy_enabled:
            self.statisticsPage = self._make_lazy_page("statistics", "STATISTICS")
            self.serversPage = self._make_lazy_page("servers", "SERVERS")
            self.mapsPage = self._make_lazy_page("maps", "MAPS")
            self.matchmakingPage = self._make_lazy_page("matchmaking", "MATCHMAKING")
            self.screenshotsPage = self._make_lazy_page("screenshots", "SCREENSHOTS")
            self.demosPage = self._make_lazy_page("demos", "DEMOS")
        else:
            for page_name, builder in self._page_builders.items():
                if page_name in self._built_pages:
                    continue
                page_widget = builder()
                setattr(self, self._page_attr_names[page_name], page_widget)
                self._built_pages.add(page_name)

        for page in (self.homePage, self.addonsPage, self.statisticsPage, self.serversPage, self.mapsPage, self.matchmakingPage, self.screenshotsPage, self.demosPage, self.settingsPage, self.changelogPage):
            self.pages.addWidget(page)
        body_layout.addWidget(self.pages, 1)
        root.addWidget(body, 1)

        self.sizeGrip = QtWidgets.QSizeGrip(self)
        self.sizeGrip.setFixedSize(18, 18)
        self.sizeGrip.raise_()

        self._fullscreen_was_maximized = False
        self.fullscreenShortcut = QtGui.QShortcut(QtGui.QKeySequence("F11"), self)
        self.fullscreenShortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
        self.fullscreenShortcut.activated.connect(self.toggle_fullscreen)

        self.screenshotWheelFilter = ScreenshotWheelFilter(self)
        QtWidgets.QApplication.instance().installEventFilter(self.screenshotWheelFilter)

        self.mediaFindShortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+F"), self)
        self.mediaFindShortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.mediaFindShortcut.activated.connect(self._focus_current_media_search)

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
        QtCore.QTimer.singleShot(120, self._apply_native_backdrop)

        self._preload_executor = ThreadPoolExecutor(
            max_workers=max(1, min(4, theme_int("performance.preload_workers", 3))),
            thread_name_prefix="Q3ElitePreload",
        )
        self._preload_futures = []
        self._telegram_fetch_lock = threading.Lock()
        self._telegram_fetch_inflight = set()
        QtCore.QTimer.singleShot(40, self._start_background_preload)
        if (theme_bool("performance.lazy_pages", True)
                and theme_bool("performance.warmup_pages", False)):
            QtCore.QTimer.singleShot(
                max(0, theme_int("performance.warmup_delay_ms", 1200)),
                self._start_page_warmup,
            )

    def _make_lazy_page(self, page_name, title):
        page = QtWidgets.QWidget()
        page.setObjectName(f"{page_name}LazyPage")
        page.setProperty("q3_lazy_page", True)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        label = QtWidgets.QLabel(f"Preparing {title}…")
        label.setObjectName("muted")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _ensure_page_built(self, page_name):
        if page_name in self._built_pages:
            return getattr(self, self._page_attr_names[page_name])
        if page_name in self._building_pages:
            return getattr(self, self._page_attr_names[page_name])
        builder = self._page_builders.get(page_name)
        if builder is None:
            return getattr(self, self._page_attr_names[page_name])

        self._building_pages.add(page_name)
        attr = self._page_attr_names[page_name]
        placeholder = getattr(self, attr)
        try:
            widget = builder()

            # V4: keep the same QStackedWidget page object and mount the real
            # section inside the lazy host. Replacing the current stack widget
            # causes a second full-window expose/composition pass on Windows;
            # with a translucent frameless shell this can look exactly like the
            # launcher briefly restarted. In-place mounting confines the first
            # load repaint to the content area.
            mount_in_place = (
                bool(placeholder.property("q3_lazy_page"))
                and theme_bool("performance.mount_lazy_pages_in_place", True)
            )
            if mount_in_place:
                lay = placeholder.layout()
                if lay is None:
                    lay = QtWidgets.QVBoxLayout(placeholder)
                while lay.count():
                    item = lay.takeAt(0)
                    child = item.widget()
                    if child is not None:
                        child.deleteLater()
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(0)
                widget.setParent(placeholder)
                lay.addWidget(widget)
                placeholder._q3_real_page = widget
                if hasattr(widget, "_changelog_cards"):
                    placeholder._changelog_cards = widget._changelog_cards
                self._built_pages.add(page_name)
                if ui_theme is not None and theme_bool("performance.polish_lazy_pages", False):
                    ui_theme.polish_widget_tree(widget)
                _install_obsidian_scrollbars(widget)
                placeholder.updateGeometry()
                placeholder.update()
                return placeholder

            old_index = self.pages.indexOf(placeholder)
            was_current = self.pages.currentWidget() is placeholder
            self.pages.insertWidget(old_index, widget)
            self.pages.removeWidget(placeholder)
            placeholder.deleteLater()
            setattr(self, attr, widget)
            self._built_pages.add(page_name)
            if ui_theme is not None and theme_bool("performance.polish_lazy_pages", False):
                ui_theme.polish_widget_tree(widget)
            _install_obsidian_scrollbars(widget)
            if was_current:
                self.pages.setCurrentWidget(widget)
            return widget
        finally:
            self._building_pages.discard(page_name)

    def _start_page_warmup(self):
        # Prioritize sections that users are most likely to enter directly
        # after HOME. Changelog is last because its Telegram data is prefetched
        # in parallel and can then build from cache without blocking Chromium/UI.
        self._page_warmup_queue = [
            "servers", "maps", "screenshots", "demos",
            "statistics", "matchmaking", "changelog",
        ]
        self._warm_next_page()

    def _warm_next_page(self):
        while getattr(self, "_page_warmup_queue", None):
            page_name = self._page_warmup_queue.pop(0)
            if page_name in self._built_pages:
                continue
            try:
                self._ensure_page_built(page_name)
            except Exception as error:
                print(f"[ui] Deferred page build failed for {page_name}: {error}")
                traceback.print_exc()
            break
        if getattr(self, "_page_warmup_queue", None):
            QtCore.QTimer.singleShot(
                max(25, theme_int("performance.warmup_step_ms", 90)),
                self._warm_next_page,
            )

    def _queue_telegram_fetch(self, url):
        """Queue one Telegram parse on the existing background pool.

        Duplicate URLs are coalesced and no Qt widget is ever touched from the
        worker. Changelog cards simply poll the small native cache.
        """
        url = str(url or "").strip()
        if not url or not getattr(self, "_preload_executor", None):
            return
        if _telegram_cached_post(url) is not None:
            return
        lock = getattr(self, "_telegram_fetch_lock", None)
        inflight = getattr(self, "_telegram_fetch_inflight", None)
        if lock is None or inflight is None:
            return
        with lock:
            if url in inflight:
                return
            inflight.add(url)

        def work():
            try:
                return _telegram_public_post_data(url)
            finally:
                with lock:
                    inflight.discard(url)

        try:
            future = self._preload_executor.submit(work)
            self._preload_futures.append(future)
        except RuntimeError:
            with lock:
                inflight.discard(url)

    def _preload_changelog_sources(self):
        """Fetch only the newest Telegram-backed cards on a worker thread.

        This is intentionally low-volume: Changelog is secondary to launcher
        interaction, so it must never compete with HOME/Maps/scroll rendering.
        """
        try:
            entries = [x for x in read_changelog_entries() if isinstance(x, dict)]
            from datetime import datetime

            def _key(item):
                value = str(item.get("date", item.get("release_date", item.get("published_at", ""))))
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
                    try:
                        return datetime.strptime(value[:10], fmt)
                    except ValueError:
                        pass
                return datetime.min

            entries.sort(key=_key, reverse=True)
            limit = max(1, min(6, theme_int("performance.changelog_latest_posts", 3)))
            fetched = 0
            seen = set()
            for release in entries:
                if fetched >= limit:
                    break
                source = str(release.get("telegram", release.get("text_source", "")) or "").strip()
                if not source or source in seen:
                    continue
                seen.add(source)
                # This function itself already runs off the GUI thread, but
                # use the shared queue so section-open retries and startup warmup
                # cannot duplicate the same URL.
                self._queue_telegram_fetch(source)
                fetched += 1
        except Exception as error:
            print(f"[preload] Changelog warm-up failed: {error}")

    def _preload_media_metadata(self):
        # Warm the Windows filesystem cache on worker threads. No QWidget or
        # QPixmap objects are touched here; those must stay on the GUI thread.
        folders = (
            GAME_ROOT / "baseq3" / "mods" / "osp" / "screenshots",
            GAME_ROOT / "Q3Elite" / "Screenshots",
            GAME_ROOT / "baseq3" / "mods" / "osp" / "demos",
        )
        try:
            for folder in folders:
                if not folder.is_dir():
                    continue
                for path in folder.iterdir():
                    if path.is_file():
                        try:
                            path.stat()
                        except OSError:
                            pass
        except OSError:
            pass

    def _submit_changelog_preload(self):
        executor = getattr(self, "_preload_executor", None)
        if executor is None:
            return
        try:
            future = executor.submit(self._preload_changelog_sources)
            self._preload_futures.append(future)
        except RuntimeError:
            pass

    def _start_background_preload(self):
        executor = getattr(self, "_preload_executor", None)
        if executor is None:
            return
        try:
            self._map_catalog_future = executor.submit(build_local_map_catalog)
            self._preload_futures = [
                executor.submit(self._preload_media_metadata),
                self._map_catalog_future,
            ]
            # Telegram is deliberately low priority. Give the launcher its first
            # interactive frames and the map/media warm-up slots first.
            if not (TELEGRAM_WEBENGINE_AVAILABLE
                    and theme_bool("performance.changelog_auto_webengine", True)):
                # Native HTML parsing is fallback-only. Never fetch the same
                # newest posts once natively and again through WebEngine.
                QtCore.QTimer.singleShot(
                    max(250, theme_int("performance.changelog_preload_delay_ms", 1800)),
                    self._submit_changelog_preload,
                )
        except RuntimeError:
            self._preload_futures = []

    def _nav_button(self, text, page, icon_name=None):
        b = GlowButton("")
        preferred = None
        if qta is not None and icon_name:
            preferred = {
                "fa5s.home": "ph.house",
                "fa5s.puzzle-piece": "ph.puzzle-piece",
                "fa5s.chart-bar": "ph.chart-bar",
                "fa5s.server": "ph.hard-drives",
                "fa5s.map": "ph.map-trifold",
                "fa5s.bell": "ph.users-three",
                "fa5s.image": "ph.image-square",
                "fa5s.film": "ph.film-strip",
                "fa5s.cog": "ph.gear",
                "fa5s.scroll": "ph.scroll",
            }.get(icon_name, icon_name)
            normal = theme_value("icons.nav_idle_color", "#F0F1F2")
            for candidate in (preferred, icon_name):
                try:
                    b.setIcon(qta.icon(candidate, color=normal))
                    break
                except Exception:
                    continue
            b.setIconSize(QtCore.QSize(
                theme_int("icons.size_navigation", 21),
                theme_int("icons.size_navigation", 21),
            ))
        b._q3_icon_preferred = preferred
        b._q3_icon_fallback = icon_name
        b.setObjectName("navButton")
        b.setToolTip(text.title())
        b.setAccessibleName(text)
        b.setCheckable(True)
        b.setProperty("page", page)
        b.setFixedSize(theme_int("layout.nav_button", 54), theme_int("layout.nav_button", 54))
        b.clicked.connect(lambda checked=False, p=page: self.show_page(p))
        return b

    def _nav_indicator_rect(self, button):
        inset = max(0, theme_int("layout.nav_indicator_inset", 5))
        top_left = button.mapTo(self.sidebar, QtCore.QPoint(0, 0))
        rect = QtCore.QRect(top_left, button.size())
        return rect.adjusted(inset, inset, -inset, -inset)

    def _move_nav_indicator(self, button, *, animate=True):
        if not hasattr(self, "navIndicator") or button is None:
            return
        if not theme_bool("layout.sidebar.use_indicator", False):
            self.navIndicator.hide()
            return
        target = self._nav_indicator_rect(button)
        self.navIndicator.show()
        self.navIndicator.lower()
        if not animate:
            self.navIndicator.setGeometry(target)
            return
        # V7 selection no longer flies through the whole sidebar. The plate
        # appears on the destination and expands quickly into place, closer to
        # Noctra's compact selected-icon response.
        self._navIndicatorAnim.stop()
        shrink = min(7, max(2, target.width() // 8))
        start = target.adjusted(shrink, shrink, -shrink, -shrink)
        self.navIndicator.setGeometry(start)
        self._navIndicatorAnim.setDuration(theme_int("motion.nav", 88))
        self._navIndicatorAnim.setStartValue(start)
        self._navIndicatorAnim.setEndValue(target)
        self._navIndicatorAnim.start()

    def _refresh_nav_icons(self):
        buttons = (
            self.homeNav, self.addonsNav, self.statisticsNav, self.serversNav,
            self.mapsNav, self.matchmakingNav, self.screenshotsNav, self.demosNav,
            self.settingsNav, self.changelogNav,
        )
        normal = theme_value("icons.nav_idle_color", "#F0F1F2")
        active = theme_value("icons.nav_active_color", "#FFFFFF")
        size = theme_int("icons.size_navigation", 21)
        for button in buttons:
            if qta is not None:
                for candidate in (getattr(button, "_q3_icon_preferred", None), getattr(button, "_q3_icon_fallback", None)):
                    if not candidate:
                        continue
                    try:
                        button.setIcon(qta.icon(candidate, color=active if button.isChecked() else normal))
                        button.setIconSize(QtCore.QSize(size, size))
                        break
                    except Exception:
                        continue
            if hasattr(button, "refresh_active_glow"):
                button.refresh_active_glow()

    def _relayout_window_controls(self):
        """Recompute title-bar geometry after a live theme/font switch."""
        if not hasattr(self, "windowControls"):
            return
        try:
            ctl_w = theme_int("window_controls.button_width", 44)
            ctl_h = theme_int("window_controls.button_height", 34)
            for button in (getattr(self, "minButton", None), getattr(self, "maxButton", None), getattr(self, "closeButton", None)):
                if button is not None:
                    button.setFixedSize(ctl_w, ctl_h)
            if hasattr(self, "launcherVersion"):
                self.launcherVersion.setMinimumWidth(0)
                self.launcherVersion.adjustSize()
                self.launcherVersion.updateGeometry()
            layout = self.windowControls.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            self.windowControls.adjustSize()
            self.windowControls.updateGeometry()
            self.windowControls.move(max(0, self.shell.width() - self.windowControls.width()), 0)
            self.windowControls.raise_()
            self._sync_window_controls()
        except RuntimeError:
            pass

    def apply_runtime_theme(self):
        _clear_obsidian_render_cache()
        try:
            OBSIDIAN_MATERIAL.reset_caches()
        except Exception:
            pass
        self.setMinimumSize(
            theme_int("layout.window.min_width", 1366),
            theme_int("layout.window.min_height", 768),
        )
        max_w = theme_int("layout.window.max_width", 0)
        max_h = theme_int("layout.window.max_height", 0)
        self.setMaximumSize(
            max_w if max_w > 0 else QtWidgets.QWIDGETSIZE_MAX,
            max_h if max_h > 0 else QtWidgets.QWIDGETSIZE_MAX,
        )
        self.sidebar.setFixedWidth(theme_int("layout.sidebar_width", 112))
        rail_pad = theme_int("layout.sidebar_padding", 20)
        self.sideLayout.setContentsMargins(rail_pad, 16, rail_pad, 14)
        content_h = theme_int("layout.content_horizontal", 28)
        content_v = theme_int("layout.content_vertical", 20)
        self.bodyLayout.setContentsMargins(content_h, content_v, content_h, content_v)
        self.bodyLayout.setSpacing(theme_int("layout.content_gap", 16))
        nav_size = theme_int("layout.nav_button", 56)
        for button in (
            self.homeNav, self.addonsNav, self.statisticsNav, self.serversNav,
            self.mapsNav, self.matchmakingNav, self.screenshotsNav, self.demosNav,
            self.settingsNav, self.changelogNav,
        ):
            button.setFixedSize(nav_size, nav_size)
        self.railStatus.setStyleSheet(
            f"color:{theme_value('colors.semantic.success', '#89C541')}; background:transparent;"
        )
        self._refresh_nav_icons()
        for glow_button in self.findChildren(GlowButton):
            try:
                glow_button.refresh_theme_effect()
            except RuntimeError:
                pass
        for themed_button in self.findChildren(ThemedIconButton):
            try:
                themed_button._refresh_theme_icon()
            except RuntimeError:
                pass
        for card in getattr(self, "homeQuickCards", []):
            try:
                card._refresh_icon()
            except Exception:
                pass
        if qta is not None:
            hero_nav_color = theme_value("home.hero_nav_icon_color", theme_value("colors.text.secondary", "#C3C5C8"))
            for button, name in ((getattr(self, "heroPrev", None), "ph.caret-left"), (getattr(self, "heroNext", None), "ph.caret-right")):
                if button is not None:
                    try:
                        button.setIcon(qta.icon(name, color=hero_nav_color))
                        button.setIconSize(QtCore.QSize(theme_int("home.hero_nav_icon_size", 16), theme_int("home.hero_nav_icon_size", 16)))
                    except Exception:
                        pass
            icon_color = theme_value("icons.tile_icon_color", theme_value("colors.text.primary", "#F3F3F3"))
            for label in self.findChildren(QtWidgets.QLabel):
                try:
                    if isinstance(label, SilverIconTile):
                        label.refresh_theme_icon()
                        continue
                    icon_name = str(label.property("theme_icon_name") or "")
                    if icon_name:
                        label.setPixmap(qta.icon(icon_name, color=icon_color).pixmap(20, 20))
                except (RuntimeError, TypeError, Exception):
                    pass
            action_color = theme_value("icons.action_icon_color", theme_value("colors.text.secondary", "#C3C5C8"))
            changelog_icons = {"WATCH VIDEO":"fa5s.play", "LIVE POST":"fa5s.bolt", "RELOAD":"fa5s.sync-alt", "OPEN SOURCE":"fa5s.external-link-alt"}
            for button in self.findChildren(QtWidgets.QPushButton, "changelogLinkButton"):
                try:
                    icon_name = changelog_icons.get(button.text().strip().upper())
                    if icon_name:
                        button.setIcon(qta.icon(icon_name, color=action_color))
                except Exception:
                    pass
            for card in self.findChildren(ChangelogCard):
                try:
                    card._update_toggle_icon()
                except (RuntimeError, TypeError):
                    pass
        active = next((b for b in (
            self.homeNav, self.addonsNav, self.statisticsNav, self.serversNav,
            self.mapsNav, self.matchmakingNav, self.screenshotsNav, self.demosNav,
            self.settingsNav, self.changelogNav,
        ) if b.isChecked()), self.homeNav)
        self._move_nav_indicator(active, animate=False)
        self.shell.reload_theme_visuals()
        outer = 0 if (self.isMaximized() or self.isFullScreen()) else theme_int("layout.window.outer_margin", 13)
        self.shell.setGeometry(outer, outer, max(1, self.width() - outer * 2), max(1, self.height() - outer * 2))
        self._apply_window_mask()
        self._apply_native_backdrop()
        if hasattr(self, "themeHubCards"):
            self._refresh_theme_hub()
        # Server presentation is theme-specific in V9: Noctra uses cinematic
        # full-card art while White Paper uses a dedicated levelshot panel.
        expected_server_presentation = str(theme_value("servers.presentation", "cinematic") or "cinematic").casefold()
        cards = getattr(self, "serverCards", [])
        if cards and any(getattr(card, "presentation", "cinematic") != expected_server_presentation for card in cards):
            self._reload_server_cards()
            QtCore.QTimer.singleShot(0, self.refresh_servers)
        if ui_theme is not None:
            ui_theme.polish_widget_tree(self)
        # Existing material cards may survive a theme hot reload; repaint them
        # so live texture opacity/path edits become visible immediately.
        for material_card in self.findChildren(SurfaceCard):
            try:
                material_card.update()
            except RuntimeError:
                pass
        _install_obsidian_scrollbars(self)
        self._refresh_heavy_scroll_surfaces()
        try:
            # Telegram Chromium renderers are intentionally detached top-level
            # windows, so findChildren() cannot see them. Re-theme both the Qt
            # snapshot widgets and their retained off-screen renderers explicitly.
            for telegram_view in self.findChildren(TelegramSnapshotView):
                telegram_view.apply_theme_palette()
            for renderer in list(getattr(self, "_telegramDetachedRenderers", []) or []):
                try:
                    renderer.apply_theme_palette()
                except RuntimeError:
                    pass
        except (RuntimeError, TypeError):
            pass
        # QSS + application-font metrics settle asynchronously. Recalculate the
        # version/control strip now and once more on the next event turn so
        # Paper -> Steel cannot leave "Launcher vX" clipped or shifted.
        self._relayout_window_controls()
        QtCore.QTimer.singleShot(0, self._relayout_window_controls)
        self.update()

    def _card(self, name="card"):
        f = SurfaceCard()
        f.setObjectName(name)
        return f

    def _build_home(self):
        """Layer 2 home: one strong launch composition, not a dashboard of boxes."""
        page = QtWidgets.QWidget()
        page.setObjectName("homePage")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        stage = QtWidgets.QHBoxLayout()
        stage.setSpacing(14)

        # ------------------------------------------------------------------
        # HERO / VISUAL IDENTITY
        # ------------------------------------------------------------------
        hero = self._card("homeHeroCard")
        hero.setMinimumHeight(theme_int("home.hero_min_height", 310))
        hero_l = QtWidgets.QVBoxLayout(hero)
        hero_l.setContentsMargins(0, 0, 0, 0)
        hero_l.setSpacing(0)

        self.heroImage = QtWidgets.QLabel("Loading Q3Elite artwork…")
        self.heroImage.setObjectName("heroImage")
        self.heroImage.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.heroImage.setMinimumHeight(280)
        # Ignore QLabel pixmap sizeHint: remote Imgur art must never resize the
        # Home composition while a higher-resolution image finishes loading.
        self.heroImage.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        self.heroImage.setScaledContents(False)
        hero_l.addWidget(self.heroImage, 1)

        hero_controls = QtWidgets.QFrame()
        hero_controls.setObjectName("heroOverlay")
        hc = QtWidgets.QHBoxLayout(hero_controls)
        hc.setContentsMargins(20, 13, 14, 13)
        hc.setSpacing(10)

        hero_text = QtWidgets.QVBoxLayout()
        hero_text.setSpacing(2)
        kicker = QtWidgets.QLabel("QUAKE III ELITE  /  CINEMATIC BUILD")
        kicker.setObjectName("heroKicker")
        hero_title = QtWidgets.QLabel("RELOADED FOR A NEW ERA")
        hero_title.setObjectName("heroTitle")
        hero_text.addWidget(kicker)
        hero_text.addWidget(hero_title)
        hc.addLayout(hero_text, 1)

        self.heroCounter = QtWidgets.QLabel("01 / 16")
        self.heroCounter.setObjectName("heroCounter")
        hc.addWidget(self.heroCounter)

        self.heroPrev = ThemedIconButton("ph.caret-left", "home.hero_nav_icon_color", "home.hero_nav_icon_hover_color")
        self.heroPrev.setObjectName("sliderArrow")
        self.heroPrev.setFixedSize(38, 38)
        self.heroNext = ThemedIconButton("ph.caret-right", "home.hero_nav_icon_color", "home.hero_nav_icon_hover_color")
        self.heroNext.setObjectName("sliderArrow")
        self.heroNext.setFixedSize(38, 38)
        if qta is not None:
            self.heroPrev.setIconSize(QtCore.QSize(15, 15))
            self.heroNext.setIconSize(QtCore.QSize(15, 15))
        else:
            self.heroPrev.setText("‹")
            self.heroNext.setText("›")
        self.heroPrev.clicked.connect(lambda: self.change_hero_image(-1))
        self.heroNext.clicked.connect(lambda: self.change_hero_image(1))
        hc.addWidget(self.heroPrev)
        hc.addWidget(self.heroNext)
        hero_l.addWidget(hero_controls)
        stage.addWidget(hero, 5)

        # ------------------------------------------------------------------
        # LAUNCH / DOWNLOAD CONTROL CENTER
        # ------------------------------------------------------------------
        self.statusCard = self._card("homeLaunchPanel")
        self.statusCard.setMinimumWidth(theme_int("home.launch_panel_width", 380))
        panel = QtWidgets.QVBoxLayout(self.statusCard)
        panel.setContentsMargins(22, 20, 22, 20)
        panel.setSpacing(10)

        eyebrow_row = QtWidgets.QHBoxLayout()
        eyebrow = QtWidgets.QLabel("READY STATE")
        eyebrow.setObjectName("homeEyebrow")
        eyebrow_row.addWidget(eyebrow)
        eyebrow_row.addStretch(1)
        self.homeStatusDot = QtWidgets.QLabel("●")
        self.homeStatusDot.setObjectName("homeStatusDot")
        eyebrow_row.addWidget(self.homeStatusDot)
        panel.addLayout(eyebrow_row)

        self.statusTitle = MetallicLabel("Checking Q3Elite…")
        self.statusTitle.setObjectName("homeStatusTitle")
        self.statusTitle.setWordWrap(True)
        panel.addWidget(self.statusTitle)

        self.statusDetail = QtWidgets.QLabel("Connecting to update services.")
        self.statusDetail.setObjectName("homeStatusDetail")
        self.statusDetail.setWordWrap(True)
        panel.addWidget(self.statusDetail)

        meta = QtWidgets.QHBoxLayout()
        meta.setSpacing(8)
        version_chip = SurfaceCard()
        version_chip.setObjectName("homeMetaChip")
        version_chip.setProperty("materialRadius", theme_int("radius.md", 10))
        vl = QtWidgets.QVBoxLayout(version_chip); vl.setContentsMargins(11, 7, 11, 7); vl.setSpacing(1)
        vcap = QtWidgets.QLabel("Q3ELITE"); vcap.setObjectName("homeMetaCaption"); vl.addWidget(vcap)
        self.q3VersionValue = MetallicLabel(read_local_q3elite_version()); self.q3VersionValue.setObjectName("homeMetaValue"); vl.addWidget(self.q3VersionValue)
        meta.addWidget(version_chip, 1)

        install_chip = SurfaceCard()
        install_chip.setObjectName("homeMetaChip")
        install_chip.setProperty("materialRadius", theme_int("radius.md", 10))
        il = QtWidgets.QVBoxLayout(install_chip); il.setContentsMargins(11, 7, 11, 7); il.setSpacing(1)
        icap = QtWidgets.QLabel("INSTALLATION"); icap.setObjectName("homeMetaCaption"); il.addWidget(icap)
        self.installedValue = MetallicLabel("Checking"); self.installedValue.setObjectName("homeMetaValue"); il.addWidget(self.installedValue)
        meta.addWidget(install_chip, 1)
        panel.addLayout(meta)

        # Fresh-install choices remain functional but become part of the launch panel.
        self.firstInstallCard = SurfaceCard()
        self.firstInstallCard.setObjectName("homeInstallOptions")
        self.firstInstallCard.setProperty("materialRadius", theme_int("radius.md", 10))
        fic = QtWidgets.QVBoxLayout(self.firstInstallCard)
        fic.setContentsMargins(12, 10, 12, 10)
        fic.setSpacing(6)
        fit = QtWidgets.QLabel("OPTIONAL COMPONENTS")
        fit.setObjectName("homeMetaCaption")
        fic.addWidget(fit)
        options = QtWidgets.QHBoxLayout()
        self.firstInstallMapsBox = MaterialCheckBox("External Maps")
        self.firstInstallMusicBox = MaterialCheckBox("Music Playlist")
        self.firstInstallMapsBox.setObjectName("compactCheck")
        self.firstInstallMusicBox.setObjectName("compactCheck")
        options.addWidget(self.firstInstallMapsBox)
        options.addWidget(self.firstInstallMusicBox)
        options.addStretch(1)
        fic.addLayout(options)
        self.firstInstallCard.setVisible(not q3elite_is_installed())
        panel.addWidget(self.firstInstallCard)

        panel.addStretch(1)

        self.playButton = GlowButton("CHECKING…")
        self.playButton.setObjectName("playButton")
        self.playButton.setMinimumHeight(70)
        panel.addWidget(self.playButton)

        progress_meta = QtWidgets.QHBoxLayout()
        self.downloadInfo = QtWidgets.QLabel("Preparing launcher…")
        self.downloadInfo.setObjectName("downloadInfo")
        progress_meta.addWidget(self.downloadInfo, 1)
        self.pauseButton = MaterialPushButton("PAUSE")
        self.pauseButton.setObjectName("homePauseButton")
        self.pauseButton.clicked.connect(toggle_download_pause)
        progress_meta.addWidget(self.pauseButton)
        panel.addLayout(progress_meta)

        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setObjectName("homeProgress")
        self.progressBar.setTextVisible(False)
        self.progressBar.setRange(0, 0)
        panel.addWidget(self.progressBar)

        tool_row = QtWidgets.QHBoxLayout()
        self.alertTitle = QtWidgets.QLabel("UPDATE")
        self.alertTitle.setObjectName("homeAlertLabel")
        tool_row.addWidget(self.alertTitle)
        self.alertText = QtWidgets.QLabel("No update requires your attention.")
        self.alertText.setObjectName("homeAlertText")
        self.alertText.setWordWrap(True)
        tool_row.addWidget(self.alertText, 1)
        self.refreshButton = MaterialPushButton("REFRESH")
        self.refreshButton.setObjectName("homeRefreshButton")
        self.refreshButton.clicked.connect(refresh_updates)
        tool_row.addWidget(self.refreshButton)
        panel.addLayout(tool_row)

        stage.addWidget(self.statusCard, 3)
        layout.addLayout(stage, 1)

        # ------------------------------------------------------------------
        # QUICK ACCESS — connects Home to actual launcher functionality.
        # ------------------------------------------------------------------
        quick = QtWidgets.QHBoxLayout()
        quick.setSpacing(10)
        quick_specs = (
            ("SERVERS", "Browse live arenas", "servers", "ph.hard-drives"),
            ("MAPS", "Local + online catalog", "maps", "ph.map-trifold"),
            ("STATISTICS", "Freekill.ru profile", "statistics", "ph.chart-bar"),
            ("ADDONS", "Maps, music & config", "addons", "ph.puzzle-piece"),
        )
        self.homeQuickCards = []
        for title, subtitle, page_name, icon_name in quick_specs:
            card = QuickLinkCard(title, subtitle, page_name, icon_name, page)
            card.clicked.connect(self.show_page)
            self.homeQuickCards.append(card)
            quick.addWidget(card, 1)
        layout.addLayout(quick)
        return page

    def _addon_row(self, title, description, checkbox, icon_name=None):
        card = self._card("addonCard")
        lay = QtWidgets.QHBoxLayout(card)
        lay.setContentsMargins(18, 15, 18, 15)
        lay.setSpacing(14)

        icon = SilverIconTile(icon_name or "", "addonIcon", 40, 20, card)
        lay.addWidget(icon)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        t = MetallicLabel(title); t.setObjectName("addonTitle")
        d = QtWidgets.QLabel(description); d.setObjectName("muted"); d.setWordWrap(True)
        text.addWidget(t)
        text.addWidget(d)
        lay.addLayout(text, 1)
        checkbox.setObjectName("addonToggle")
        lay.addWidget(checkbox, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        return card

    def _build_addons(self):
        page = QtWidgets.QWidget()
        page.setObjectName("addonsPage")
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        htext = QtWidgets.QVBoxLayout(); htext.setSpacing(2)
        title = MetallicLabel("ADDONS"); title.setObjectName("pageTitle"); htext.addWidget(title)
        sub = QtWidgets.QLabel("Optional Q3Elite content. Changes are staged until you apply them."); sub.setObjectName("muted"); htext.addWidget(sub)
        header.addLayout(htext, 1)
        self.addonMessage = QtWidgets.QLabel(""); self.addonMessage.setObjectName("message"); header.addWidget(self.addonMessage)
        lay.addLayout(header)

        basic = self._card("addonCoreCard")
        bl = QtWidgets.QHBoxLayout(basic); bl.setContentsMargins(18, 15, 18, 15); bl.setSpacing(14)
        core_icon = SilverIconTile("ph.cube", "addonCoreIcon", 44, 22, basic)
        bl.addWidget(core_icon)
        bt = QtWidgets.QVBoxLayout(); bt.setSpacing(2)
        x = MetallicLabel("Quake 3 Elite Basic"); x.setObjectName("addonTitle"); bt.addWidget(x)
        desc = QtWidgets.QLabel("Core launcher-managed game installation"); desc.setObjectName("muted"); bt.addWidget(desc)
        bl.addLayout(bt,1)
        installed = QtWidgets.QLabel("REQUIRED"); installed.setObjectName("installedBadge"); bl.addWidget(installed)
        lay.addWidget(basic)

        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
        self.mapsBox = ToggleSwitch()
        self.musicBox = ToggleSwitch()
        grid.addWidget(self._addon_row("External Maps", "Community map collection • cache retained for repairs", self.mapsBox, "ph.map-trifold"), 0, 0)
        grid.addWidget(self._addon_row("Music Playlist", "Extended soundtrack package • cache retained", self.musicBox, "ph.music-notes"), 0, 1)

        autoexec_card = self._card("addonCard")
        autoexec_l = QtWidgets.QHBoxLayout(autoexec_card); autoexec_l.setContentsMargins(18,15,18,15); autoexec_l.setSpacing(14)
        auto_icon = SilverIconTile("ph.gear", "addonIcon", 40, 20, autoexec_card)
        autoexec_l.addWidget(auto_icon)
        autoexec_t = QtWidgets.QVBoxLayout(); autoexec_t.setSpacing(2)
        autoexec_title = MetallicLabel("Autoexec Update"); autoexec_title.setObjectName("addonTitle"); autoexec_t.addWidget(autoexec_title)
        auto_desc = QtWidgets.QLabel("Synchronize the distributed autoexec.cfg once"); auto_desc.setObjectName("muted"); autoexec_t.addWidget(auto_desc)
        autoexec_l.addLayout(autoexec_t,1)
        self.autoexecUpdateButton = GlowButton("SYNC AUTOEXEC"); self.autoexecUpdateButton.setObjectName("secondaryButton")
        self.autoexecUpdateButton.clicked.connect(lambda: start_component_action("update-autoexec")); self.autoexecUpdateButton.setEnabled(q3elite_is_installed())
        autoexec_l.addWidget(self.autoexecUpdateButton)
        grid.addWidget(autoexec_card,1,0,1,2)
        grid.setColumnStretch(0,1); grid.setColumnStretch(1,1)
        lay.addLayout(grid)
        lay.addStretch(1)

        footer = QtWidgets.QFrame(); footer.setObjectName("sectionActionBar")
        fl = QtWidgets.QHBoxLayout(footer); fl.setContentsMargins(14,10,14,10)
        staged = QtWidgets.QLabel("Install/remove choices are applied as one operation."); staged.setObjectName("muted"); fl.addWidget(staged,1)
        cancel = MaterialPushButton("RESET"); cancel.setObjectName("secondaryButton"); cancel.clicked.connect(refresh_component_gui); fl.addWidget(cancel)
        self.applyAddonsButton = GlowButton("APPLY CHANGES"); self.applyAddonsButton.setObjectName("applyButton")
        self.applyAddonsButton.clicked.connect(apply_component_changes); self.applyAddonsButton.setEnabled(q3elite_is_installed()); fl.addWidget(self.applyAddonsButton)
        lay.addWidget(footer)
        return page

    def _build_theme_hub(self):
        hub = self._card("settingsCard")
        hub.setObjectName("settingsCard")
        layout = QtWidgets.QVBoxLayout(hub)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        head = QtWidgets.QHBoxLayout()
        title = MetallicLabel("THEME HUB")
        title.setObjectName("settingsCardTitle")
        head.addWidget(title)
        head.addStretch(1)
        active = QtWidgets.QLabel("")
        active.setObjectName("themeHubActive")
        self.themeHubActiveLabel = active
        head.addWidget(active)
        layout.addLayout(head)

        desc = QtWidgets.QLabel(
            "Switch the launcher material instantly. Presets share the same layout and Figtree typography; "
            "only surfaces, reflections, backdrop and accent behavior change."
        )
        desc.setObjectName("settingsHint")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.themeHubCards = {}
        themes = ui_theme.available_themes() if ui_theme is not None else []
        preferred_order = {
            "Steel": 0,
            "Paper": 1,
        }
        themes.sort(key=lambda item: preferred_order.get(item.get("id"), 99))
        for index, preset in enumerate(themes[:2]):
            theme_id = str(preset.get("id") or "")
            card = SurfaceCard()
            card.setObjectName("themeHubCard")
            card.setProperty("active", False)
            cl = QtWidgets.QVBoxLayout(card)
            cl.setContentsMargins(12, 12, 12, 12)
            cl.setSpacing(7)

            preview = dict(preset.get("preview") or {})
            a = str(preview.get("a") or "#050505")
            b = str(preview.get("b") or "#151515")
            accent = str(preview.get("accent") or "#72D3E6")
            swatch = QtWidgets.QFrame()
            swatch.setObjectName("themeSwatch")
            swatch.setFixedHeight(42)
            swatch.setStyleSheet(
                "QFrame#themeSwatch {"
                f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {a},stop:0.70 {b},stop:0.71 {accent},stop:1 {accent});"
                "border:1px solid rgba(255,255,255,42);border-radius:8px;}"
            )
            cl.addWidget(swatch)

            name = QtWidgets.QLabel(str(preset.get("name") or theme_id))
            name.setObjectName("themeHubTitle")
            cl.addWidget(name)
            detail = QtWidgets.QLabel(str(preset.get("description") or ""))
            detail.setObjectName("themeHubDescription")
            detail.setWordWrap(True)
            detail.setMinimumHeight(34)
            cl.addWidget(detail)
            apply_btn = MaterialPushButton("APPLY")
            apply_btn.setObjectName("secondaryButton")
            apply_btn.clicked.connect(lambda _checked=False, tid=theme_id: self._apply_theme_from_hub(tid))
            cl.addWidget(apply_btn)
            grid.addWidget(card, index // 2, index % 2)
            self.themeHubCards[theme_id] = (card, apply_btn)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self._refresh_theme_hub()
        return hub

    def _apply_theme_from_hub(self, theme_id):
        if ui_theme is None:
            return
        try:
            ui_theme.set_theme(str(theme_id), persist=True)
        except Exception as error:
            self.qerror(f"Could not apply theme: {error}")

    def _refresh_theme_hub(self):
        if not hasattr(self, "themeHubCards"):
            return
        active_id = ui_theme.theme_name if ui_theme is not None else ""
        for theme_id, (card, button) in self.themeHubCards.items():
            is_active = theme_id == active_id
            card.setProperty("active", is_active)
            card.style().unpolish(card); card.style().polish(card)
            button.setText("ACTIVE" if is_active else "APPLY")
            button.setEnabled(not is_active)
        if hasattr(self, "themeHubActiveLabel"):
            current_name = next((x.get("name") for x in (ui_theme.available_themes() if ui_theme else []) if x.get("id") == active_id), active_id)
            self.themeHubActiveLabel.setText(f"ACTIVE  •  {current_name}")

    def _optimize_heavy_scroll_area(self, scroll):
        """Reduce hover/material churn while a complex scroll area is moving.

        Install the hook regardless of the theme active at construction time so
        a live Paper -> Steel switch behaves exactly like a clean Steel launch.
        """
        if getattr(scroll, "_q3_scroll_perf_timer", None) is not None:
            return
        idle_ms = max(40, theme_int("performance.scroll_hover_idle_ms", 90))
        timer = QtCore.QTimer(scroll)
        timer.setSingleShot(True)

        def finish_scroll():
            try:
                self.setProperty("q3_scroll_active", False)
            except RuntimeError:
                pass

        def mark_scroll(*_args):
            if not OBSIDIAN_MATERIAL.enabled(ui_theme):
                self.setProperty("q3_scroll_active", False)
                return
            self.setProperty("q3_scroll_active", True)
            timer.start(max(40, theme_int("performance.scroll_hover_idle_ms", idle_ms)))

        timer.timeout.connect(finish_scroll)
        scroll.verticalScrollBar().valueChanged.connect(mark_scroll)
        scroll.horizontalScrollBar().valueChanged.connect(mark_scroll)
        scroll._q3_scroll_perf_timer = timer

    def _set_opaque_scroll_surface(self, widget, enabled):
        """Guarantee a real painted background for heavy Steel scroll regions.

        WA_OpaquePaintEvent alone only *promises* that a widget paints every
        pixel; it does not paint those pixels. Pair it with an explicit palette
        fill so the translucent top-level window can never expose desktop pixels.
        """
        if widget is None:
            return
        enabled = bool(enabled)
        try:
            # QWidget/QScrollArea stylesheet backgrounds are not guaranteed to
            # paint when WA_OpaquePaintEvent is merely asserted.  That produced
            # literal transparent pixels in the frameless translucent HWND.
            # Let Qt's styled-background path paint the pixels, while keeping
            # StaticContents so scrolling can still reuse backing-store strips.
            widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
            widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_StaticContents, enabled)
            widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, enabled)
            widget.setAutoFillBackground(enabled)
            if enabled:
                palette = QtGui.QPalette(widget.palette())
                fill = theme_color("colors.surface.deep", "#050506")
                fill.setAlpha(255)
                palette.setColor(QtGui.QPalette.ColorRole.Window, fill)
                palette.setColor(QtGui.QPalette.ColorRole.Base, fill)
                widget.setPalette(palette)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()
        except RuntimeError:
            pass

    def _refresh_heavy_scroll_surfaces(self):
        steel = OBSIDIAN_MATERIAL.enabled(ui_theme)
        settings_opaque = steel and theme_bool("performance.opaque_settings_viewport", True)
        matchmaking_opaque = steel and theme_bool("performance.opaque_matchmaking_viewport", True)

        for widget in (
            getattr(self, "settingsRealPage", None),
            getattr(self, "settingsViewport", None),
            getattr(self, "settingsScrollBody", None),
        ):
            self._set_opaque_scroll_surface(widget, settings_opaque)

        for widget in (
            getattr(self, "matchmakingRealPage", None),
            getattr(self, "matchmakingViewport", None),
            getattr(self, "matchmakingBody", None),
        ):
            self._set_opaque_scroll_surface(widget, matchmaking_opaque)

        if hasattr(self, "settingsScroll"):
            self._optimize_heavy_scroll_area(self.settingsScroll)
        if hasattr(self, "matchmakingScroll"):
            self._optimize_heavy_scroll_area(self.matchmakingScroll)

    def _build_settings(self):
        page = QtWidgets.QWidget()
        page.setObjectName("settingsPage")
        self.settingsRealPage = page
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QtWidgets.QScrollArea()
        self.settingsScroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_viewport = scroll.viewport()
        settings_viewport.setObjectName("settingsViewport")
        self.settingsViewport = settings_viewport

        body = QtWidgets.QWidget()
        body.setObjectName("settingsScrollBody")
        self.settingsScrollBody = body
        settings_opaque = OBSIDIAN_MATERIAL.enabled(ui_theme) and theme_bool("performance.opaque_settings_viewport", True)
        for surface in (page, settings_viewport, body):
            self._set_opaque_scroll_surface(surface, settings_opaque)
        lay = QtWidgets.QVBoxLayout(body)
        lay.setContentsMargins(4, 4, 20, 8)
        lay.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        htext = QtWidgets.QVBoxLayout(); htext.setSpacing(2)
        title = MetallicLabel("SETTINGS"); title.setObjectName("pageTitle"); htext.addWidget(title)
        subtitle = QtWidgets.QLabel("Launcher behavior, updates, integrations and maintenance."); subtitle.setObjectName("muted"); htext.addWidget(subtitle)
        header.addLayout(htext, 1)
        self.settingsMessage = QtWidgets.QLabel(""); self.settingsMessage.setObjectName("message"); header.addWidget(self.settingsMessage)
        lay.addLayout(header)
        lay.addWidget(self._build_theme_hub())

        def make_card(title_text, desc_text=""):
            card = self._card("settingsCard")
            card_l = QtWidgets.QVBoxLayout(card)
            card_l.setContentsMargins(18, 16, 18, 16)
            card_l.setSpacing(8)
            title_label = MetallicLabel(title_text); title_label.setObjectName("settingsCardTitle"); card_l.addWidget(title_label)
            if desc_text:
                desc = QtWidgets.QLabel(desc_text); desc.setObjectName("settingsHint"); desc.setWordWrap(True); card_l.addWidget(desc)
            return card, card_l

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        # Updates -----------------------------------------------------------
        updates, ul = make_card("UPDATES", "Choose which managed Q3Elite components update automatically.")
        self.autoQ3Box = ToggleSwitch("Automatically update Q3Elite")
        self.autoLauncherBox = ToggleSwitch("Automatically update Launcher")
        self.autoOspBox = ToggleSwitch("Automatically update OSP2-BE")
        for box in (self.autoQ3Box, self.autoLauncherBox, self.autoOspBox):
            box.setObjectName("settingsToggle")
            ul.addWidget(box)
        ul.addStretch(1)
        grid.addWidget(updates, 0, 0)

        # Startup -----------------------------------------------------------
        startup, sl = make_card("STARTUP & TRAY", "Keep Windows startup behavior independent from game launch settings.")
        self.startWindowsBox = ToggleSwitch("Start with Windows")
        self.startMinimizedBox = ToggleSwitch("Start minimized")
        self.startMinimizedBox.setToolTip("When started automatically with Windows, launch directly to the system tray.")
        self.startWindowsBox.toggled.connect(self.startMinimizedBox.setEnabled)
        self.trayBox = ToggleSwitch("Minimize to Windows system tray")
        for box in (self.startWindowsBox, self.startMinimizedBox, self.trayBox):
            box.setObjectName("settingsToggle")
            sl.addWidget(box)
        sl.addStretch(1)
        grid.addWidget(startup, 0, 1)

        # Visual / content --------------------------------------------------
        visual, vl = make_card("VISUAL & CONTENT", "Optional launcher/game presentation integrations.")
        self.vulkanLayerBox = ToggleSwitch("Enable ReShade for Vulkan")
        self.changelogMediaBox = ToggleSwitch("Show screenshots in changelog")
        for box in (self.vulkanLayerBox, self.changelogMediaBox):
            box.setObjectName("settingsToggle")
            vl.addWidget(box)
        vl.addStretch(1)
        grid.addWidget(visual, 1, 0)

        # Server behavior ---------------------------------------------------
        servers, svl = make_card("SERVER MONITOR", "Reduce background work and startup noise when preferred.")
        self.pauseServerRefreshBox = ToggleSwitch("Pause refresh while Launcher is out of focus")
        self.suppressStartupSoundBox = ToggleSwitch("Suppress startup notification sounds for 1 minute")
        for box in (self.pauseServerRefreshBox, self.suppressStartupSoundBox):
            box.setObjectName("settingsToggle")
            svl.addWidget(box)
        svl.addStretch(1)
        grid.addWidget(servers, 1, 1)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        # Cleanup -----------------------------------------------------------
        cleanup, cl = make_card("TEMPORARY FILE CLEANUP", "Optional scheduled cleanup for captured media. Disabled means keep files indefinitely.")

        def make_cleanup_row(label_text, combo_name, spin_name):
            row = QtWidgets.QHBoxLayout(); row.setSpacing(10)
            checkbox = ToggleSwitch(label_text); checkbox.setObjectName("settingsToggle"); checkbox.setMinimumWidth(230)
            combo = QtWidgets.QComboBox(); combo.setObjectName(combo_name)
            combo.addItem("older than 1 day", 1); combo.addItem("older than 3 days", 3); combo.addItem("older than 7 days", 7); combo.addItem("older than 30 days", 30); combo.addItem("Custom", -1)
            combo.setFixedWidth(theme_int("settings.cleanup_combo_width", 190)); combo.setEnabled(False)
            custom = QtWidgets.QSpinBox(); custom.setObjectName(spin_name); custom.setRange(1,3650); custom.setSuffix(" days"); custom.setValue(14); custom.setFixedWidth(110); custom.setVisible(False); custom.setEnabled(False)
            def refresh():
                enabled = checkbox.isChecked(); combo.setEnabled(enabled); custom.setEnabled(enabled); custom.setVisible(enabled and combo.currentData() == -1)
            checkbox.toggled.connect(refresh); combo.currentIndexChanged.connect(refresh)
            row.addWidget(checkbox); row.addStretch(1); row.addWidget(combo); row.addWidget(custom)
            cl.addLayout(row)
            return checkbox, combo, custom

        self.cleanupScreenshotsBox, self.cleanupScreenshotsCombo, self.cleanupScreenshotsCustom = make_cleanup_row("Clean screenshots", "cleanupScreenshotsCombo", "cleanupScreenshotsCustom")
        self.cleanupDemosBox, self.cleanupDemosCombo, self.cleanupDemosCustom = make_cleanup_row("Clean demos", "cleanupDemosCombo", "cleanupDemosCustom")
        cleanup_hint = QtWidgets.QLabel("Screenshots include OSP + Q3Elite captures. Demos target OSP demos."); cleanup_hint.setObjectName("settingsHint"); cl.addWidget(cleanup_hint)
        lay.addWidget(cleanup)

        # Storage / maintenance --------------------------------------------
        tools_grid = QtWidgets.QGridLayout(); tools_grid.setHorizontalSpacing(12); tools_grid.setVerticalSpacing(12)
        cache, cc = make_card("DOWNLOAD CACHE", "Launcher-generated caches and downloaded archives.")
        path_row = QtWidgets.QHBoxLayout()
        cp = QtWidgets.QLabel(str(CACHE_DIR)); cp.setObjectName("settingsPath"); cp.setWordWrap(True); path_row.addWidget(cp,1)
        open_cache = MaterialPushButton(""); open_cache.setObjectName("iconButton"); open_cache.setToolTip("Open Cache folder"); open_cache.setFixedSize(38,38)
        if qta is not None:
            try: open_cache.setIcon(qta.icon("ph.folder-open", color=theme_value("colors.text.secondary", "#C1CBD2")))
            except Exception: open_cache.setText("…")
        else: open_cache.setText("…")
        open_cache.clicked.connect(self.open_cache_folder); path_row.addWidget(open_cache); cc.addLayout(path_row)
        cache_actions = QtWidgets.QHBoxLayout()
        clear_cache = MaterialPushButton("CLEAR MEDIA CACHE"); clear_cache.setObjectName("secondaryButton"); clear_cache.clicked.connect(self.clear_launcher_cache); cache_actions.addWidget(clear_cache)
        clear_download_cache = MaterialPushButton("CLEAR DOWNLOADS"); clear_download_cache.setObjectName("secondaryButton"); clear_download_cache.clicked.connect(self.clear_download_cache); cache_actions.addWidget(clear_download_cache)
        cache_actions.addStretch(1); cc.addLayout(cache_actions)
        tools_grid.addWidget(cache, 0, 0)

        maintenance, ml = make_card("MAINTENANCE", "Repair launcher files or open the Q3Elite configuration editor.")
        maintenance_actions = QtWidgets.QHBoxLayout()
        self.repairLauncherButton = MaterialPushButton("REPAIR LAUNCHER"); self.repairLauncherButton.setObjectName("secondaryButton"); self.repairLauncherButton.clicked.connect(self.repair_launcher); maintenance_actions.addWidget(self.repairLauncherButton)
        self.configEditorButton = GlowButton("CONFIG EDITOR"); self.configEditorButton.setObjectName("applyButton"); self.configEditorButton.clicked.connect(self.open_config_editor); self.configEditorButton.setEnabled(config_editor_available()); maintenance_actions.addWidget(self.configEditorButton)
        ml.addLayout(maintenance_actions); ml.addStretch(1)
        tools_grid.addWidget(maintenance, 0, 1)
        tools_grid.setColumnStretch(0,1); tools_grid.setColumnStretch(1,1)
        lay.addLayout(tools_grid)

        # Keep the primary save action visible while the settings body scrolls.
        lay.addStretch(1)
        scroll.setWidget(body)
        self._optimize_heavy_scroll_area(scroll)
        outer.addWidget(scroll, 1)

        footer = QtWidgets.QFrame(); footer.setObjectName("sectionActionBar")
        fl = QtWidgets.QHBoxLayout(footer); fl.setContentsMargins(14,10,14,10)
        note = QtWidgets.QLabel("Changes are stored in launcher settings and applied immediately where possible."); note.setObjectName("muted"); fl.addWidget(note,1)
        apply = GlowButton("SAVE SETTINGS"); apply.setObjectName("applyButton"); apply.clicked.connect(apply_settings); fl.addWidget(apply)
        self.settingsFooterWrap = QtWidgets.QWidget()
        self.settingsFooterWrap.setObjectName("settingsFooterWrap")
        self.settingsFooterWrapLayout = QtWidgets.QHBoxLayout(self.settingsFooterWrap)
        self.settingsFooterWrapLayout.setContentsMargins(4, 0, 20, 4)
        self.settingsFooterWrapLayout.addWidget(footer)
        outer.addWidget(self.settingsFooterWrap)
        QtCore.QTimer.singleShot(0, self._sync_scroll_clearance)
        return page

    # ------------------------------------------------------------------
    # STATISTICS — freekill.ru player statistics
    # ------------------------------------------------------------------
    def _build_statistics(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = MetallicLabel("STATISTICS")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        hint = QtWidgets.QLabel("FREEKILL.RU  •  WEEK / MONTH")
        hint.setObjectName("muted")
        header.addWidget(hint)
        root.addLayout(header)

        search_card = SurfaceCard()
        search_card.setObjectName("statisticsSearchCard")
        self.statisticsSearchCard = search_card
        search_l = QtWidgets.QVBoxLayout(search_card)
        search_l.setContentsMargins(18, 15, 18, 15)
        search_l.setSpacing(9)

        search_row = QtWidgets.QHBoxLayout()
        search_row.setSpacing(10)
        self.statisticsNickname = QtWidgets.QLineEdit()
        self.statisticsNickname.setObjectName("statisticsNickname")
        self.statisticsNickname.setPlaceholderText("freekill.ru profile URL")
        self.statisticsNickname.setClearButtonEnabled(True)
        self.statisticsNickname.setText(str(launcher_settings.get("statistics_profile_url", "") or ""))
        self.statisticsNickname.returnPressed.connect(self.lookup_statistics)
        search_row.addWidget(self.statisticsNickname, 1)

        self.statisticsSearchButton = GlowButton("SEARCH")
        self.statisticsSearchButton.setObjectName("applyButton")
        self.statisticsSearchButton.setFixedWidth(145)
        self.statisticsSearchButton.clicked.connect(self.lookup_statistics)
        search_row.addWidget(self.statisticsSearchButton)
        search_l.addLayout(search_row)

        self.statisticsMessage = QtWidgets.QLabel("Link your freekill.ru profile once; it will be saved by the launcher.")
        self.statisticsMessage.setObjectName("statisticsMessage")
        self.statisticsMessage.setWordWrap(True)
        search_l.addWidget(self.statisticsMessage)
        root.addWidget(search_card)

        self.statisticsResults = QtWidgets.QScrollArea()
        self.statisticsResults.setObjectName("statisticsScroll")
        self.statisticsResults.setWidgetResizable(True)
        self.statisticsResults.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.statisticsResults.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.statisticsResults.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.statisticsContent = QtWidgets.QWidget()
        self.statisticsContent.setObjectName("statisticsContent")
        self.statisticsLayout = QtWidgets.QVBoxLayout(self.statisticsContent)
        self.statisticsLayout.setContentsMargins(0, 0, 8, 0)
        self.statisticsLayout.setSpacing(11)
        self.statisticsResults.setWidget(self.statisticsContent)
        root.addWidget(self.statisticsResults, 1)

        self._statistics_payload = None
        self._statistics_period = str(launcher_settings.get("statistics_period", "week") or "week").lower()
        if self._statistics_period not in ("week", "month"):
            self._statistics_period = "week"
        self._statistics_worker = None
        self.statisticsFilterShortcut = QtGui.QShortcut(QtGui.QKeySequence("F4"), self)
        self.statisticsFilterShortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
        self.statisticsFilterShortcut.activated.connect(self._statistics_cycle_period)
        self.statisticsFilterShortcut.setEnabled(False)
        self._show_statistics_empty()
        return page

    @staticmethod
    def _statistics_player_id(value):
        value = str(value or "").strip()
        if not value:
            raise ValueError("Enter a nickname or freekill.ru profile URL.")
        if "://" in value:
            parsed = urllib.parse.urlparse(value)
            if not parsed.netloc.lower().endswith("freekill.ru"):
                raise ValueError("Only freekill.ru profile URLs are supported.")
            encoded = urllib.parse.parse_qs(parsed.query).get("name", [""])[0].strip()
            if not encoded:
                raise ValueError("This freekill.ru URL does not contain a player name.")
            return encoded
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    @staticmethod
    def _statistics_find_current(value):
        if isinstance(value, dict):
            if value.get("player") == "current" and isinstance(value.get("stat"), list):
                return value
            for child in value.values():
                found = ModernLauncherWindow._statistics_find_current(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = ModernLauncherWindow._statistics_find_current(child)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _statistics_find_by_base64(value, player_id):
        if isinstance(value, dict):
            if value.get("base64") == player_id and isinstance(value.get("stat"), list):
                return value
            for child in value.values():
                found = ModernLauncherWindow._statistics_find_by_base64(child, player_id)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = ModernLauncherWindow._statistics_find_by_base64(child, player_id)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _statistics_normalize_record(record, fallback_name=""):
        stat = list(record.get("stat") or [])
        weapon_data = list(record.get("data") or [])
        if len(stat) < 20:
            raise ValueError("freekill.ru returned an incomplete player record.")
        names = ["Gauntlet", "Machinegun", "Shotgun", "Grenade Launcher", "Rocket Launcher", "Lightning Gun", "Railgun", "Plasma Gun"]
        weapons = []
        for i, name in enumerate(names):
            row = weapon_data[i] if i < len(weapon_data) and isinstance(weapon_data[i], list) else []
            vals = list(row) + [0, 0, 0, 0]
            weapons.append({"weapon": name, "hits": vals[0], "shots": vals[1], "frags": vals[2], "deaths": vals[3]})
        deaths = int(stat[4] or 0)
        dr = int(stat[1] or 0)
        return {
            "nickname": record.get("player") if record.get("player") != "current" else fallback_name,
            "elo": int(stat[19] or 0),
            "games": int(stat[12] or 0) + int(stat[13] or 0),
            "online_ms": int(stat[7] or 0),
            "kills": int(stat[2] or 0),
            "deaths": deaths,
            "kd": (float(stat[2]) / deaths) if deaths else 0.0,
            "damage_given": int(stat[0] or 0),
            "damage_received": dr,
            "damage_ratio": (float(stat[0]) / dr) if dr else 0.0,
            "thaws": int(stat[3] or 0),
            "unfreezes": int(stat[5] or 0),
            "suicides": int(stat[6] or 0),
            "weapons": weapons,
        }

    @staticmethod
    def _statistics_fetch(value):
        player_id = ModernLauncherWindow._statistics_player_id(value)
        try:
            decoded_name = base64.b64decode(player_id + "=" * (-len(player_id) % 4)).decode("utf-8", errors="replace")
        except Exception:
            decoded_name = value

        # freekill frontend sends Moscow offset relative to the browser's local UTC offset.
        local_offset = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
        local_hours = local_offset.total_seconds() / 3600.0
        msk_shift = local_hours - 3
        if float(msk_shift).is_integer():
            msk_shift = int(msk_shift)
        user = int(time.time() * 1000)
        request_text = f"shift={msk_shift}\tuser={user}\tname={player_id}\t"
        token = base64.b64encode(request_text.encode("utf-8")).decode("ascii")
        url = "https://freekill.ru/fcgi/" + token
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://freekill.ru/index.html?name=" + urllib.parse.quote(player_id),
        })
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        objects = payload.get("object") or []
        if len(objects) < 4:
            raise ValueError("freekill.ru returned an unexpected response.")
        week_obj, month_obj = objects[-4], objects[-3]
        # Endpoint nesting has changed across deployments. Search the complete
        # WEEK/MONTH object by player id, then use `current` as compatibility
        # fallback. This keeps click/F4 period switching functional.
        week = ModernLauncherWindow._statistics_find_by_base64(week_obj, player_id)
        if week is None:
            week = ModernLauncherWindow._statistics_find_current(week_obj)
        month = ModernLauncherWindow._statistics_find_by_base64(month_obj, player_id)
        if month is None:
            month = ModernLauncherWindow._statistics_find_current(month_obj)
        if week is None and month is None:
            raise LookupError(f"Player '{decoded_name}' was not found in current week/month statistics.")
        clean_name = payload.get("clean") or decoded_name
        return {
            "nickname": clean_name,
            "week": ModernLauncherWindow._statistics_normalize_record(week, clean_name) if week else None,
            "month": ModernLauncherWindow._statistics_normalize_record(month, clean_name) if month else None,
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

    def _statistics_show_search(self):
        if hasattr(self, "statisticsSearchCard"):
            self.statisticsSearchCard.show()
        self.statisticsNickname.setFocus()
        self.statisticsNickname.selectAll()

    def lookup_statistics(self):
        value = self.statisticsNickname.text().strip()
        if not value:
            self.statisticsMessage.setText("Enter your freekill.ru profile URL first.")
            self.statisticsNickname.setFocus()
            return
        self.statisticsSearchButton.setEnabled(False)
        self.statisticsSearchButton.setText("SEARCHING...")
        self.statisticsMessage.setText("Loading WEEK and MONTH statistics from freekill.ru...")
        self._show_statistics_empty("Loading player statistics...")

        class StatisticsWorker(QtCore.QThread):
            done = pyqtSignal(object)
            failed = pyqtSignal(str)
            def __init__(self, query, parent=None):
                super().__init__(parent)
                self.query = query
            def run(self):
                try:
                    self.done.emit(ModernLauncherWindow._statistics_fetch(self.query))
                except Exception as exc:
                    self.failed.emit(str(exc))

        self._statistics_worker = StatisticsWorker(value, self)
        self._statistics_worker.done.connect(self._statistics_loaded)
        self._statistics_worker.failed.connect(self._statistics_failed)
        self._statistics_worker.start()

    def _statistics_loaded(self, payload):
        self.statisticsSearchButton.setText("SEARCH")
        self.statisticsSearchButton.setEnabled(True)
        self._statistics_payload = payload
        try:
            player_id = self._statistics_player_id(self.statisticsNickname.text().strip())
            saved_url = "https://freekill.ru/index.html?name=" + urllib.parse.quote(player_id)
            self.statisticsNickname.setText(saved_url)
            launcher_settings["statistics_profile_url"] = saved_url
            save_launcher_settings(launcher_settings)
        except Exception:
            pass
        # Keep the user's last WEEK/MONTH filter across launcher restarts.
        preferred_period = str(launcher_settings.get("statistics_period", self._statistics_period) or "week").lower()
        if preferred_period in ("week", "month") and payload.get(preferred_period) is not None:
            self._statistics_period = preferred_period
        elif payload.get("week") is not None:
            self._statistics_period = "week"
        else:
            self._statistics_period = "month"
        self.statisticsMessage.setText("Statistics loaded from freekill.ru.")
        self._render_statistics(payload)
        if hasattr(self, "statisticsSearchCard"):
            self.statisticsSearchCard.hide()

    def _statistics_failed(self, message):
        if hasattr(self, "statisticsSearchCard"):
            self.statisticsSearchCard.show()
        self.statisticsSearchButton.setText("SEARCH")
        self.statisticsSearchButton.setEnabled(True)
        self.statisticsMessage.setText("Statistics error: " + message)
        self._show_statistics_empty("Could not load this player's WEEK / MONTH statistics.")

    @staticmethod
    def _statistics_online(ms):
        minutes = max(0, int(ms or 0) // 60000)
        hours, mins = divmod(minutes, 60)
        return f"{hours}h {mins:02d}m" if hours else f"{mins}m"

    def _statistics_tab_button(self, text):
        button = MaterialPushButton(text)
        button.setObjectName("statisticsTabButton")
        button.setCheckable(True)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setMinimumWidth(110)
        return button

    def _render_statistics(self, payload):
        self._clear_statistics_results()
        data = payload.get(self._statistics_period)
        if data is None:
            other = "month" if self._statistics_period == "week" else "week"
            data = payload.get(other)
            self._statistics_period = other
        if data is None:
            self._show_statistics_empty("No current WEEK / MONTH data for this player.")
            return

        profile = SurfaceCard()
        profile.setObjectName("statisticsProfileCard")
        profile_l = QtWidgets.QHBoxLayout(profile)
        profile_l.setContentsMargins(18, 12, 18, 12)
        nick = QtWidgets.QLabel(str(payload.get("nickname") or data.get("nickname") or "Unknown"))
        nick.setObjectName("statisticsPlayerName")
        profile_l.addWidget(nick, 1)

        self.statisticsPeriodToggle = MaterialPushButton(self._statistics_period.upper())
        self.statisticsPeriodToggle.setObjectName("statisticsPeriodToggle")
        self.statisticsPeriodToggle.setProperty("period", self._statistics_period)
        self.statisticsPeriodToggle.setMinimumWidth(118)
        self.statisticsPeriodToggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.statisticsPeriodToggle.setToolTip("Toggle WEEK / MONTH  •  F4")
        self.statisticsPeriodToggle.setEnabled(payload.get("week") is not None or payload.get("month") is not None)
        self.statisticsPeriodToggle.clicked.connect(lambda _checked=False: self._statistics_cycle_period())
        profile_l.addWidget(self.statisticsPeriodToggle)

        elo = QtWidgets.QLabel(f"ELO  {data.get('elo', 0):,}")
        elo.setObjectName("statisticsMatchesValue")
        profile_l.addWidget(elo)
        self.statisticsLayout.addWidget(profile)

        weapon_title = QtWidgets.QLabel("WEAPONS")
        weapon_title.setObjectName("statisticsGroupTitle")
        self.statisticsLayout.addWidget(weapon_title)
        self.statisticsLayout.addWidget(self._build_statistics_weapon_table(data))

        general_title = QtWidgets.QLabel("GENERAL")
        general_title.setObjectName("statisticsGroupTitle")
        self.statisticsLayout.addWidget(general_title)
        self.statisticsLayout.addWidget(self._build_statistics_general(data))
        self.statisticsLayout.addStretch(1)

    def _statistics_cycle_period(self):
        if not self._statistics_payload:
            return
        target = "month" if self._statistics_period == "week" else "week"
        if self._statistics_payload.get(target) is not None:
            self._statistics_switch_period(target)
            return
        # Retry once when the endpoint returned only one period instead of
        # making F4/click silently appear disabled.
        if self.statisticsNickname.text().strip() and not (self._statistics_worker and self._statistics_worker.isRunning()):
            self.lookup_statistics()

    def _statistics_switch_period(self, period):
        if self._statistics_payload and self._statistics_payload.get(period) is not None:
            self._statistics_period = period
            launcher_settings["statistics_period"] = period
            try:
                save_launcher_settings(launcher_settings)
            except Exception as error:
                print(f"[statistics] Could not save period: {error}")
            self._render_statistics(self._statistics_payload)

    def _build_statistics_weapon_table(self, data):
        table = QtWidgets.QTableWidget()
        table.setObjectName("statisticsWeaponTable")
        table.setHorizontalHeader(MaterialHeaderView(QtCore.Qt.Orientation.Horizontal, table))
        table.setItemDelegate(FullRowHoverDelegate(table))
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["WEAPON", "ACCURACY", "HITS", "SHOTS", "FRAGS"])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            table.horizontalHeader().setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        weapons = data.get("weapons", []) or []
        table.setIconSize(QtCore.QSize(20, 20))
        table.setRowCount(len(weapons))
        weapon_icons = {
            "Gauntlet": "gauntlet.png",
            "Machinegun": "machinegun.png",
            "Shotgun": "shotgun.png",
            "Grenade Launcher": "grenadelauncher.png",
            "Rocket Launcher": "rocketlauncher.png",
            "Lightning Gun": "lightninggun.png",
            "Railgun": "railgun.png",
            "Plasma Gun": "plasmagun.png",
        }
        statistics_icons_dir = ASSETS_DIR / "statistics"
        for row, weapon in enumerate(weapons):
            hits = int(weapon.get("hits", 0) or 0)
            shots = int(weapon.get("shots", 0) or 0)
            frags = int(weapon.get("frags", 0) or 0)
            accuracy = (hits / shots * 100.0) if shots else None
            weapon_name = weapon.get("weapon", "Unknown")
            values = [weapon_name, f"{accuracy:.1f}%" if accuracy is not None else "—", f"{hits:,}", f"{shots:,}", f"{frags:,}"]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                if col == 0:
                    icon_path = statistics_icons_dir / weapon_icons.get(weapon_name, "")
                    if icon_path.is_file():
                        item.setIcon(QtGui.QIcon(str(icon_path)))
                else:
                    item.setTextAlignment(int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
                table.setItem(row, col, item)
            table.setRowHeight(row, 27)
        table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.verticalHeader().setDefaultSectionSize(27)
        header_h = max(32, table.horizontalHeader().sizeHint().height())
        table.setFixedHeight(header_h + max(1, len(weapons)) * 27 + table.frameWidth() * 2 + 2)
        return table

    def _build_statistics_general(self, data):
        """Compact dashboard cards; ELO stays only in the profile header."""
        frame = QtWidgets.QFrame()
        frame.setObjectName("statisticsGeneralCards")
        grid = QtWidgets.QGridLayout(frame)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(7)

        def add_card(row, column, label, value, column_span=1):
            tile = SurfaceCard()
            tile.setObjectName("statisticsCompactTile")
            tile_l = QtWidgets.QVBoxLayout(tile)
            tile_l.setContentsMargins(10, 6, 10, 6)
            tile_l.setSpacing(1)

            name = QtWidgets.QLabel(label)
            name.setObjectName("statisticsStatName")
            val = MetallicLabel(value)
            val.setObjectName("statisticsStatValue")
            val.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            tile_l.addWidget(name)
            tile_l.addWidget(val)
            grid.addWidget(tile, row, column, 1, column_span)

        # Row 1: core activity/combat values. K/D gets its own card.
        add_card(0, 0, "GAMES", f"{data.get('games', 0):,}")
        add_card(0, 1, "ONLINE", self._statistics_online(data.get('online_ms', 0)))
        add_card(0, 2, "KILLS", f"{data.get('kills', 0):,}")
        add_card(0, 3, "DEATHS", f"{data.get('deaths', 0):,}")
        add_card(0, 4, "K/D", f"{data.get('kd', 0.0):.2f}")

        # Row 2: keep the damage ratio directly beside both damage totals.
        add_card(1, 0, "DAMAGE GIVEN", f"{data.get('damage_given', 0):,}", 2)
        add_card(1, 2, "DAMAGE RECEIVED", f"{data.get('damage_received', 0):,}", 2)
        add_card(1, 4, "DG / DR", f"{data.get('damage_ratio', 0.0):.2f}")

        # Row 3: Freeze-specific values, kept separate for quicker reading.
        add_card(2, 0, "THAWS", f"{data.get('thaws', 0):,}", 2)
        add_card(2, 2, "UNFREEZES", f"{data.get('unfreezes', 0):,}", 2)
        add_card(2, 4, "SUICIDES", f"{data.get('suicides', 0):,}")

        for col in range(5):
            grid.setColumnStretch(col, 1)
        return frame

    # ------------------------------------------------------------------
    # SCREENSHOTS / DEMOS — local OSP media
    # ------------------------------------------------------------------
    def _osp_screenshots_dir(self):
        return GAME_ROOT / "baseq3" / "mods" / "osp" / "screenshots"

    def _reshade_screenshots_dir(self):
        return GAME_ROOT / "Q3Elite" / "Screenshots"

    def _osp_demos_dir(self):
        return GAME_ROOT / "baseq3" / "mods" / "osp" / "demos"

    def _media_category(self, name):
        """Infer a useful gametype bucket from common Q3 screenshot/demo filenames."""
        s = str(name or "").lower()
        stem = Path(s).stem

        # Screenshots made by common Q3 screenshot commands are often shotXXXX.*
        if "shot" in stem:
            return "Singleplayer"

        # Token-aware checks avoid accidental matches inside map/player names.
        tokens = [x for x in re.split(r"[^a-z0-9]+", stem) if x]
        if "ctf" in tokens or any(x.startswith("ctf") and x[3:].isdigit() for x in tokens):
            return "CTF"
        if "tdm" in tokens or "team" in tokens:
            return "TDM"
        if "ffa" in tokens or "dm" in tokens:
            return "FFA"
        return "Others"

    def _media_matches_filter(self, path, query, category):
        if query and query.lower() not in path.name.lower():
            return False
        return category == "All" or self._media_category(path.name) == category

    def _media_sort_files(self, files, mode):
        reverse = mode.endswith("↓")
        if mode.startswith("Name"):
            return sorted(files, key=lambda p: p.name.lower(), reverse=reverse)
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=reverse)

    def _make_media_toolbar(self, search_attr, sort_attr, filter_attr, refresh_callback):
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)

        search = QtWidgets.QLineEdit()
        search.setObjectName("mediaSearch")
        search.setPlaceholderText("Ctrl+F  Search...")
        search.setClearButtonEnabled(True)
        setattr(self, search_attr, search)
        bar.addWidget(search, 1)

        sort = QtWidgets.QComboBox()
        sort.setObjectName("mediaCombo")
        sort.addItems(["Date ↓", "Date ↑", "Name ↓", "Name ↑"])
        setattr(self, sort_attr, sort)
        bar.addWidget(sort)

        filt = QtWidgets.QComboBox()
        filt.setObjectName("mediaCombo")
        filt.addItems(["All", "FFA", "TDM", "CTF", "Singleplayer", "Others"])
        setattr(self, filter_attr, filt)
        bar.addWidget(filt)

        # Typing used to rebuild the entire screenshot/demo model on every
        # keystroke. Debounce text filtering while keeping sort/filter clicks
        # immediate. The timer is parented to the input and therefore follows
        # the page lifetime automatically.
        search_timer = QtCore.QTimer(search)
        search_timer.setSingleShot(True)
        search_timer.setInterval(120)
        search_timer.timeout.connect(refresh_callback)
        search.textChanged.connect(lambda _text, t=search_timer: t.start())
        sort.currentTextChanged.connect(refresh_callback)
        filt.currentTextChanged.connect(refresh_callback)
        return bar

    def _focus_current_media_search(self):
        page = self.pages.currentWidget()
        if page is getattr(self, "statisticsPage", None):
            self._statistics_show_search()
            return True
        if page is getattr(self, "screenshotsPage", None):
            self.screenshotSearch.setFocus()
            self.screenshotSearch.selectAll()
            return True
        if page is getattr(self, "demosPage", None):
            self.demoSearch.setFocus()
            self.demoSearch.selectAll()
            return True
        if page is getattr(self, "mapsPage", None):
            target = self.onlineMapsSearch if getattr(self, "mapsMode", "local") == "online" else self.mapsSearch
            target.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
            target.selectAll()
            return True
        return False

    def _build_screenshots(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = MetallicLabel("SCREENSHOTS")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.screenshotCountLabel = QtWidgets.QLabel("")
        self.screenshotCountLabel.setObjectName("muted")
        header.addWidget(self.screenshotCountLabel)
        refresh = GlowButton("↻  REFRESH")
        refresh.setObjectName("serverToolbarButton")
        refresh.clicked.connect(self.refresh_screenshots)
        header.addWidget(refresh)
        self.screenshotSourceButton = GlowButton("")
        self.screenshotSourceButton.setObjectName("mediaSourceButton")
        self.screenshotSourceButton.clicked.connect(self._cycle_screenshot_source)
        header.addWidget(self.screenshotSourceButton)

        prev_top = GlowButton("‹")
        prev_top.setObjectName("serverToolbarButton")
        prev_top.setFixedWidth(42)
        prev_top.clicked.connect(lambda: self._step_screenshot(-1))
        header.addWidget(prev_top)
        next_top = GlowButton("›")
        next_top.setObjectName("serverToolbarButton")
        next_top.setFixedWidth(42)
        next_top.clicked.connect(lambda: self._step_screenshot(1))
        header.addWidget(next_top)
        full = GlowButton("FULL SCREEN")
        full.setObjectName("serverToolbarButton")
        full.clicked.connect(self.open_screenshot_fullscreen)
        header.addWidget(full)
        root.addLayout(header)



        self.screenshotToolsWidget = QtWidgets.QWidget()
        self.screenshotToolsWidget.setObjectName("screenshotToolsWidget")
        self.screenshotToolsWidget.setLayout(self._make_media_toolbar(
            "screenshotSearch", "screenshotSort", "screenshotFilter", self.refresh_screenshots
        ))
        self.screenshotToolsWidget.show()
        root.addWidget(self.screenshotToolsWidget)
        self.screenshotSearchKeyFilter = ScreenshotSearchFilter(self)
        self.screenshotSearch.installEventFilter(self.screenshotSearchKeyFilter)
        for action in self.screenshotSearch.actions():
            action.triggered.connect(
                lambda checked=False: QtCore.QTimer.singleShot(0, self._hide_screenshot_tools)
            )

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        body.setContentsMargins(0, 0, 0, 0)

        self.screenshotList = QtWidgets.QListWidget()
        self.screenshotList.setObjectName("mediaList")
        self.screenshotList.setMinimumWidth(190)
        self.screenshotList.setMaximumWidth(230)
        self.screenshotList.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Expanding
        )
        self.screenshotList.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.screenshotList.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.screenshotList.setMouseTracking(True)
        self.screenshotList.viewport().setMouseTracking(True)
        self.screenshotList.setItemDelegate(FullRowHoverDelegate(self.screenshotList))
        self.screenshotList.currentItemChanged.connect(self._on_screenshot_current_item_changed)
        self.screenshotList.itemClicked.connect(self._on_screenshot_item_clicked)
        self.screenshotList.itemDoubleClicked.connect(lambda _item: self.open_screenshot_fullscreen())
        self.screenshotPreview = ScreenshotPreviewLabel("No screenshots found.")
        self.screenshotPreview.setObjectName("screenshotPreview")
        self.screenshotPreview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.screenshotPreview.resized.connect(
            lambda: QtCore.QTimer.singleShot(0, self._rescale_screenshot_preview)
        )
        self.screenshotPreview.setMinimumSize(320, 180)
        self.screenshotPreview.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding
        )
        self.screenshotPreviewFrame = QtWidgets.QFrame()
        self.screenshotPreviewFrame.setObjectName("mediaPreviewFrame")
        frame_l = QtWidgets.QVBoxLayout(self.screenshotPreviewFrame)
        frame_l.setContentsMargins(0, 0, 0, 0)
        frame_l.addWidget(self.screenshotPreview)
        previewHost = AspectRatioHost(self.screenshotPreviewFrame, 16, 9, page)

        body.addWidget(previewHost, 1)
        body.addWidget(self.screenshotList)
        root.addLayout(body, 1)

        self.screenshotStatusLabel = QtWidgets.QLabel("")
        self.screenshotStatusLabel.setObjectName("screenshotStatus")
        self.screenshotStatusLabel.setMinimumHeight(24)
        self.screenshotStatusLabel.hide()
        root.addWidget(self.screenshotStatusLabel)
        self.screenshotStatusTimer = QtCore.QTimer(self)
        self.screenshotStatusTimer.setSingleShot(True)
        self.screenshotStatusTimer.timeout.connect(self.screenshotStatusLabel.hide)

        self.screenshotKeys = QtWidgets.QLabel(
            "KEY BINDS\n"
            "↑ / ↓ / ← / → : Navigation\n"
            "F : Fullscreen\n"
            "Ctrl+C : Copy screenshot (also Fullscreen)\n"
            "O : Open location\n"
            "Del : Delete screenshot\n"
            "F2 : Rename screenshot\n"
            "F3 : Cycle sorting\n"
            "F4 : Cycle gametype filter\n"
            "Ctrl+F : Search\n"
            "F1 : Show/Hide binds"
        )
        self.screenshotKeys.setObjectName("keyBindsLegend")
        self.screenshotKeys.hide()
        root.addWidget(self.screenshotKeys)

        self.screenshotShortcuts = []

        def shortcut(seq, fn):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            sc.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)
            sc.setEnabled(False)
            self.screenshotShortcuts.append(sc)
        shortcut("F1", self._toggle_screenshot_keys)
        shortcut("Left", lambda: self._step_screenshot(-1))
        shortcut("Right", lambda: self._step_screenshot(1))
        shortcut("Up", lambda: self._step_screenshot(-1))
        shortcut("Down", lambda: self._step_screenshot(1))
        shortcut("F", self.open_screenshot_fullscreen)
        shortcut("Ctrl+C", self.copy_selected_screenshot)
        shortcut("O", lambda: self.open_media_location(self._selected_screenshot_path()))
        shortcut("Delete", self.delete_selected_screenshot)
        shortcut("F2", self.rename_selected_screenshot)
        shortcut("F3", self._cycle_screenshot_sort)
        shortcut("F4", self._cycle_screenshot_filter)

        self._sync_screenshot_source_buttons()
        self._screenshots_loaded = False
        self._screenshots_dirty = True
        self.screenshotDirWatcher = QtCore.QFileSystemWatcher(self)
        for folder in (self._osp_screenshots_dir(), self._reshade_screenshots_dir()):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                self.screenshotDirWatcher.addPath(str(folder))
            except OSError:
                pass
        self.screenshotDirWatcher.directoryChanged.connect(self._mark_screenshots_dirty)
        return page

    def _mark_screenshots_dirty(self, _path=None):
        self._screenshots_dirty = True
        if self.pages.currentWidget() is getattr(self, "screenshotsPage", None):
            QtCore.QTimer.singleShot(180, self._refresh_screenshots_if_dirty)

    def _refresh_screenshots_if_dirty(self):
        if (getattr(self, "_screenshots_dirty", False)
                and self.pages.currentWidget() is getattr(self, "screenshotsPage", None)):
            self.refresh_screenshots()

    def _toggle_screenshot_keys(self):
        self.screenshotKeys.setVisible(not self.screenshotKeys.isVisible())
        # Redraw after Qt has recalculated the body geometry.
        QtCore.QTimer.singleShot(0, self._rescale_screenshot_preview)
        QtCore.QTimer.singleShot(30, self._rescale_screenshot_preview)

    def _hide_screenshot_tools(self):
        # Search toolbar is permanent; this helper now only leaves the input.
        self.screenshotList.setFocus()

    def _set_screenshot_status(self, text, error=False):
        if not text:
            self.screenshotStatusLabel.hide()
            return
        self.screenshotStatusLabel.setText(str(text))
        self.screenshotStatusLabel.setProperty("error", bool(error))
        self.screenshotStatusLabel.style().unpolish(self.screenshotStatusLabel)
        self.screenshotStatusLabel.style().polish(self.screenshotStatusLabel)
        self.screenshotStatusLabel.show()
        self.screenshotStatusTimer.start(1500)

    def _select_screenshot_row(self, row, show=True):
        count = self.screenshotList.count()
        if count <= 0:
            self._currentScreenshotPath = None
            self._currentScreenshotPixmap = None
            return None
        row = int(row) % count
        item = self.screenshotList.item(row)
        if item is None:
            return None

        # Keep Qt's native QListWidget current-item behavior. This fixes mouse
        # selection and also gives F2/Delete/etc. a real currentItem().
        self.screenshotList.setCurrentItem(
            item, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        item.setSelected(True)
        self.screenshotList.scrollToItem(item)

        path = Path(item.data(QtCore.Qt.ItemDataRole.UserRole))
        self._currentScreenshotPath = path if path.is_file() else None
        if show:
            self._display_screenshot_path(self._currentScreenshotPath)
        return item

    def _on_screenshot_current_item_changed(self, current, previous=None):
        if current is None:
            return
        path = Path(current.data(QtCore.Qt.ItemDataRole.UserRole))
        if path.is_file():
            self._currentScreenshotPath = path
            self._display_screenshot_path(path)

    def _on_screenshot_item_clicked(self, item):
        if item is None:
            return
        row = self.screenshotList.row(item)
        self._select_screenshot_row(row, show=True)

    def _display_screenshot_path(self, path):
        if not path or not Path(path).is_file():
            self._currentScreenshotPath = None
            self._currentScreenshotPixmap = None
            self.screenshotPreview.clear()
            self.screenshotPreview.setText("No screenshot selected.")
            return
        path = Path(path)
        pix = _load_pixmap_file(path)
        if pix.isNull():
            self.screenshotPreview.clear()
            self.screenshotPreview.setText("Could not load screenshot.")
            return
        self._currentScreenshotPath = path
        self._currentScreenshotPixmap = pix
        self._rescale_screenshot_preview()

    def set_screenshot_source(self, source):
        global launcher_settings
        source = source if source in ("reshade", "osp", "both") else "reshade"
        launcher_settings["screenshot_source"] = source
        try:
            save_launcher_settings(launcher_settings)
        except Exception:
            pass
        self._sync_screenshot_source_buttons()
        self.refresh_screenshots()

    def _cycle_screenshot_source(self):
        modes = ["reshade", "osp", "both"]
        current = launcher_settings.get("screenshot_source", "reshade")
        self.set_screenshot_source(modes[(modes.index(current) + 1) % len(modes)] if current in modes else "reshade")

    def _sync_screenshot_source_buttons(self):
        source = launcher_settings.get("screenshot_source", "reshade")
        if source not in ("reshade", "osp", "both"):
            source = "reshade"
        labels = {
            "reshade": "ReShade Screenshots",
            "osp": "OSP Screenshots",
            "both": "Both Locations",
        }
        if hasattr(self, "screenshotSourceButton"):
            self.screenshotSourceButton.setText(labels[source])
        return source

    def _cycle_media_sort(self, combo, prefix):
        current = combo.currentText()
        target = f"{prefix} ↓" if current == f"{prefix} ↑" else f"{prefix} ↑"
        combo.setCurrentText(target)

    def open_media_location(self, path):
        if not path:
            return
        try:
            import subprocess
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        except Exception:
            pass

    def copy_selected_screenshot(self):
        path = self._selected_screenshot_path()
        if path:
            pix = _load_pixmap_file(path)
            if not pix.isNull():
                QtWidgets.QApplication.clipboard().setPixmap(pix)
                self._set_screenshot_status(f"Copied to clipboard: {path.name}")

    def delete_selected_screenshot(self):
        path = self._selected_screenshot_path()
        if not path:
            return
        try:
            path.unlink()
            self.refresh_screenshots()
        except Exception as error:
            self.qerror(f"Could not delete screenshot:\n{error}")

    def rename_selected_screenshot(self):
        path = self._selected_screenshot_path()
        if not path:
            self._set_screenshot_status("No screenshot selected.", True)
            return
        item = self.screenshotList.currentItem()
        if item is None:
            self._set_screenshot_status("Selected screenshot is not available.", True)
            return

        # Keep the extension outside the editable text. The commit is handled
        # after the editor closes; itemChanged cannot recursively refresh the list.
        self._renameScreenshotPath = path
        self._renameScreenshotItem = item
        self._renameScreenshotActive = True

        self.screenshotList.blockSignals(True)
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsEditable)
        item.setText(path.stem)
        self.screenshotList.blockSignals(False)

        self.screenshotList.editItem(item)
        editor = self.screenshotList.itemWidget(item)
        QtCore.QTimer.singleShot(0, self._hook_screenshot_rename_editor)

    def _hook_screenshot_rename_editor(self):
        if not getattr(self, "_renameScreenshotActive", False):
            return
        editor = self.screenshotList.findChild(QtWidgets.QLineEdit)
        if editor is None:
            QtCore.QTimer.singleShot(10, self._hook_screenshot_rename_editor)
            return
        self._renameScreenshotEditor = editor
        self._renameScreenshotCancelled = False
        editor.setText(Path(self._renameScreenshotPath).stem)
        editor.selectAll()
        editor.installEventFilter(self)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(self._screenshot_rename_focus_changed)
            except Exception:
                pass
            app.focusChanged.connect(self._screenshot_rename_focus_changed)
        editor.editingFinished.connect(self._finish_screenshot_rename_editor)

    def _screenshot_rename_focus_changed(self, old, now):
        if not getattr(self, "_renameScreenshotActive", False):
            return
        editor = getattr(self, "_renameScreenshotEditor", None)
        if editor is not None and old is editor and now is not editor:
            QtCore.QTimer.singleShot(0, self._commit_screenshot_rename)

    def _finish_screenshot_rename_editor(self):
        if getattr(self, "_renameScreenshotCancelled", False):
            self._cancel_screenshot_rename()
        else:
            self._commit_screenshot_rename()

    def _cancel_screenshot_rename(self):
        if not getattr(self, "_renameScreenshotActive", False):
            return
        self._renameScreenshotActive = False
        self._renameScreenshotCancelled = True
        path = getattr(self, "_renameScreenshotPath", None)
        item = getattr(self, "_renameScreenshotItem", None)
        editor = getattr(self, "_renameScreenshotEditor", None)
        if editor is not None:
            try:
                editor.removeEventFilter(self)
            except Exception:
                pass
        if item is not None and path:
            self.screenshotList.blockSignals(True)
            item.setText(Path(path).name)
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.screenshotList.blockSignals(False)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(self._screenshot_rename_focus_changed)
            except Exception:
                pass
        self._renameScreenshotPath = None
        self._renameScreenshotItem = None
        self._renameScreenshotEditor = None
        self._set_screenshot_status("Rename cancelled.")
        self.screenshotList.setFocus()

    def _commit_screenshot_rename(self):
        if not getattr(self, "_renameScreenshotActive", False):
            return
        self._renameScreenshotActive = False

        path = getattr(self, "_renameScreenshotPath", None)
        item = getattr(self, "_renameScreenshotItem", None)
        editor = getattr(self, "_renameScreenshotEditor", None)
        stem = editor.text().strip() if editor is not None else (item.text().strip() if item else "")
        if editor is not None:
            try:
                editor.removeEventFilter(self)
            except Exception:
                pass

        app = QtWidgets.QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(self._screenshot_rename_focus_changed)
            except Exception:
                pass
        self._renameScreenshotPath = None
        self._renameScreenshotItem = None
        self._renameScreenshotEditor = None
        if not path or not path.is_file():
            self.refresh_screenshots()
            return

        if stem.lower().endswith(path.suffix.lower()):
            stem = stem[:-len(path.suffix)]
        stem = stem.strip().rstrip(".") or path.stem
        stem = re.sub(r'[<>:"/\\\\|?*]', "_", stem)
        new_path = path.with_name(stem + path.suffix)

        try:
            if new_path == path:
                self.refresh_screenshots()
                self._set_screenshot_status("Filename unchanged.")
                return
            if new_path.exists():
                self.refresh_screenshots()
                self._set_screenshot_status(f'Rename failed: "{new_path.name}" already exists.', True)
                return
            path.rename(new_path)
            self._currentScreenshotPath = new_path
            self.refresh_screenshots()
            self._set_screenshot_status(f"Renamed to: {new_path.name}")
        except PermissionError:
            self.refresh_screenshots()
            self._set_screenshot_status("Rename failed: screenshot is currently in use.", True)
        except OSError as error:
            self.refresh_screenshots()
            self._set_screenshot_status(f"Rename failed: {error}", True)

    def _cycle_screenshot_sort(self):
        modes = ["Date ↓", "Date ↑", "Name ↓", "Name ↑"]
        current = self.screenshotSort.currentText()
        self.screenshotSort.setCurrentText(modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0])

    def _cycle_screenshot_filter(self):
        modes = ["All", "FFA", "TDM", "CTF", "Singleplayer", "Others"]
        current = self.screenshotFilter.currentText()
        self.screenshotFilter.setCurrentText(modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0])

    def refresh_screenshots(self):
        osp_folder = self._osp_screenshots_dir()
        reshade_folder = self._reshade_screenshots_dir()
        osp_folder.mkdir(parents=True, exist_ok=True)
        reshade_folder.mkdir(parents=True, exist_ok=True)
        current_path = getattr(self, "_currentScreenshotPath", None)
        current_item = self.screenshotList.currentItem()
        previous = str(current_path) if current_path else (
            current_item.data(QtCore.Qt.ItemDataRole.UserRole) if current_item else None
        )
        source = self._sync_screenshot_source_buttons()
        folders = [reshade_folder] if source == "reshade" else [osp_folder] if source == "osp" else [reshade_folder, osp_folder]
        files = []
        for folder in folders:
            for ext in ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.webp"):
                files.extend(folder.glob(ext))
        query = self.screenshotSearch.text().strip() if hasattr(self, "screenshotSearch") else ""
        category = self.screenshotFilter.currentText() if hasattr(self, "screenshotFilter") else "All"
        mode = self.screenshotSort.currentText() if hasattr(self, "screenshotSort") else "Date ↓"
        files = [p for p in files if self._media_matches_filter(p, query, category)]
        files = self._media_sort_files(files, mode)
        self.screenshotList.setUpdatesEnabled(False)
        try:
            self.screenshotList.clear()
            restore_row = 0
            for i, path in enumerate(files):
                item = QtWidgets.QListWidgetItem(path.name)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(str(path))
                self.screenshotList.addItem(item)
                if previous and str(path) == previous:
                    restore_row = i
        finally:
            self.screenshotList.setUpdatesEnabled(True)
        self._screenshots_loaded = True
        self._screenshots_dirty = False
        self.screenshotCountLabel.setText(f"{len(files)} screenshot{'s' if len(files) != 1 else ''}")
        if files:
            row = min(restore_row, len(files) - 1)
            QtCore.QTimer.singleShot(0, lambda r=row: self._select_screenshot_row(r, show=True))
        else:
            self.screenshotList.clearSelection()
            self.screenshotList.setCurrentItem(None)
            self._currentScreenshotPath = None
            self._currentScreenshotPixmap = None
            self.screenshotPreview.clear()
            self.screenshotPreview.setText("No screenshots found.")

    def _selected_screenshot_path(self):
        item = self.screenshotList.currentItem()
        if item is not None:
            path = Path(item.data(QtCore.Qt.ItemDataRole.UserRole))
            if path.is_file():
                self._currentScreenshotPath = path
                return path
        path = getattr(self, "_currentScreenshotPath", None)
        return Path(path) if path and Path(path).is_file() else None

    def _show_selected_screenshot(self, _row=-1):
        self._display_screenshot_path(self._selected_screenshot_path())

    def _rescale_screenshot_preview(self):
        pix = getattr(self, "_currentScreenshotPixmap", None)
        if pix is None or pix.isNull():
            return

        area = self.screenshotPreview.size()
        if area.width() <= 1 or area.height() <= 1:
            return

        # Prefer using the complete available HEIGHT so the screenshot and
        # filename list visually end at the same Y position.
        target_h = area.height()
        target_w = round(target_h * 16 / 9)
        if target_w > area.width():
            target_w = area.width()
            target_h = round(target_w * 9 / 16)

        target = QtCore.QSize(max(1, target_w), max(1, target_h))
        scaled = pix.scaled(
            target,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation
        )

        self.screenshotPreview.setPixmap(_rounded_pixmap(scaled, theme_float("media.preview_radius", 11.0)))

    def _step_screenshot(self, direction):
        count = self.screenshotList.count()
        if not count:
            return
        row = self.screenshotList.currentRow()
        if row < 0:
            row = 0
        self._select_screenshot_row((row + direction) % count, show=True)

    def open_screenshot_fullscreen(self):
        path = self._selected_screenshot_path()
        if not path:
            return
        window = self

        class _ScreenshotDialog(MaterialDialog):
            def __init__(self, parent):
                super().__init__(parent)
                self.setObjectName("screenshotFullscreen")
                self.setWindowFlags(
                    QtCore.Qt.WindowType.FramelessWindowHint |
                    QtCore.Qt.WindowType.Dialog
                )
                lay = QtWidgets.QVBoxLayout(self)
                lay.setContentsMargins(0, 0, 0, 0)
                self.imageLabel = QtWidgets.QLabel()
                self.imageLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.imageLabel.setStyleSheet("background:#000;")
                self.imageLabel.setMouseTracking(True)
                self.imageLabel.installEventFilter(self)
                self.installEventFilter(self)
                lay.addWidget(self.imageLabel)
                self.refresh_image()
                self.wheelFilter = FullscreenScreenshotWheelFilter(self)
                app = QtWidgets.QApplication.instance()
                if app is not None:
                    app.installEventFilter(self.wheelFilter)

            def refresh_image(self):
                path = window._selected_screenshot_path()
                if not path:
                    return
                pix = _load_pixmap_file(path)
                if pix.isNull():
                    return
                self.setWindowTitle(path.name)
                screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos()) or QtGui.QGuiApplication.primaryScreen()
                target = screen.geometry().size() if screen else self.size()
                self.imageLabel.setPixmap(
                    pix.scaled(
                        target,
                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation
                    )
                )

            def step(self, direction):
                window._step_screenshot(direction)
                self.refresh_image()

            def eventFilter(self, obj, event):
                if event.type() == QtCore.QEvent.Type.Wheel:
                    delta = event.angleDelta().y() or event.angleDelta().x()
                    if delta:
                        self.step(1 if delta < 0 else -1)
                        event.accept()
                        return True
                return super().eventFilter(obj, event)

            def wheelEvent(self, event):
                delta = event.angleDelta().y() or event.angleDelta().x()
                if delta:
                    self.step(1 if delta < 0 else -1)
                    event.accept()
                    return
                super().wheelEvent(event)

            def keyPressEvent(self, event):
                key = event.key()
                if key in (QtCore.Qt.Key.Key_Escape, QtCore.Qt.Key.Key_F11, QtCore.Qt.Key.Key_F):
                    self.accept()
                    return
                if key in (QtCore.Qt.Key.Key_Right, QtCore.Qt.Key.Key_Down):
                    self.step(1)
                    return
                if key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Up):
                    self.step(-1)
                    return
                if (event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier
                        and key == QtCore.Qt.Key.Key_C):
                    window.copy_selected_screenshot()
                    return
                super().keyPressEvent(event)

            def done(self, result):
                app = QtWidgets.QApplication.instance()
                if app is not None and hasattr(self, "wheelFilter"):
                    try:
                        app.removeEventFilter(self.wheelFilter)
                    except Exception:
                        pass
                super().done(result)

            def mouseDoubleClickEvent(self, event):
                self.accept()

        dlg = _ScreenshotDialog(self)
        dlg.showFullScreen()
        dlg.exec()


    def _build_demos(self):
        page = QtWidgets.QWidget()
        page.setObjectName("demosPage")
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title_box = QtWidgets.QVBoxLayout(); title_box.setSpacing(1)
        title = MetallicLabel("DEMOS"); title.setObjectName("pageTitle"); title_box.addWidget(title)
        subtitle = QtWidgets.QLabel("Browse, inspect and launch recorded Quake 3 sessions."); subtitle.setObjectName("muted"); title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.demoCountLabel = QtWidgets.QLabel("")
        self.demoCountLabel.setObjectName("muted")
        header.addWidget(self.demoCountLabel)
        refresh = GlowButton("↻  REFRESH")
        refresh.setObjectName("serverToolbarButton")
        refresh.clicked.connect(self.refresh_demos)
        header.addWidget(refresh)
        root.addLayout(header)

        root.addLayout(self._make_media_toolbar(
            "demoSearch", "demoSort", "demoFilter", self.refresh_demos
        ))
        self.demoFilter.blockSignals(True)
        self.demoFilter.clear()
        self.demoFilter.addItems(["All", "Singleplayer", "Others"])
        self.demoFilter.blockSignals(False)

        self.demoSearchKeyFilter = ScreenshotSearchFilter(self)
        self.demoSearch.installEventFilter(self.demoSearchKeyFilter)

        split = QtWidgets.QHBoxLayout()
        split.setSpacing(12)

        library = self._card("demoLibraryCard")
        library_l = QtWidgets.QVBoxLayout(library)
        library_l.setContentsMargins(0, 0, 0, 0)
        library_l.setSpacing(0)

        self.demoList = QtWidgets.QTreeWidget()
        self.demoList.setObjectName("demoList")
        self.demoList.setHeader(MaterialHeaderView(QtCore.Qt.Orientation.Horizontal, self.demoList))
        self.demoList.setHeaderLabels(["DEMO", "TYPE", "MODIFIED", "SIZE"])
        self.demoList.setRootIsDecorated(False)
        self.demoList.setAlternatingRowColors(False)
        self.demoList.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.demoList.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.demoList.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.demoList.setMouseTracking(True)
        self.demoList.viewport().setMouseTracking(True)
        self.demoList.setItemDelegate(FullRowHoverDelegate(self.demoList))
        self.demoList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.demoList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.demoList.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.demoList.header().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.demoList.headerItem().setTextAlignment(
            3, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        )
        self.demoList.itemDoubleClicked.connect(lambda _item, _col: self.play_selected_demo())
        self.demoList.currentItemChanged.connect(lambda _c, _p: self._update_demo_details())
        self.demoWheelFilter = DemoWheelFilter(self)
        self.demoList.viewport().installEventFilter(self.demoWheelFilter)
        library_l.addWidget(self.demoList)
        split.addWidget(library, 1)

        details = self._card("demoDetailCard")
        details_width = theme_int("demos.details_width", 378)
        details.setFixedWidth(details_width)
        dl = QtWidgets.QVBoxLayout(details)
        dl.setContentsMargins(20, 18, 20, 18)
        dl.setSpacing(10)

        detail_label = QtWidgets.QLabel("SELECTED DEMO")
        detail_label.setObjectName("sectionTitle")
        dl.addWidget(detail_label)

        self.demoPreview = AspectPixmapLabel(
            theme_int("demos.preview_aspect_w", 16),
            theme_int("demos.preview_aspect_h", 9),
        )
        self.demoPreview.setObjectName("demoPreview")
        self.demoPreview.setText("NO LEVELSHOT")
        dl.addWidget(self.demoPreview)
        self.demoMapName = QtWidgets.QLabel("MAP  —")
        self.demoMapName.setObjectName("demoMapName")
        dl.addWidget(self.demoMapName)

        self.demoDetailName = QtWidgets.QLabel("No demo selected")
        self.demoDetailName.setObjectName("demoDetailName")
        self.demoDetailName.setWordWrap(True)
        self.demoDetailName.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.demoDetailName.setMinimumWidth(0)
        self.demoDetailName.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        dl.addWidget(self.demoDetailName)

        self.demoDetailMeta = QtWidgets.QLabel("Choose a recording from the library.")
        self.demoDetailMeta.setObjectName("muted")
        self.demoDetailMeta.setWordWrap(True)
        dl.addWidget(self.demoDetailMeta)

        self.demoDetailPath = ElidedTextLabel("")
        self.demoDetailPath.setObjectName("demoDetailPath")
        self.demoDetailPath.setFixedHeight(22)
        dl.addWidget(self.demoDetailPath)
        dl.addStretch(1)

        self.demoPlayButton = GlowButton("▶  PLAY DEMO")
        self.demoPlayButton.setObjectName("applyButton")
        self.demoPlayButton.setEnabled(False)
        self.demoPlayButton.clicked.connect(self.play_selected_demo)
        dl.addWidget(self.demoPlayButton)

        tools = QtWidgets.QGridLayout(); tools.setHorizontalSpacing(7); tools.setVerticalSpacing(7)
        demo_open = MaterialPushButton("OPEN LOCATION"); demo_open.setObjectName("secondaryButton"); demo_open.clicked.connect(lambda: self.open_media_location(self._selected_demo_path())); tools.addWidget(demo_open,0,0)
        demo_copy = MaterialPushButton("COPY PATH"); demo_copy.setObjectName("secondaryButton"); demo_copy.clicked.connect(self.copy_selected_demo_path); tools.addWidget(demo_copy,0,1)
        demo_rename = MaterialPushButton("RENAME"); demo_rename.setObjectName("secondaryButton"); demo_rename.clicked.connect(self.rename_selected_demo); tools.addWidget(demo_rename,1,0)
        demo_delete = MaterialPushButton("DELETE"); demo_delete.setObjectName("dangerButton"); demo_delete.clicked.connect(self.delete_selected_demo); tools.addWidget(demo_delete,1,1)
        dl.addLayout(tools)
        split.addWidget(details)
        root.addLayout(split, 1)

        self.demoStatusLabel = QtWidgets.QLabel("Double-click a demo or select it and press PLAY DEMO.")
        self.demoStatusLabel.setObjectName("muted")
        root.addWidget(self.demoStatusLabel)

        self.demoKeys = QtWidgets.QLabel()
        self.demoKeys.setText(
            "KEY BINDS\n"
            "Arrow Keys : Navigation\n"
            "Mouse Wheel : Navigation\n"
            "Enter / P : Play demo\n"
            "Ctrl+C : Copy demo path\n"
            "O : Open location\n"
            "Del : Delete demo\n"
            "F2 : Rename demo\n"
            "F3 : Sort Date ↓ / Date ↑ / Name ↓ / Name ↑\n"
            "F4 : Filter All / Singleplayer / Others\n"
            "Ctrl+F : Search\n"
            "F1 : Show/Hide binds"
        )
        self.demoKeys.setObjectName("keyBindsLegend")
        self.demoKeys.setWordWrap(False)
        self.demoKeys.hide()
        library_l.addWidget(self.demoKeys)

        self.demoShortcuts = []
        def shortcut(seq, fn):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            sc.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)
            sc.setEnabled(False)
            self.demoShortcuts.append(sc)
        shortcut("F1", self._toggle_demo_keys_overlay)
        shortcut("Up", lambda: self._step_demo(-1))
        shortcut("Left", lambda: self._step_demo(-1))
        shortcut("Down", lambda: self._step_demo(1))
        shortcut("Right", lambda: self._step_demo(1))
        shortcut("Return", self.play_selected_demo)
        shortcut("Ctrl+C", self.copy_selected_demo_path)
        shortcut("O", lambda: self.open_media_location(self._selected_demo_path()))
        shortcut("Delete", self.delete_selected_demo)
        shortcut("F2", self.rename_selected_demo)
        shortcut("F3", self._cycle_demo_sort)
        shortcut("F4", self._cycle_demo_filter)
        self._demos_loaded = False
        self._demos_dirty = True
        self.demoDirWatcher = QtCore.QFileSystemWatcher(self)
        try:
            folder = self._osp_demos_dir()
            folder.mkdir(parents=True, exist_ok=True)
            self.demoDirWatcher.addPath(str(folder))
        except OSError:
            pass
        self.demoDirWatcher.directoryChanged.connect(self._mark_demos_dirty)
        return page

    def _mark_demos_dirty(self, _path=None):
        self._demos_dirty = True
        if self.pages.currentWidget() is getattr(self, "demosPage", None):
            QtCore.QTimer.singleShot(180, self._refresh_demos_if_dirty)

    def _refresh_demos_if_dirty(self):
        if (getattr(self, "_demos_dirty", False)
                and self.pages.currentWidget() is getattr(self, "demosPage", None)):
            self.refresh_demos()

    def _position_demo_keys_overlay(self):
        # V18 keeps F1 help in the demo-library column, exactly like Screenshots.
        return

    def _toggle_demo_keys_overlay(self):
        if not hasattr(self, "demoKeys"):
            return
        self.demoKeys.setVisible(not self.demoKeys.isVisible())


    @staticmethod
    def _demo_map_name(filename):
        name = Path(str(filename or "")).name
        stem = re.sub(r"\.dm_\d+$", "", name, flags=re.I)
        parts = stem.split("-")
        if len(parts) >= 5:
            candidate = "-".join(parts[4:]).strip()
            if candidate:
                return candidate
        return stem

    def _update_demo_preview(self, path):
        if not hasattr(self, "demoPreview"):
            return
        mapname = self._demo_map_name(path.name if path else "")
        self.demoMapName.setText(f"MAP  {mapname or '—'}")
        levelshot = _resolve_levelshot_path(mapname)
        if levelshot is None:
            self.demoPreview.setSourcePixmap(QtGui.QPixmap())
            self.demoPreview.setText("NO LEVELSHOT")
            return
        pix = _load_pixmap_file(levelshot)
        self.demoPreview.setText("")
        self.demoPreview.setSourcePixmap(pix)

    def _update_demo_details(self):
        path = self._selected_demo_path()
        enabled = bool(path and path.is_file())
        if hasattr(self, "demoPlayButton"):
            self.demoPlayButton.setEnabled(enabled and bool(q3elite_is_installed()))
        if not hasattr(self, "demoDetailName"):
            return
        if not enabled:
            self.demoDetailName.setText("No demo selected")
            self.demoDetailMeta.setText("Choose a recording from the library.")
            self.demoDetailPath.setText("")
            self.demoMapName.setText("MAP  —")
            self.demoPreview.setSourcePixmap(QtGui.QPixmap())
            self.demoPreview.setText("NO LEVELSHOT")
            return
        try:
            st = path.stat()
            modified = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%d %b %Y  •  %H:%M")
            size = st.st_size
            size_text = f"{size / (1024*1024):.1f} MB" if size >= 1024*1024 else f"{size / 1024:.0f} KB"
        except Exception:
            modified, size_text = "Unknown date", "—"
        category = self._demo_category(path.name)
        self.demoDetailName.setText(path.stem)
        self.demoDetailMeta.setText(f"{category}  •  {modified}  •  {size_text}")
        self.demoDetailPath.setText(str(path))
        self._update_demo_preview(path)

    def _demo_category(self, name):
        return "Singleplayer" if "localhost" in str(name or "").lower() else "Others"

    def _demo_matches_filter(self, path, query, category):
        if query and query.lower() not in path.name.lower():
            return False
        return category == "All" or self._demo_category(path.name) == category

    def _step_demo(self, direction):
        count = self.demoList.topLevelItemCount()
        if not count:
            return
        item = self.demoList.currentItem()
        row = self.demoList.indexOfTopLevelItem(item) if item else -1
        if row < 0:
            row = 0
        target = (row + direction) % count
        self.demoList.setCurrentItem(self.demoList.topLevelItem(target))
        self.demoList.scrollToItem(self.demoList.currentItem())

    def _cycle_demo_sort(self):
        modes = ["Date ↓", "Date ↑", "Name ↓", "Name ↑"]
        current = self.demoSort.currentText()
        self.demoSort.setCurrentText(modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0])

    def _cycle_demo_filter(self):
        modes = ["All", "Singleplayer", "Others"]
        current = self.demoFilter.currentText()
        self.demoFilter.setCurrentText(modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0])

    def _update_demo_play_button(self):
        if hasattr(self, "demoPlayButton"):
            self.demoPlayButton.setEnabled(
                bool(q3elite_is_installed()) and self._selected_demo_path() is not None
            )

    def _selected_demo_path(self):
        item = self.demoList.currentItem()
        if not item:
            return None
        path = Path(item.data(0, QtCore.Qt.ItemDataRole.UserRole))
        return path if path.is_file() else None

    def copy_selected_demo_path(self):
        path = self._selected_demo_path()
        if not path:
            return
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(str(path.resolve()))])
        QtWidgets.QApplication.clipboard().setMimeData(mime)
        self.demoStatusLabel.setText(f"Copied file: {path.name}")

    def delete_selected_demo(self):
        path = self._selected_demo_path()
        if not path:
            return
        try:
            path.unlink()
            self.demoStatusLabel.setText(f"Deleted: {path.name}")
            self.refresh_demos()
        except Exception as error:
            self.demoStatusLabel.setText(f"Delete failed: {error}")

    def rename_selected_demo(self):
        item = self.demoList.currentItem()
        path = self._selected_demo_path()
        if not item or not path or getattr(self, "_renameDemoActive", False):
            return
        self._renameDemoActive = True
        self._renameDemoCancelled = False
        self._renameDemoPath = path
        self._renameDemoItem = item
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsEditable)
        self.demoList.editItem(item, 0)
        QtCore.QTimer.singleShot(0, self._hook_demo_rename_editor)

    def _hook_demo_rename_editor(self):
        if not getattr(self, "_renameDemoActive", False):
            return
        editor = self.demoList.findChild(QtWidgets.QLineEdit)
        if editor is None:
            QtCore.QTimer.singleShot(10, self._hook_demo_rename_editor)
            return
        self._renameDemoEditor = editor
        editor.setText(self._renameDemoPath.stem)
        editor.selectAll()
        self._demoRenameFilter = DemoRenameFilter(self)
        editor.installEventFilter(self._demoRenameFilter)
        editor.editingFinished.connect(self._finish_demo_rename_editor)

    def _finish_demo_rename_editor(self):
        if getattr(self, "_renameDemoCancelled", False):
            self._cancel_demo_rename()
        else:
            self._commit_demo_rename()

    def _cancel_demo_rename(self):
        if not getattr(self, "_renameDemoActive", False):
            return
        self._renameDemoActive = False
        path = getattr(self, "_renameDemoPath", None)
        item = getattr(self, "_renameDemoItem", None)
        editor = getattr(self, "_renameDemoEditor", None)
        if editor is not None:
            try: editor.removeEventFilter(self._demoRenameFilter)
            except Exception: pass
        if item is not None and path:
            self.demoList.blockSignals(True)
            item.setText(0, path.name)
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.demoList.blockSignals(False)
        self._renameDemoPath = self._renameDemoItem = self._renameDemoEditor = None
        self.demoStatusLabel.setText("Rename cancelled.")
        self.demoList.setFocus()

    def _commit_demo_rename(self):
        if not getattr(self, "_renameDemoActive", False):
            return
        self._renameDemoActive = False
        path = getattr(self, "_renameDemoPath", None)
        item = getattr(self, "_renameDemoItem", None)
        editor = getattr(self, "_renameDemoEditor", None)
        stem = editor.text().strip() if editor is not None else ""
        if editor is not None:
            try: editor.removeEventFilter(self._demoRenameFilter)
            except Exception: pass
        self._renameDemoPath = self._renameDemoItem = self._renameDemoEditor = None
        if not path or not path.is_file():
            self.refresh_demos()
            return
        if stem.lower().endswith(path.suffix.lower()):
            stem = stem[:-len(path.suffix)]
        stem = re.sub(r'[<>:"/\\|?*]', "_", stem.strip()).rstrip(".") or path.stem
        new_path = path.with_name(stem + path.suffix)
        try:
            if new_path.exists() and new_path != path:
                self.refresh_demos()
                self.demoStatusLabel.setText(f'Rename failed: "{new_path.name}" already exists.')
                return
            if new_path != path:
                path.rename(new_path)
            self.refresh_demos()
            for i in range(self.demoList.topLevelItemCount()):
                candidate = self.demoList.topLevelItem(i)
                if Path(candidate.data(0, QtCore.Qt.ItemDataRole.UserRole)) == new_path:
                    self.demoList.setCurrentItem(candidate)
                    break
            self.demoStatusLabel.setText(
                f"Renamed to: {new_path.name}" if new_path != path else "Filename unchanged."
            )
        except Exception as error:
            self.refresh_demos()
            self.demoStatusLabel.setText(f"Rename failed: {error}")

    def refresh_demos(self):
        import datetime
        folder = self._osp_demos_dir()
        folder.mkdir(parents=True, exist_ok=True)
        previous = self.demoList.currentItem().data(0, QtCore.Qt.ItemDataRole.UserRole) if self.demoList.currentItem() else None
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower().startswith(".dm_")]
        query = self.demoSearch.text().strip() if hasattr(self, "demoSearch") else ""
        category = self.demoFilter.currentText() if hasattr(self, "demoFilter") else "All"
        mode = self.demoSort.currentText() if hasattr(self, "demoSort") else "Date ↓"
        files = [p for p in files if self._demo_matches_filter(p, query, category)]
        files = self._media_sort_files(files, mode)
        self.demoList.setUpdatesEnabled(False)
        try:
            self.demoList.clear()
            restore = None
            for path in files:
                st = path.stat()
                modified = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d  %H:%M")
                size = st.st_size
                size_text = f"{size / (1024*1024):.1f} MB" if size >= 1024*1024 else f"{size / 1024:.0f} KB"
                item = QtWidgets.QTreeWidgetItem([path.name, self._demo_category(path.name), modified, size_text])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, str(path))
                item.setSizeHint(0, QtCore.QSize(0, theme_int("demos.row_height", 38)))
                item.setTextAlignment(3, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
                self.demoList.addTopLevelItem(item)
                if previous and str(path) == previous:
                    restore = item
        finally:
            self.demoList.setUpdatesEnabled(True)
        self._demos_loaded = True
        self._demos_dirty = False
        self.demoCountLabel.setText(f"{len(files)} demo{'s' if len(files) != 1 else ''}")
        if restore:
            self.demoList.setCurrentItem(restore)
        elif self.demoList.topLevelItemCount():
            self.demoList.setCurrentItem(self.demoList.topLevelItem(0))
        self._update_demo_play_button()
        self._update_demo_details()

    def play_selected_demo(self):
        if not q3elite_is_installed():
            self.demoStatusLabel.setText("Install Quake 3 Elite before playing demos.")
            self._update_demo_play_button()
            return
        item = self.demoList.currentItem()
        if not item:
            self.demoStatusLabel.setText("Select a demo first.")
            return
        path = Path(item.data(0, QtCore.Qt.ItemDataRole.UserRole))
        if not path.is_file():
            self.demoStatusLabel.setText("Demo file no longer exists.")
            return
        launcher_bat = GAME_ROOT / "Q3Elite" / "Engines" / "Q3Elite (Vulkan) - Cinematic.bat"
        if not launcher_bat.is_file():
            self.qerror(f"Q3Elite Vulkan launcher was not found:\n{launcher_bat}")
            return
        try:
            import subprocess
            # Quake resolves demos from osp/demos, so pass the demo filename.
            subprocess.Popen(
                [str(launcher_bat), "+demo", path.name],
                cwd=str(launcher_bat.parent),
                shell=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.demoStatusLabel.setText(f"Playing {path.name}")
        except Exception as error:
            self.qerror(f"Could not play demo:\n{error}")

    # ------------------------------------------------------------------
    # SERVERS — native Quake 3 UDP monitor
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # MATCHMAKING — rule based server notifications
    # ------------------------------------------------------------------
    def _load_matchmaking_rules(self):
        try:
            if MATCHMAKING_FILE.is_file():
                raw = json.loads(MATCHMAKING_FILE.read_text(encoding="utf-8"))
                rules = raw.get("rules", raw) if isinstance(raw, dict) else raw
                if isinstance(rules, list):
                    return [r for r in rules if isinstance(r, dict)]
        except Exception as error:
            print(f"[matchmaking] Could not read rules: {error}")
        return []

    def _save_matchmaking_rules(self):
        MATCHMAKING_FILE.parent.mkdir(parents=True, exist_ok=True)
        MATCHMAKING_FILE.write_text(json.dumps({"rules": self.matchmakingRules}, indent=2, ensure_ascii=False), encoding="utf-8")

    def _matchmaking_servers(self):
        path = getattr(self, "serverConfigPath", USER_SERVERS_FILE)
        return server_monitor.load_servers(path)

    def _build_matchmaking(self):
        page = QtWidgets.QWidget()
        page.setObjectName("matchmakingPage")
        self.matchmakingRealPage = page
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(4, 4, 4, 4)
        header = QtWidgets.QHBoxLayout()
        title = MetallicLabel("MATCHMAKING")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        add = GlowButton("+  ADD RULE")
        add.setObjectName("serverToolbarButton")
        add.clicked.connect(self.add_matchmaking_rule)
        header.addWidget(add)
        outer.addLayout(header)
        flow = self._card("matchmakingIntroCard")
        flow.setProperty("materialTextureKey", "material.matchmaking.card_texture_path")
        flow_l = QtWidgets.QHBoxLayout(flow)
        flow_l.setContentsMargins(18, 14, 18, 14)
        flow_l.setSpacing(12)
        steps = (
            ("01", "SELECT SERVERS", "Choose which servers the rule watches."),
            ("02", "SET CONDITIONS", "Player count, map and player-name filters are combined."),
            ("03", "GET NOTIFIED", "A notification fires only when the rule becomes matched."),
        )
        for n, heading, detail in steps:
            step = SurfaceCard(); step.setObjectName("matchmakingStep")
            sl = QtWidgets.QHBoxLayout(step); sl.setContentsMargins(12, 10, 12, 10); sl.setSpacing(10)
            badge = SilverNumberBadge(n); badge.setObjectName("matchmakingStepNumber"); sl.addWidget(badge)
            tx = QtWidgets.QVBoxLayout(); tx.setSpacing(1)
            ht = MetallicLabel(heading); ht.setObjectName("matchmakingStepTitle"); tx.addWidget(ht)
            dt = QtWidgets.QLabel(detail); dt.setObjectName("muted"); dt.setWordWrap(True); tx.addWidget(dt)
            sl.addLayout(tx,1); flow_l.addWidget(step,1)
        # Keep the non-scrolling intro aligned with the scroll viewport rather
        # than extending beneath the vertical scrollbar lane.
        flow_wrap = QtWidgets.QWidget()
        flow_wrap.setObjectName("matchmakingIntroWrap")
        flow_wrap_l = QtWidgets.QHBoxLayout(flow_wrap)
        self.matchmakingIntroWrapLayout = flow_wrap_l
        flow_wrap_l.setContentsMargins(0, 0, 18, 0)
        flow_wrap_l.addWidget(flow)
        outer.addWidget(flow_wrap)
        self.matchmakingScroll = QtWidgets.QScrollArea()
        self.matchmakingScroll.setWidgetResizable(True)
        self.matchmakingScroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.matchmakingScroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Matchmaking contains many transparent/material child controls. On a
        # translucent top-level window, a transparent scroll viewport can make
        # Qt propagate every exposed scroll strip all the way back to the shell.
        # Give Obsidian an opaque paint barrier so scrolling reuses viewport
        # pixels instead of recompositing the launcher background.
        mm_viewport = self.matchmakingScroll.viewport()
        mm_viewport.setObjectName("matchmakingViewport")
        self.matchmakingViewport = mm_viewport
        self.matchmakingBody = QtWidgets.QWidget()
        self.matchmakingBody.setObjectName("matchmakingScrollBody")
        matchmaking_opaque = OBSIDIAN_MATERIAL.enabled(ui_theme) and theme_bool("performance.opaque_matchmaking_viewport", True)
        for surface in (page, mm_viewport, self.matchmakingBody):
            self._set_opaque_scroll_surface(surface, matchmaking_opaque)
        self.matchmakingLayout = QtWidgets.QVBoxLayout(self.matchmakingBody)
        self.matchmakingLayout.setContentsMargins(0, 8, 18, 4)
        self.matchmakingLayout.setSpacing(10)
        self.matchmakingScroll.setWidget(self.matchmakingBody)
        self._optimize_heavy_scroll_area(self.matchmakingScroll)
        outer.addWidget(self.matchmakingScroll, 1)
        self.matchmakingRules = self._load_matchmaking_rules()
        self.matchmakingRuleStates = {}
        self.matchmakingResults = {}
        self.matchmakingWorker = None
        self.matchmakingStartedAt = time.monotonic()
        self.matchmakingBadgesHiddenUntil = 0.0
        self.matchmakingSound = None
        self.matchmakingAudio = None
        if QtMultimedia is not None:
            try:
                self.matchmakingAudio = QtMultimedia.QAudioOutput(self)
                self.matchmakingAudio.setVolume(1.0)
                self.matchmakingSound = QtMultimedia.QMediaPlayer(self)
                self.matchmakingSound.setAudioOutput(self.matchmakingAudio)
                self.matchmakingSound.setSource(QtCore.QUrl.fromLocalFile(str(NOTIFY_SOUND)))
            except Exception as error:
                print(f"[matchmaking] Sound unavailable: {error}")
        self.matchmakingTimer = QtCore.QTimer(self)
        self.matchmakingTimer.setInterval(30000)
        self.matchmakingTimer.timeout.connect(self.refresh_matchmaking)
        self.matchmakingTimer.start()
        self._rebuild_matchmaking_rules()
        QtCore.QTimer.singleShot(1500, self.refresh_matchmaking)
        return page

    def _server_choices_widget(self, selected):
        host = SurfaceCard()
        host.setObjectName("matchmakingServerChoices")
        host.setProperty("materialRadius", theme_float("material.matchmaking.inset_radius", 7.0))
        host.setProperty("materialTextureKey", "material.matchmaking.inset_texture_path")
        lay = QtWidgets.QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        checks = []
        for number, server in enumerate(self._matchmaking_servers(), 1):
            address = str(server.get("address", ""))
            alias = _q3_plain_ascii(server.get("name", "")) or address
            box = MaterialCheckBox(f"#{number}  {alias}  ({address})")
            box.setProperty("serverNumber", number)
            box.setChecked(number in selected)
            lay.addWidget(box)
            checks.append(box)
        if not checks:
            lay.addWidget(QtWidgets.QLabel("No servers in servers.json"))
        return host, checks

    def _rebuild_matchmaking_rules(self):
        while self.matchmakingLayout.count():
            item = self.matchmakingLayout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        if not self.matchmakingRules:
            empty = QtWidgets.QLabel("No matchmaking rules yet.\nPress + ADD RULE to create one.")
            empty.setObjectName("serverEmpty")
            empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.matchmakingLayout.addWidget(empty)
        self.matchmakingEditors = []
        for idx, rule in enumerate(self.matchmakingRules):
            card = self._card("matchmakingRuleCard")
            card.setProperty("materialTextureKey", "material.matchmaking.card_texture_path")
            v = QtWidgets.QVBoxLayout(card)
            v.setContentsMargins(18, 16, 18, 16)
            v.setSpacing(10)
            top = QtWidgets.QHBoxLayout()
            active = ToggleSwitch("ACTIVE")
            active.setObjectName("matchmakingActive")
            active.setChecked(bool(rule.get("active", False)))
            top.addWidget(active)
            alias = QtWidgets.QLineEdit(str(rule.get("alias", f"Rule #{idx+1}")))
            alias.setPlaceholderText(f"Rule #{idx+1} alias")
            alias.setMinimumWidth(360)
            alias.setMaximumWidth(560)
            top.addWidget(alias)
            status = QtWidgets.QLabel("NOT MATCHED")
            status.setObjectName("matchmakingStatus")
            status.setProperty("matched", False)
            top.addWidget(status)
            top.addStretch(1)
            delete = MaterialPushButton("DELETE RULE")
            delete.setObjectName("dangerButton")
            delete.clicked.connect(lambda checked=False, i=idx: self.delete_matchmaking_rule(i))
            top.addWidget(delete)
            v.addLayout(top)

            watch_title = QtWidgets.QLabel("WATCH THESE SERVERS")
            watch_title.setObjectName("matchmakingSectionTitle")
            v.addWidget(watch_title)
            server_mode_row = QtWidgets.QHBoxLayout()
            server_mode_row.addWidget(QtWidgets.QLabel("MATCH MODE"))
            server_mode = QtWidgets.QComboBox()
            server_mode.addItem("ANY selected server (OR)", "or")
            server_mode.addItem("ALL selected servers (AND)", "and")
            server_mode.setCurrentIndex(max(0, server_mode.findData(str(rule.get("server_mode", "or")).lower())))
            server_mode_row.addWidget(server_mode)
            server_mode_row.addStretch(1)
            v.addLayout(server_mode_row)
            servers_host, checks = self._server_choices_widget(set(int(x) for x in rule.get("servers", []) if str(x).isdigit()))
            v.addWidget(servers_host)

            # Conditions inside one server are combined with AND. Each one can be disabled.
            when_title = QtWidgets.QLabel("WHEN ALL ENABLED CONDITIONS ARE TRUE")
            when_title.setObjectName("matchmakingSectionTitle")
            v.addWidget(when_title)
            conditions_host = SurfaceCard(); conditions_host.setObjectName("matchmakingConditions")
            conditions_host.setProperty("materialRadius", theme_float("material.matchmaking.inset_radius", 7.0))
            conditions_host.setProperty("materialTextureKey", "material.matchmaking.inset_texture_path")
            conditions_box = QtWidgets.QVBoxLayout(conditions_host)
            conditions_box.setContentsMargins(12, 10, 12, 10)
            conditions_box.setSpacing(7)

            players_row = QtWidgets.QHBoxLayout()
            players_row.addWidget(QtWidgets.QLabel("PLAYERS"))
            min_on = MaterialCheckBox("More than")
            min_on.setChecked(bool(rule.get("min_enabled", True)))
            players_row.addWidget(min_on)
            minp = QtWidgets.QSpinBox(); minp.setRange(0, 128); minp.setValue(int(rule.get("min_players", 3))); minp.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons); minp.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter); minp.setFixedWidth(74); players_row.addWidget(minp)
            max_on = MaterialCheckBox("Less than")
            max_on.setChecked(bool(rule.get("max_enabled", True)))
            players_row.addWidget(max_on)
            maxp = QtWidgets.QSpinBox(); maxp.setRange(0, 128); maxp.setValue(int(rule.get("max_players", 6))); maxp.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons); maxp.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter); maxp.setFixedWidth(74); players_row.addWidget(maxp)
            players_row.addStretch(1)
            conditions_box.addLayout(players_row)

            map_conditions_host = QtWidgets.QWidget()
            map_conditions_layout = QtWidgets.QVBoxLayout(map_conditions_host)
            map_conditions_layout.setContentsMargins(0, 0, 0, 0)
            map_conditions_layout.setSpacing(5)
            map_rows = []

            def add_map_condition(value=None):
                value = value or {}
                row_widget = QtWidgets.QWidget()
                r = QtWidgets.QHBoxLayout(row_widget); r.setContentsMargins(0, 0, 0, 0)
                enabled = MaterialCheckBox(f"MAP NAME [{len(map_rows)+1}]")
                enabled.setChecked(bool(value.get("enabled", True))); r.addWidget(enabled)
                op = QtWidgets.QComboBox(); op.addItem("=", "eq"); op.addItem("!=", "neq")
                op.setCurrentIndex(max(0, op.findData(value.get("operator", "eq")))); r.addWidget(op)
                edit = QtWidgets.QLineEdit(str(value.get("name", ""))); edit.setPlaceholderText("map name"); r.addWidget(edit, 1)
                remove = MaterialPushButton("×"); remove.setObjectName("smallButton"); remove.setFixedSize(32, 32); r.addWidget(remove)
                entry = {"widget":row_widget,"enabled":enabled,"op":op,"text":edit}
                map_rows.append(entry)
                map_conditions_layout.addWidget(row_widget)
                def remove_row():
                    if entry in map_rows: map_rows.remove(entry)
                    row_widget.deleteLater()
                    for n, item in enumerate(map_rows, 1): item["enabled"].setText(f"MAP NAME [{n}]")
                remove.clicked.connect(remove_row)

            old_map_conditions = rule.get("map_conditions", [])
            # Migrate v3's single map condition to the multi-condition format.
            if not old_map_conditions and rule.get("map_enabled", False):
                old_map_conditions = [{"enabled": True, "operator": rule.get("map_operator", "eq"), "name": rule.get("map_name", "")}]
            for condition in old_map_conditions:
                if isinstance(condition, dict): add_map_condition(condition)

            add_map = MaterialPushButton("+ MAP CONDITION")
            add_map.setObjectName("secondaryButton")
            # Bind this rule's local function now; otherwise Python's loop closure
            # would make every button call the function from the last rule.
            add_map.clicked.connect(lambda checked=False, fn=add_map_condition: fn())
            conditions_box.addWidget(map_conditions_host)
            conditions_box.addWidget(add_map, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

            player_conditions_host = QtWidgets.QWidget()
            player_conditions_layout = QtWidgets.QVBoxLayout(player_conditions_host)
            player_conditions_layout.setContentsMargins(0, 0, 0, 0)
            player_conditions_layout.setSpacing(5)
            player_rows = []

            def add_player_condition(value=None):
                value = value or {}
                row_widget = QtWidgets.QWidget()
                r = QtWidgets.QHBoxLayout(row_widget); r.setContentsMargins(0, 0, 0, 0)
                enabled = MaterialCheckBox(f"PLAYER NAME [{len(player_rows)+1}]")
                enabled.setChecked(bool(value.get("enabled", True))); r.addWidget(enabled)
                op = QtWidgets.QComboBox(); op.addItem("=", "eq"); op.addItem("!=", "neq")
                op.setCurrentIndex(max(0, op.findData(value.get("operator", "eq")))); r.addWidget(op)
                edit = QtWidgets.QLineEdit(str(value.get("name", ""))); edit.setPlaceholderText("player name"); r.addWidget(edit, 1)
                remove = MaterialPushButton("×"); remove.setObjectName("smallButton"); remove.setFixedSize(32, 32); r.addWidget(remove)
                entry = {"widget":row_widget,"enabled":enabled,"op":op,"text":edit}
                player_rows.append(entry)
                player_conditions_layout.addWidget(row_widget)
                def remove_row():
                    if entry in player_rows: player_rows.remove(entry)
                    row_widget.deleteLater()
                    for n, item in enumerate(player_rows, 1): item["enabled"].setText(f"PLAYER NAME [{n}]")
                remove.clicked.connect(remove_row)

            old_player_conditions = rule.get("player_conditions", [])
            # Migrate the old single map/player text rule into a player condition when possible.
            if not old_player_conditions and rule.get("type") == "text" and rule.get("text"):
                old_player_conditions = [{"enabled": True, "operator": "eq", "name": rule.get("text", "")}]
            for condition in old_player_conditions:
                if isinstance(condition, dict): add_player_condition(condition)

            add_player = MaterialPushButton("+ PLAYER CONDITION")
            add_player.setObjectName("secondaryButton")
            add_player.clicked.connect(lambda checked=False, fn=add_player_condition: fn())
            conditions_box.addWidget(player_conditions_host)
            conditions_box.addWidget(add_player, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

            v.addWidget(conditions_host)
            then_title = QtWidgets.QLabel("THEN NOTIFY")
            then_title.setObjectName("matchmakingSectionTitle")
            v.addWidget(then_title)
            notify_row = QtWidgets.QHBoxLayout()
            notify_row.addWidget(QtWidgets.QLabel("ACCENT"))
            color = QtWidgets.QComboBox()
            for name, value in (("Blue", "#3b82f6"), ("White", "#ffffff"), ("Red", "#e53935"), ("Green", "#4caf50"), ("Orange", "#f0a51a")):
                color.addItem(name, value)
            color.setCurrentIndex(max(0, color.findData(rule.get("color", "#3b82f6"))))
            notify_row.addWidget(color)
            sound = MaterialCheckBox("notify.mp3"); sound.setChecked(bool(rule.get("sound", False))); notify_row.addWidget(sound)
            notify_row.addStretch(1)
            v.addLayout(notify_row)

            save = MaterialPushButton("SAVE RULE")
            save.setObjectName("secondaryButton")
            v.addWidget(save, 0, QtCore.Qt.AlignmentFlag.AlignRight)
            editor = {"active":active,"alias":alias,"server_mode":server_mode,"checks":checks,"min_on":min_on,"min":minp,"max_on":max_on,"max":maxp,"map_rows":map_rows,"player_rows":player_rows,"color":color,"sound":sound,"status":status}
            self.matchmakingEditors.append(editor)
            min_on.toggled.connect(minp.setEnabled); max_on.toggled.connect(maxp.setEnabled)
            minp.setEnabled(min_on.isChecked()); maxp.setEnabled(max_on.isChecked())
            save.clicked.connect(lambda checked=False, i=idx: self.save_matchmaking_rule(i))
            self.matchmakingLayout.addWidget(card)
        self.matchmakingLayout.addStretch(1)
        self._update_matchmaking_status_ui()

    def add_matchmaking_rule(self):
        number = len(self.matchmakingRules) + 1
        self.matchmakingRules.append({"id":uuid.uuid4().hex,"alias":f"Rule #{number}","active":False,"servers":[],"server_mode":"or","min_enabled":True,"min_players":3,"max_enabled":True,"max_players":6,"map_enabled":False,"map_operator":"eq","map_name":"","player_conditions":[],"color":"#3b82f6","sound":False})
        self._save_matchmaking_rules(); self._rebuild_matchmaking_rules()

    def delete_matchmaking_rule(self, index):
        if 0 <= index < len(self.matchmakingRules):
            self.matchmakingRules.pop(index); self._save_matchmaking_rules(); self._rebuild_matchmaking_rules(); self.refresh_matchmaking()

    def save_matchmaking_rule(self, index):
        if not (0 <= index < len(self.matchmakingRules)): return
        e = self.matchmakingEditors[index]
        old = self.matchmakingRules[index]
        self.matchmakingRules[index] = {"id":old.get("id") or uuid.uuid4().hex,"alias":e["alias"].text().strip() or f"Rule #{index+1}","active":e["active"].isChecked(),"servers":[int(b.property("serverNumber")) for b in e["checks"] if b.isChecked()],"server_mode":e["server_mode"].currentData(),"min_enabled":e["min_on"].isChecked(),"min_players":e["min"].value(),"max_enabled":e["max_on"].isChecked(),"max_players":e["max"].value(),"map_conditions":[{"enabled":r["enabled"].isChecked(),"operator":r["op"].currentData(),"name":r["text"].text().strip()} for r in e["map_rows"]],"player_conditions":[{"enabled":r["enabled"].isChecked(),"operator":r["op"].currentData(),"name":r["text"].text().strip()} for r in e["player_rows"]],"color":e["color"].currentData(),"sound":e["sound"].isChecked()}
        self._save_matchmaking_rules(); self.matchmakingRuleStates[self.matchmakingRules[index]["id"]] = False; self._update_matchmaking_status_ui(); self.refresh_matchmaking()

    def refresh_matchmaking(self):
        active = [r for r in self.matchmakingRules if r.get("active") and r.get("servers")]
        if not active or (self.matchmakingWorker is not None and self.matchmakingWorker.isRunning()):
            if not active: self._update_matchmaking_status_ui()
            return
        servers = self._matchmaking_servers()
        needed = sorted({int(n) for r in active for n in r.get("servers", []) if 1 <= int(n) <= len(servers)})
        selected = []
        self._matchmakingWorkerNumbers = needed
        for n in needed: selected.append(dict(servers[n-1]))
        if not selected: return
        self.matchmakingWorker = ServerQueryWorker(selected, self)
        self.matchmakingWorker.serverReady.connect(self._matchmaking_result_ready)
        self.matchmakingWorker.batchFinished.connect(self._matchmaking_refresh_finished)
        self.matchmakingWorker.start()

    def _matchmaking_result_ready(self, index, result):
        if 0 <= index < len(getattr(self, "_matchmakingWorkerNumbers", [])):
            self.matchmakingResults[self._matchmakingWorkerNumbers[index]] = result

    def _matchmaking_refresh_finished(self):
        self._evaluate_matchmaking_rules()

    def _rule_matches(self, rule):
        selected = [int(n) for n in rule.get("servers", [])]
        if not selected:
            return False

        def server_matches(number):
            result = self.matchmakingResults.get(number)
            if not result or not result.get("online"):
                return False
            conditions = []
            count = int(result.get("clients", 0) or 0)
            if rule.get("min_enabled", True):
                conditions.append(count > int(rule.get("min_players", 3)))
            if rule.get("max_enabled", True):
                conditions.append(count < int(rule.get("max_players", 6)))

            actual_map = str(result.get("mapname", "")).casefold().strip()
            map_conditions = rule.get("map_conditions", [])
            # Compatibility with v3 single-map rules.
            if not map_conditions and rule.get("map_enabled", False):
                map_conditions = [{"enabled": True, "operator": rule.get("map_operator", "eq"), "name": rule.get("map_name", "")}]
            for mc in map_conditions:
                if not isinstance(mc, dict) or not mc.get("enabled", True):
                    continue
                wanted = str(mc.get("name", "")).casefold().strip()
                if not wanted:
                    conditions.append(False)
                    continue
                equal = actual_map == wanted
                conditions.append(equal if mc.get("operator", "eq") == "eq" else not equal)

            players = [_q3_plain_ascii(p.get("name", "")).casefold().strip() for p in (result.get("players", []) or [])]
            player_conditions = rule.get("player_conditions", [])
            # Compatibility with v1 text rules.
            if not player_conditions and rule.get("type") == "text" and rule.get("text"):
                player_conditions = [{"enabled": True, "operator": "eq", "name": rule.get("text", "")}]
            for pc in player_conditions:
                if not isinstance(pc, dict) or not pc.get("enabled", True):
                    continue
                wanted = _q3_plain_ascii(pc.get("name", "")).casefold().strip()
                if not wanted:
                    conditions.append(False)
                    continue
                present = any(name == wanted for name in players)
                conditions.append(present if pc.get("operator", "eq") == "eq" else not present)

            # All enabled arguments inside a single rule/server are ANDed.
            return bool(conditions) and all(conditions)

        states = [server_matches(number) for number in selected]
        if str(rule.get("server_mode", "or")).lower() == "and":
            return bool(states) and all(states)
        return any(states)

    def _evaluate_matchmaking_rules(self):
        for rule in self.matchmakingRules:
            rid = rule.get("id") or str(id(rule))
            matched = bool(rule.get("active")) and self._rule_matches(rule)
            previous = self.matchmakingRuleStates.get(rid, False)
            self.matchmakingRuleStates[rid] = matched
            if matched and not previous and rule.get("sound"):
                suppress = launcher_settings.get("suppress_startup_notification_sound", True) and (time.monotonic() - self.matchmakingStartedAt < 60)
                if not suppress and self.matchmakingSound is not None and NOTIFY_SOUND.is_file():
                    self.matchmakingSound.play()
        self._update_matchmaking_status_ui()

    def _badge_icon(self, colors):
        """Render up to three simultaneous rule badges in the sidebar icon."""
        colors = list(colors)[:3]
        pix = QtGui.QPixmap(24, 18)
        pix.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pix)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        positions = {1: [12], 2: [8, 16], 3: [5, 12, 19]}.get(len(colors), [])
        for x, color in zip(positions, colors):
            painter.setBrush(QtGui.QColor(color))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawEllipse(QtCore.QPointF(x, 9), 4.0, 4.0)
        painter.end()
        return QtGui.QIcon(pix)

    def _update_matchmaking_status_ui(self):
        matches = []
        for i, rule in enumerate(self.matchmakingRules):
            rid = rule.get("id") or str(id(rule))
            if self.matchmakingRuleStates.get(rid, False):
                matches.append((i, rule))
        badges_hidden = time.monotonic() < getattr(self, "matchmakingBadgesHiddenUntil", 0.0)
        if matches and not badges_hidden:
            visible = matches[:3]
            self.matchmakingNav.setIcon(self._badge_icon([r.get("color", "#3b82f6") for _, r in visible]))
            aliases = [r.get("alias") or f"Rule #{i+1}" for i, r in matches]
            extra = len(matches) - len(visible)
            tip = "Matched: " + ", ".join(aliases[:3])
            if extra > 0:
                tip += f" (+{extra} more)"
            self.matchmakingNav.setToolTip(tip)
        else:
            if qta is not None:
                try: self.matchmakingNav.setIcon(qta.icon("fa5s.bell", color='#b8b8b8', color_active='#b00000'))
                except Exception: pass
            self.matchmakingNav.setToolTip("")
        for i, e in enumerate(getattr(self, "matchmakingEditors", [])):
            rule = self.matchmakingRules[i]
            rid = rule.get("id") or str(id(rule))
            matched = self.matchmakingRuleStates.get(rid, False)
            e["status"].setText("MATCHED" if matched else "NOT MATCHED")
            e["status"].setProperty("matched", bool(matched))
            e["status"].style().unpolish(e["status"])
            e["status"].style().polish(e["status"])

    def _build_maps(self):
        page=QtWidgets.QWidget(); page.setObjectName("mapsPage")
        root=QtWidgets.QVBoxLayout(page); root.setContentsMargins(4,4,4,4); root.setSpacing(9)
        header=QtWidgets.QHBoxLayout()
        title=MetallicLabel("MAPS"); title.setObjectName("pageTitle"); header.addWidget(title)
        self.mapsLocalTab=GlowButton("LOCAL"); self.mapsLocalTab.setObjectName("mapModeButton"); self.mapsLocalTab.setProperty("active",True)
        self.mapsOnlineTab=GlowButton("ONLINE"); self.mapsOnlineTab.setObjectName("mapModeButton"); self.mapsOnlineTab.setProperty("active",False)
        self.mapsLocalTab.clicked.connect(lambda:self.set_maps_mode("local")); self.mapsOnlineTab.clicked.connect(lambda:self.set_maps_mode("online"))
        header.addWidget(self.mapsLocalTab); header.addWidget(self.mapsOnlineTab); header.addStretch(1)
        self.mapsStatus=QtWidgets.QLabel("Local map catalog"); self.mapsStatus.setObjectName("muted"); header.addWidget(self.mapsStatus)
        self.mapsRefreshButton=GlowButton("↻  REFRESH MAPS"); self.mapsRefreshButton.setObjectName("mapToolbarButton")
        self.mapsRefreshButton.clicked.connect(lambda:self.refresh_maps(force=True)); header.addWidget(self.mapsRefreshButton)
        root.addLayout(header)
        self.mapsStack=QtWidgets.QStackedWidget(); root.addWidget(self.mapsStack,1)

        local=QtWidgets.QWidget(); ll=QtWidgets.QVBoxLayout(local); ll.setContentsMargins(0,0,0,0); ll.setSpacing(8)
        c=QtWidgets.QHBoxLayout(); self.mapsSearch=QtWidgets.QLineEdit(); self.mapsSearch.setObjectName("mapsSearch")
        self.mapsSearch.setPlaceholderText("Search local maps…   Ctrl+F"); self.mapsSearch.setClearButtonEnabled(True); self.mapsSearch.textChanged.connect(self._filter_maps_table); c.addWidget(self.mapsSearch,1)
        self.mapsSortButton=GlowButton("SORT: NAME"); self.mapsSortButton.setObjectName("mapToolbarButton"); self.mapsSortButton.setProperty("active",False); self.mapsSortButton.clicked.connect(self.cycle_maps_gametype_sort); c.addWidget(self.mapsSortButton)
        self.mapsLocationButton=GlowButton("ALL"); self.mapsLocationButton.setObjectName("mapToolbarButton"); self.mapsLocationButton.clicked.connect(self.cycle_maps_location_filter); c.addWidget(self.mapsLocationButton); ll.addLayout(c)
        self.mapsHint=QtWidgets.QLabel("F1 Shortcuts   ↑↓ Select   O Select PAK   Ctrl+F Search   Enter Play   Del Delete"); self.mapsHint.setObjectName("mapsHint"); ll.addWidget(self.mapsHint)
        self.mapsKeys=QtWidgets.QLabel("KEY BINDS\n↑ / ↓ : Navigation\nEnter : Play\nO : Select PAK\nDel : Delete PAK\nCtrl+F : Search\nF3 : Sort\nF4 : Location\nF1 : Show/Hide"); self.mapsKeys.setObjectName("keyBindsLegend"); self.mapsKeys.hide(); ll.addWidget(self.mapsKeys)
        self.mapsTable=QtWidgets.QTableWidget(0,6); self.mapsTable.setObjectName("mapsTable"); self.mapsTable.setHorizontalHeader(MaterialHeaderView(QtCore.Qt.Orientation.Horizontal, self.mapsTable)); self.mapsTable.setHorizontalHeaderLabels(["LEVELSHOT","MAP","PAK","SIZE","GAMETYPE",""])
        self.mapsTable.verticalHeader().setVisible(False); self.mapsTable.setShowGrid(False); self.mapsTable.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows); self.mapsTable.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection); self.mapsTable.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers); self.mapsTable.setIconSize(QtCore.QSize(144,81)); self.mapsTable.setMouseTracking(True); self.mapsTable.viewport().setMouseTracking(True); self.mapsTable.setWordWrap(False); self.mapsTable.setItemDelegate(MapRowHoverDelegate(self.mapsTable))
        h=self.mapsTable.horizontalHeader(); h.setSectionResizeMode(0,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(0,164); h.setSectionResizeMode(1,QtWidgets.QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(2,QtWidgets.QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(3,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(3,theme_int("maps.size_column",100)); h.setSectionResizeMode(4,QtWidgets.QHeaderView.ResizeMode.ResizeToContents); h.setSectionResizeMode(5,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(5,theme_int("maps.local_actions_column",188))
        ll.addWidget(self.mapsTable,1); self.mapsStack.addWidget(local)

        online=QtWidgets.QWidget(); ol=QtWidgets.QVBoxLayout(online); ol.setContentsMargins(0,0,0,0); ol.setSpacing(8)
        self.onlineMapsSearch=QtWidgets.QLineEdit(); self.onlineMapsSearch.setObjectName("mapsSearch"); self.onlineMapsSearch.setPlaceholderText("Search LvLWorld + Worldspawn maps…   Ctrl+F"); self.onlineMapsSearch.setClearButtonEnabled(True); ol.addWidget(self.onlineMapsSearch)
        self.onlineMapsHeading=QtWidgets.QLabel("LATEST MAPS — LvLWorld"); self.onlineMapsHeading.setObjectName("onlineMapsHeading"); ol.addWidget(self.onlineMapsHeading)
        self.onlineMapsTable=QtWidgets.QTableWidget(0,7); self.onlineMapsTable.setObjectName("mapsTable"); self.onlineMapsTable.setHorizontalHeader(MaterialHeaderView(QtCore.Qt.Orientation.Horizontal, self.onlineMapsTable)); self.onlineMapsTable.setHorizontalHeaderLabels(["LEVELSHOT","MAP","PAKNAME","SIZE","RELEASED","SOURCE",""])
        self.onlineMapsTable.verticalHeader().setVisible(False); self.onlineMapsTable.verticalHeader().setDefaultSectionSize(92); self.onlineMapsTable.setShowGrid(False); self.onlineMapsTable.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows); self.onlineMapsTable.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection); self.onlineMapsTable.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers); self.onlineMapsTable.setIconSize(QtCore.QSize(144,81)); self.onlineMapsTable.setMouseTracking(True); self.onlineMapsTable.viewport().setMouseTracking(True); self.onlineMapsTable.setWordWrap(False); self.onlineMapsTable.setItemDelegate(MapRowHoverDelegate(self.onlineMapsTable))
        h=self.onlineMapsTable.horizontalHeader(); h.setSectionResizeMode(0,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(0,164); h.setSectionResizeMode(1,QtWidgets.QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(2,QtWidgets.QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(3,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(3,theme_int("maps.size_column",100)); h.setSectionResizeMode(4,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(4,110); h.setSectionResizeMode(5,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(5,78); h.setSectionResizeMode(6,QtWidgets.QHeaderView.ResizeMode.Fixed); h.resizeSection(6,theme_int("maps.online_actions_column",194))
        ol.addWidget(self.onlineMapsTable,1)
        self.onlineLoadMore=GlowButton("LOAD MORE"); self.onlineLoadMore.setObjectName("mapToolbarButton"); self.onlineLoadMore.clicked.connect(self._load_more_online_maps); ol.addWidget(self.onlineLoadMore,0,QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.mapsStack.addWidget(online)

        self.localMaps=[]; self.mapCatalogWorker=None; self.onlineMapsWorker=None; self.onlineDownloadWorkers=[]; self.onlineDownloadByKey={}; self.mapsGametypeSort=0; self.mapsLocationFilter="All"; self.mapsMode="local"; self.onlineLatestLoaded=False; self.onlineSearchSerial=0; self.onlineLatestLimit=12; self._sourceFavicons={}; self._sourceFaviconWorkers=[]
        self._maps_loaded = False
        self.onlineSearchTimer=QtCore.QTimer(self); self.onlineSearchTimer.setSingleShot(True); self.onlineSearchTimer.setInterval(450); self.onlineSearchTimer.timeout.connect(self._run_online_search); self.onlineMapsSearch.textChanged.connect(self._online_search_changed)
        self.mapShortcuts=[]
        def shortcut(key,fn):
            q=QtGui.QShortcut(QtGui.QKeySequence(key),self); q.setContext(QtCore.Qt.ShortcutContext.WindowShortcut); q.activated.connect(fn); q.setEnabled(False); self.mapShortcuts.append(q)
        shortcut("F1",lambda:self.mapsKeys.setVisible(not self.mapsKeys.isVisible()) if self.mapsMode=="local" else None); shortcut("Up",lambda:self._step_map(-1) if self.mapsMode=="local" else self._step_online_map(-1)); shortcut("Down",lambda:self._step_map(1) if self.mapsMode=="local" else self._step_online_map(1)); shortcut("Return",lambda:self.play_selected_map() if self.mapsMode=="local" else self.activate_selected_online_map()); shortcut("O",lambda:self.open_selected_map_location() if self.mapsMode=="local" else self.open_selected_online_map_location()); shortcut("Delete",lambda:self.delete_selected_map_pak() if self.mapsMode=="local" else self.delete_selected_online_map()); shortcut("F3",lambda:self.cycle_maps_gametype_sort() if self.mapsMode=="local" else None); shortcut("F4",lambda:self.cycle_maps_location_filter() if self.mapsMode=="local" else None)
        self.mapsTable.itemDoubleClicked.connect(lambda _:self.play_selected_map())
        return page

    def set_maps_mode(self,mode):
        self.mapsMode="online" if mode=="online" else "local"; online=self.mapsMode=="online"; self.mapsStack.setCurrentIndex(1 if online else 0); self.mapsRefreshButton.setVisible(not online)
        for b,a in ((self.mapsLocalTab,not online),(self.mapsOnlineTab,online)):
            b.setProperty("active",a); b.style().unpolish(b); b.style().polish(b)
        if online:
            if not self.onlineLatestLoaded and not self.onlineMapsSearch.text().strip(): self._start_online_worker("latest","")
        else:self.mapsStatus.setText(f"{len(self.localMaps)} local map(s)")

    def _online_search_changed(self,text):
        q=text.strip(); self.onlineSearchTimer.stop()
        self._onlineThumbGeneration += 1
        if not q:
            self.onlineMapsHeading.setText("LATEST MAPS — LvLWorld"); self.onlineLatestLimit=12; self.onlineLoadMore.show(); self._start_online_worker("latest","")
        else:
            self.onlineMapsHeading.setText("SEARCH RESULTS — LvLWorld + Worldspawn"); self.onlineLoadMore.hide(); self.onlineMapsTable.setRowCount(0); self.onlineSearchTimer.start()

    def _run_online_search(self):
        q=self.onlineMapsSearch.text().strip()
        if q:self._start_online_worker("search",q)

    def _start_online_worker(self,mode,q,append=False):
        self.onlineSearchSerial+=1; serial=self.onlineSearchSerial; self.mapsStatus.setText("Searching LvLWorld + Worldspawn…" if mode=="search" else "Loading latest LvLWorld maps…")
        w = OnlineMapsWorker(mode, q, 1, self, self.onlineLatestLimit)
        self.onlineMapsWorker = w
        w.ready.connect(lambda data, src, meta, n=serial, a=append: self._online_ready(n, data, src, meta, a))
        w.failed.connect(lambda e, n=serial: self._online_failed(n, e))
        w.start()

    def _load_more_online_maps(self):
        if self.onlineMapsSearch.text().strip():return
        self.onlineLatestLimit += 12
        self._start_online_worker("latest","",append=True)

    def _online_failed(self, serial, error):
        if serial != self.onlineSearchSerial:
            return
        print(f"[online maps] ERROR: {error}")
        short = str(error).replace("\\n", " ").strip()
        if len(short) > 150:
            short = short[:147] + "..."
        self.mapsStatus.setText(f"Online error: {short}")
        self.onlineMapsTable.setRowCount(0)

    def _online_ready(self, serial, items, source, meta=None, append=False):
        if serial != self.onlineSearchSerial: return
        # Do not invalidate already loaded thumbnail workers when merely appending.
        if not append:self._onlineThumbGeneration += 1
        thumb_generation = self._onlineThumbGeneration
        if source in ("Worldspawn Index","LvLWorld"): self.onlineLatestLoaded = True
        total = int((meta or {}).get("total") or len(items))
        t=self.onlineMapsTable
        scroll_value=t.verticalScrollBar().value()
        if append:
            existing={_online_map_key(t.item(row,0).data(QtCore.Qt.ItemDataRole.UserRole))
                      for row in range(t.rowCount()) if t.item(row,0) is not None}
            items=[x for x in items if _online_map_key(x) not in existing]
        else:
            t.setRowCount(0)
        for data in items:
            row_source=str(data.get("source") or "")
            r=t.rowCount(); t.insertRow(r); t.setRowHeight(r,92)
            shot=QtWidgets.QTableWidgetItem(); shot.setData(QtCore.Qt.ItemDataRole.UserRole,data); t.setItem(r,0,shot)
            for col,val in ((1,data.get("name","")),(2,data.get("pak") or "—"),(3,data.get("size") or "—"),(4,data.get("released") or "—")):
                t.setItem(r,col,QtWidgets.QTableWidgetItem(str(val)))
            source_item=QtWidgets.QTableWidgetItem("")
            source_name = str(data.get("source") or "")
            source_item.setData(QtCore.Qt.ItemDataRole.UserRole, source_name)
            source_item.setToolTip(source_name)
            t.setItem(r,5,source_item)
            self._set_source_favicon_cell(r, source_name)
            self._ensure_source_favicon(source_name)
            self._set_online_row_action(r,data)
            if row_source=="LvLWorld" and data.get("lvl_id"):
                meta_worker=LvLWorldMetadataWorker(r,str(data.get("lvl_id")),thumb_generation,self)
                self._worldspawnShotWorkers.append(meta_worker)
                meta_worker.ready.connect(self._lvlworld_metadata_ready)
                previous_meta=None
                for candidate in reversed(self._worldspawnShotWorkers[:-1]):
                    if isinstance(candidate,LvLWorldMetadataWorker) and candidate.isRunning():
                        previous_meta=candidate; break
                if previous_meta is not None: previous_meta.finished.connect(meta_worker.start)
                else: meta_worker.start()
                meta_worker.finished.connect(lambda w=meta_worker:self._worldspawn_shot_worker_done(w))
            thumb=str(data.get("levelshot_url") or "")
            if thumb and row_source=="Worldspawn Index":
                worker=WorldspawnImageWorker(r,thumb,self)
                self._worldspawnShotWorkers.append(worker)
                worker.ready.connect(self._worldspawn_levelshot_url_ready)
                worker.finished.connect(lambda w=worker:self._worldspawn_shot_worker_done(w))
                worker.start()
            elif thumb and row_source=="LvLWorld":
                worker=LvLWorldImageWorker(r,thumb,thumb_generation,self)
                self._worldspawnShotWorkers.append(worker)
                worker.ready.connect(self._lvlworld_levelshot_ready)
                # Start LvLWorld images one-by-one: the server was refusing
                # simultaneous Qt HTTP/2 streams.
                previous=None
                for candidate in reversed(self._worldspawnShotWorkers[:-1]):
                    if isinstance(candidate,LvLWorldImageWorker) and candidate.isRunning():
                        previous=candidate; break
                if previous is not None:
                    previous.finished.connect(worker.start)
                else:
                    worker.start()
                worker.finished.connect(lambda w=worker:self._worldspawn_shot_worker_done(w))
            elif thumb:
                reply=self._mapNetwork.get(QtNetwork.QNetworkRequest(QtCore.QUrl(thumb)))
                reply.setProperty("online_map_row",r)
                reply.setProperty("online_map_generation",thumb_generation)
                reply.finished.connect(lambda rep=reply:self._online_levelshot_ready(rep))
            elif row_source=="Worldspawn Index" and data.get("defrag_map_name"):
                worker=DefragThumbnailWorker(r,str(data.get("defrag_map_name")),self)
                worker.resultGeneration=thumb_generation
                self._worldspawnShotWorkers.append(worker)
                worker.ready.connect(lambda row,url,w=worker:self._defrag_thumbnail_url_ready(row,url,getattr(w,"resultGeneration",-1)))
                worker.finished.connect(lambda w=worker:self._worldspawn_shot_worker_done(w))
                worker.start()
        shown=t.rowCount()
        self.mapsStatus.setText(f"{shown} shown • {total} indexed map(s) • {source}" if shown else f"No results • {source}")
        if hasattr(self,"onlineLoadMore"):
            self.onlineLoadMore.setVisible(not self.onlineMapsSearch.text().strip() and shown<total)
        if append:
            t.verticalScrollBar().setValue(scroll_value)

    def _set_map_thumbnail(self, table, row, pixmap):
        if not (0 <= row < table.rowCount()) or pixmap is None or pixmap.isNull():
            return
        label = table.cellWidget(row, 0)
        if not isinstance(label, AspectFillLabel):
            label = AspectFillLabel(16, 9, table)
            label.setObjectName("mapLevelshot16x9")
            label.setMinimumSize(1, 1)
            label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
            host = QtWidgets.QWidget(table)
            host.setObjectName("mapLevelshotHost")
            lay = QtWidgets.QHBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            lay.addWidget(label)
            table.setCellWidget(row, 0, host)
        label.setSourcePixmap(pixmap)

    def _set_online_row_action(self,row,data):
        if not (0 <= row < self.onlineMapsTable.rowCount()): return
        installed = _online_installed_files(data); key = _online_map_key(data); active = self.onlineDownloadByKey.get(key)
        host = QtWidgets.QWidget(); host.setObjectName("mapOnlineActions")
        al = QtWidgets.QHBoxLayout(host); al.setContentsMargins(0,0,0,0); al.setSpacing(theme_int("maps.action_gap", 8)); al.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        primary_w = theme_int("maps.primary_action_width", 116)
        delete_w = theme_int("maps.delete_action_width", 40)
        action_gap = theme_int("maps.action_gap", 8)
        total_w = primary_w + action_gap + delete_w
        host.setFixedWidth(total_w)
        if installed:
            play = MaterialPushButton("PLAY"); play.setObjectName("mapPlayButton"); play.setFixedWidth(primary_w); play.clicked.connect(lambda checked=False,x=dict(data):self.play_online_map(x))
            delete = MaterialPushButton(""); delete.setObjectName("mapDeleteIconButton"); delete.setFixedSize(delete_w,38); delete.setToolTip("Delete downloaded map")
            if qta is not None:
                try: delete.setIcon(qta.icon("ph.trash", color=theme_value("colors.semantic.danger", "#D45158")))
                except Exception: delete.setText("×")
            else: delete.setText("×")
            delete.clicked.connect(lambda checked=False,x=dict(data):self.delete_online_map(x)); al.addWidget(play); al.addWidget(delete)
        else:
            if active is not None and active.isRunning():
                action = MaterialPushButton("PAUSE"); action.clicked.connect(lambda checked=False,x=dict(data):self.pause_online_map(x))
            else:
                partial = _online_partial_path(data).is_file(); label = "LOCKED" if data.get("locked") else ("RESUME" if partial else "DOWNLOAD")
                action = MaterialPushButton(label); action.setEnabled(not data.get("locked")); action.clicked.connect(lambda checked=False,x=dict(data):self.download_online_map(x))
            action.setObjectName("mapDownloadButton"); action.setFixedWidth(total_w); al.addWidget(action)
        self.onlineMapsTable.setCellWidget(row,6,host)

    def _online_row_for_item(self,item):
        key=_online_map_key(item)
        for row in range(self.onlineMapsTable.rowCount()):
            cell=self.onlineMapsTable.item(row,0)
            data=cell.data(QtCore.Qt.ItemDataRole.UserRole) if cell else None
            if isinstance(data,dict) and _online_map_key(data)==key:
                return row
        return -1

    def _set_source_favicon_cell(self, row, source):
        if not (0 <= row < self.onlineMapsTable.rowCount()):
            return
        host = self.onlineMapsTable.cellWidget(row, 5)
        if host is None:
            host = QtWidgets.QWidget(self.onlineMapsTable)
            host.setObjectName("mapSourceIconHost")
            layout = QtWidgets.QHBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            label = QtWidgets.QLabel()
            label.setObjectName("mapSourceIcon")
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setFixedSize(24, 24)
            layout.addStretch(1)
            layout.addWidget(label)
            layout.addStretch(1)
            self.onlineMapsTable.setCellWidget(row, 5, host)
        else:
            label = host.findChild(QtWidgets.QLabel, "mapSourceIcon")
        if label is None:
            return
        label.setToolTip(str(source or ""))
        icon = self._sourceFavicons.get(str(source or ""))
        if icon is not None:
            label.setPixmap(icon.pixmap(18, 18))
        else:
            label.clear()

    def _ensure_source_favicon(self,source):
        if source not in ("LvLWorld","Worldspawn Index"):return
        if source in self._sourceFavicons:
            self._apply_source_favicon(source); return
        if any(getattr(w,"source","")==source for w in self._sourceFaviconWorkers):return
        w=SourceFaviconWorker(source,self); self._sourceFaviconWorkers.append(w)
        w.ready.connect(self._source_favicon_ready)
        w.finished.connect(lambda x=w:self._source_favicon_done(x))
        w.start()

    def _source_favicon_ready(self,source,data):
        pix=QtGui.QPixmap()
        if data and pix.loadFromData(bytes(data)):
            self._sourceFavicons[source]=_circular_icon_from_pixmap(pix, 18)
            self._apply_source_favicon(source)

    def _apply_source_favicon(self,source):
        icon=self._sourceFavicons.get(source)
        if icon is None:return
        for row in range(self.onlineMapsTable.rowCount()):
            item=self.onlineMapsTable.item(row,5)
            if item is not None and str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")==source:
                self._set_source_favicon_cell(row, source)

    def _source_favicon_done(self,w):
        try:self._sourceFaviconWorkers.remove(w)
        except ValueError:pass
        w.deleteLater()

    def _lvlworld_metadata_ready(self,row,meta,generation):
        if generation != self._onlineThumbGeneration:return
        if not isinstance(meta,dict) or not (0 <= row < self.onlineMapsTable.rowCount()):return
        for col,key in ((2,"pak"),(3,"size"),(4,"released")):
            value=str(meta.get(key) or "").strip()
            if value:
                item=self.onlineMapsTable.item(row,col)
                if item is not None:item.setText(value)
        shot=str(meta.get("levelshot_url") or "").strip()
        if shot:
            worker=LvLWorldImageWorker(row,shot,generation,self)
            self._worldspawnShotWorkers.append(worker)
            worker.ready.connect(self._lvlworld_levelshot_ready)
            worker.finished.connect(lambda w=worker:self._worldspawn_shot_worker_done(w))
            worker.start()

    def _lvlworld_levelshot_ready(self,row,data,generation):
        if generation != self._onlineThumbGeneration:return
        if not data or not (0 <= row < self.onlineMapsTable.rowCount()):return
        pix=QtGui.QPixmap()
        if not pix.loadFromData(bytes(data)):return
        self._set_map_thumbnail(self.onlineMapsTable, row, pix)

    def _worldspawn_shot_worker_done(self, worker):
        try:self._worldspawnShotWorkers.remove(worker)
        except ValueError:pass
        worker.deleteLater()

    def _worldspawn_levelshot_url_ready(self,row,data):
        if not data or not (0 <= row < self.onlineMapsTable.rowCount()):return
        pix=QtGui.QPixmap()
        if not pix.loadFromData(bytes(data)):return
        self._set_map_thumbnail(self.onlineMapsTable, row, pix)

    def _defrag_thumbnail_url_ready(self,row,url,generation=None):
        if generation is not None and generation != self._onlineThumbGeneration:return
        if not url or not (0 <= row < self.onlineMapsTable.rowCount()):return
        req=QtNetwork.QNetworkRequest(QtCore.QUrl(url))
        reply=self._mapNetwork.get(req)
        reply.setProperty("online_map_row",row)
        reply.setProperty("online_map_generation",self._onlineThumbGeneration if generation is None else generation)
        reply.finished.connect(lambda rep=reply:self._online_levelshot_ready(rep))

    def _online_levelshot_ready(self, reply):
        try:
            generation=reply.property("online_map_generation")
            if generation is not None and int(generation) != self._onlineThumbGeneration:return
            row=int(reply.property("online_map_row")); raw=bytes(reply.readAll()); pix=QtGui.QPixmap()
            if raw and pix.loadFromData(raw) and 0 <= row < self.onlineMapsTable.rowCount():
                self._set_map_thumbnail(self.onlineMapsTable, row, pix)
        finally:
            reply.deleteLater()

    def _step_online_map(self,delta):
        t=self.onlineMapsTable
        count=t.rowCount()
        if count<=0:return
        row=t.currentRow()
        if row<0:
            row=0 if delta>=0 else count-1
        else:
            row=max(0,min(count-1,row+delta))
        t.selectRow(row)
        item=t.item(row,1) or t.item(row,0)
        if item is not None:
            t.scrollToItem(item,QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)

    def activate_selected_online_map(self):
        data=self._selected_online_map_data()
        if not data:return
        key=_online_map_key(data)
        active=self.onlineDownloadByKey.get(key)
        if active is not None and active.isRunning():
            self.pause_online_map(data)
        elif _online_installed_files(data):
            self.play_online_map(data)
        elif not data.get("locked"):
            self.download_online_map(data)

    def delete_selected_online_map(self):
        data=self._selected_online_map_data()
        if not data:return
        if _online_installed_files(data):
            # Same immediate-delete path as the row DELETE button; no dialog.
            self.delete_online_map(data)

    def _selected_online_map_data(self):
        row=self.onlineMapsTable.currentRow()
        if row < 0:return None
        cell=self.onlineMapsTable.item(row,0)
        data=cell.data(QtCore.Qt.ItemDataRole.UserRole) if cell else None
        return data if isinstance(data,dict) else None

    def open_selected_online_map_location(self):
        data=self._selected_online_map_data()
        if not data:return
        installed=_online_installed_files(data)
        if not installed:return
        path=DOWNLOADED_MAPS_DIR/Path(installed[0]).name
        if not path.is_file():return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer.exe","/select,",str(path)])
            else:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.parent)))
        except Exception:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.parent)))

    def play_online_map(self,item):
        installed=_online_installed_files(item)
        if not installed:return

        candidates=[]
        for name in installed:
            pk3=DOWNLOADED_MAPS_DIR/Path(name).name
            if not pk3.is_file():continue
            try:
                for entry in _scan_map_pk3(pk3,extract_levelshots=False):
                    bsp=str(entry.get("map") or "").strip()
                    if bsp and bsp not in candidates:candidates.append(bsp)
            except Exception as error:
                print(f"[online maps] BSP scan failed: {pk3.name} -> {type(error).__name__}: {error}")

        if not candidates:
            self.mapsStatus.setText("PLAY failed: downloaded PK3 contains no maps/*.bsp")
            return

        # Prefer the BSP matching the source map/file slug. If the package
        # contains one map, simply use that exact BSP name.
        hints=[
            str(item.get("map") or "").strip().casefold(),
            Path(str(item.get("pak") or "")).stem.casefold(),
        ]
        bsp_name=""
        for candidate in candidates:
            if candidate.casefold() in hints:
                bsp_name=candidate; break
        if not bsp_name:
            bsp_name=candidates[0]

        # Identical launch path/arguments to LOCAL maps.
        self.play_local_map(bsp_name)

    def download_online_map(self,item):
        key=_online_map_key(item)
        current=self.onlineDownloadByKey.get(key)
        if current is not None and current.isRunning():return
        w=OnlineMapDownloadWorker(item,self)
        self.onlineDownloadWorkers.append(w)
        self.onlineDownloadByKey[key]=w
        row=self._online_row_for_item(item)
        w.progress.connect(lambda p,x=dict(item):self._online_download_progress(p,x))
        w.paused.connect(lambda x=dict(item):self._online_download_paused(x))
        w.ready.connect(lambda name,x=dict(item):self._online_download_ready(name,x))
        w.failed.connect(lambda e,x=dict(item):self._online_download_failed(e,x))
        w.finished.connect(lambda x=w,k=key,d=dict(item):self._online_download_done(x,k,d))
        w.start()
        if row>=0:
            self._set_online_row_action(row,item)
            self.onlineMapsTable.selectRow(row)

    def pause_online_map(self,item):
        w=self.onlineDownloadByKey.get(_online_map_key(item))
        if w is not None and w.isRunning():
            self.mapsStatus.setText("Pausing download…")
            w.request_pause()

    def _online_download_progress(self,p,item):
        self.mapsStatus.setText(f"Downloading… {p}%")

    def _online_download_paused(self,item):
        self.mapsStatus.setText("Download paused • partial file kept")

    def delete_online_map(self,item):
        row=self._online_row_for_item(item)
        names=_online_installed_files(item)
        deleted=[]
        for name in names:
            path=DOWNLOADED_MAPS_DIR/Path(name).name
            try:
                if path.is_file():
                    path.unlink(); deleted.append(path.name)
            except OSError as error:
                self.mapsStatus.setText(f"Delete failed: {error}")
                return
        _forget_online_install(item)
        partials=[_online_partial_path(item)]
        rawpak=Path(str(item.get("pak") or "").replace("\\","/")).name
        if rawpak:partials.append(DOWNLOADED_MAPS_DIR/(rawpak+".part"))
        if item.get("source")=="LvLWorld":
            ident=str(item.get("lvl_id") or item.get("map") or "online_map")
            legacy=re.sub(r'[^A-Za-z0-9._-]+',"_",ident).strip("._")+".zip.part"
            partials.append(DOWNLOADED_MAPS_DIR/legacy)
        for partial in partials:
            try:partial.unlink(missing_ok=True)
            except OSError:pass
        # Force the local catalog to forget the deleted PK3 instead of retaining
        # stale manifest state until a later full rebuild.
        try:MAPS_MANIFEST_FILE.unlink(missing_ok=True)
        except OSError:pass
        self.mapsStatus.setText("Deleted "+(", ".join(deleted) if deleted else "downloaded map"))
        self.refresh_maps(force=False)
        # Do not rebuild ONLINE results: preserve selection, scroll, metadata,
        # thumbnails and the user's current place in the list.
        if row >= 0:
            self._set_online_row_action(row,item)
            self.onlineMapsTable.selectRow(row)


    def _online_download_done(self,w,key=None,item=None):
        if w in self.onlineDownloadWorkers:self.onlineDownloadWorkers.remove(w)
        if key and self.onlineDownloadByKey.get(key) is w:
            self.onlineDownloadByKey.pop(key,None)
        if isinstance(item,dict):
            row=self._online_row_for_item(item)
            if row>=0:
                self._set_online_row_action(row,item)
                self.onlineMapsTable.selectRow(row)

    def _online_download_failed(self,error,item):
        self.mapsStatus.setText(f"Download failed: {error}")

    def _online_download_ready(self,name,item=None):
        self.mapsStatus.setText(f"Downloaded {name}")
        self.refresh_maps(force=False)
        # Update only the action cell. Re-running search/latest here used to
        # clear selection, reset row state and make keyboard use frustrating.
        if isinstance(item,dict):
            row=self._online_row_for_item(item)
            if row >= 0:
                self._set_online_row_action(row,item)
                self.onlineMapsTable.selectRow(row)


    def _build_map_levelshot_index(self):
        supported = {".jpg", ".jpeg", ".png", ".webp", ".tga"}
        index = {}
        # Preserve old priority exactly: managed !, local !, managed normal, local normal.
        buckets = [[], [], [], []]
        for directory, base in ((MAP_LEVELSHOTS_DIR, 0), (MAP_DISCOVERED_LEVELSHOTS_DIR, 1)):
            if not directory.is_dir():
                continue
            try:
                paths = sorted(
                    (p for p in directory.iterdir() if p.is_file() and p.suffix.casefold() in supported),
                    key=lambda p: p.name.casefold(),
                )
            except OSError:
                continue
            for path in paths:
                stem = path.stem
                important = stem.startswith("!")
                if important:
                    stem = stem[1:]
                bucket = base if important else base + 2
                for alias in (part.strip().casefold() for part in stem.split("&")):
                    if alias:
                        buckets[bucket].append((alias, path))
        for bucket in buckets:
            for alias, path in bucket:
                index.setdefault(alias, []).append(path)
        return index

    def _map_levelshot_pixmap(self, item):
        """Resolve levelshots from one prebuilt directory index, not N directory scans."""
        bsp = str(item.get("map", "") or "").strip().casefold()
        index = getattr(self, "_map_levelshot_index", None)
        if index is None:
            self._map_levelshot_index = self._build_map_levelshot_index()
            index = self._map_levelshot_index
        candidates = list(index.get(bsp, []))
        levelshot = str(item.get("levelshot", "") or "")
        if levelshot:
            candidates.append(Path(levelshot))
        candidates.extend([
            MAP_UNKNOWN_LEVELSHOT,
            ASSETS_DIR / "servers" / "levelshots" / "unknownmap.png",
            ASSETS_DIR / "servers" / "levelshots" / "unknown.png",
        ])
        for path in candidates:
            if path.is_file():
                pix = _load_pixmap_file(path)
                if not pix.isNull():
                    return pix
        return QtGui.QPixmap()

    def refresh_maps(self, force=False):
        if getattr(self, "_maps_loaded", False) and not force:
            return
        if self.mapCatalogWorker is not None and self.mapCatalogWorker.isRunning():
            return
        if force:
            try:
                MAPS_MANIFEST_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            # Forced refresh must not consume stale startup preload.
            self._map_catalog_future = None

        preload = getattr(self, "_map_catalog_future", None)
        if not force and preload is not None:
            self.mapsStatus.setText("Loading cached map catalog…" if preload.done() else "Scanning local maps in background…")
            self.mapsRefreshButton.setEnabled(False)
            if preload.done():
                try:
                    maps, changed = preload.result()
                    self._map_catalog_future = None
                    self._maps_catalog_ready(maps, changed)
                    return
                except Exception as error:
                    print(f"[maps] Startup preload failed: {error}")
                    self._map_catalog_future = None
            else:
                QtCore.QTimer.singleShot(45, self._consume_map_catalog_preload)
                return

        self.mapsStatus.setText("Scanning local maps…")
        self.mapsRefreshButton.setEnabled(False)
        self.mapCatalogWorker = MapCatalogWorker(self)
        self.mapCatalogWorker.ready.connect(self._maps_catalog_ready)
        self.mapCatalogWorker.failed.connect(self._maps_catalog_failed)
        self.mapCatalogWorker.start()

    def _consume_map_catalog_preload(self):
        if getattr(self, "_maps_loaded", False) or not hasattr(self, "mapsTable"):
            return
        future = getattr(self, "_map_catalog_future", None)
        if future is None:
            return self.refresh_maps(force=False)
        if not future.done():
            QtCore.QTimer.singleShot(45, self._consume_map_catalog_preload)
            return
        try:
            maps, changed = future.result()
            self._map_catalog_future = None
            self._maps_catalog_ready(maps, changed)
        except Exception as error:
            print(f"[maps] Startup preload failed: {error}")
            self._map_catalog_future = None
            self.refresh_maps(force=False)

    def _maps_catalog_failed(self, message):
        self.mapsRefreshButton.setEnabled(True)
        self.mapsStatus.setText("Map scan failed")
        self.qerror(f"Could not scan local maps:\n{message}")

    def _maps_catalog_ready(self, maps, changed_paks):
        self.localMaps = list(maps or [])
        self._maps_loaded = True
        self.mapsRefreshButton.setEnabled(True)
        suffix = f" • scanned {changed_paks} new/changed PAK(s)" if changed_paks else " • manifest cache"
        self.mapsStatus.setText(f"{len(self.localMaps)} local map(s){suffix}")
        self._populate_maps_table()

    @staticmethod
    def _map_gametype_sort_key(item):
        order = {"FFA": 0, "TDM": 1, "Duel": 2, "CTF": 3, "Unknown": 9}
        gt = str(item.get("gametype", "Unknown") or "Unknown")
        first = gt.split(" / ", 1)[0]
        return (order.get(first, 8), gt.casefold(), str(item.get("name", "")).casefold())

    def _visible_maps(self):
        items = list(self.localMaps)
        if self.mapsLocationFilter != "All":
            items = [x for x in items if x.get("location") == self.mapsLocationFilter]
        if self.mapsGametypeSort == 1:
            items.sort(key=self._map_gametype_sort_key)
        elif self.mapsGametypeSort == 2:
            items.sort(key=self._map_gametype_sort_key, reverse=True)
        else:
            items.sort(key=lambda x: (str(x.get("name", "")).casefold(), str(x.get("pak", "")).casefold()))
        return items

    def _populate_maps_table(self):
        table = self.mapsTable
        self._map_population_serial = int(getattr(self, "_map_population_serial", 0)) + 1
        serial = self._map_population_serial
        items = self._visible_maps()
        self._map_levelshot_index = self._build_map_levelshot_index()
        self._map_population_items = items
        table.setUpdatesEnabled(False)
        table.clearContents()
        table.setRowCount(len(items))
        table.setUpdatesEnabled(True)
        self.mapsStatus.setText(f"Loading {len(items)} local map(s)…")
        QtCore.QTimer.singleShot(0, lambda s=serial: self._populate_maps_table_batch(s, 0))

    def _populate_maps_table_batch(self, serial, start_row):
        if serial != getattr(self, "_map_population_serial", -1):
            return
        table = self.mapsTable
        items = getattr(self, "_map_population_items", [])
        batch = max(4, theme_int("performance.map_population_batch", 10))
        stop = min(len(items), start_row + batch)
        table.setUpdatesEnabled(False)
        try:
            for row in range(start_row, stop):
                item = items[row]
                table.setRowHeight(row, 92)
                shot_item = QtWidgets.QTableWidgetItem()
                pix = self._map_levelshot_pixmap(item)
                shot_item.setData(QtCore.Qt.ItemDataRole.UserRole, item)
                table.setItem(row, 0, shot_item)
                if not pix.isNull():
                    self._set_map_thumbnail(table, row, pix)

                map_item = QtWidgets.QTableWidgetItem(str(item.get("name") or item.get("map") or "Unknown"))
                map_item.setToolTip(f"BSP: {item.get('map', '')}\nLocation: {item.get('location', 'External')}")
                table.setItem(row, 1, map_item)
                pak_item = QtWidgets.QTableWidgetItem(str(item.get("pak", "")))
                pak_item.setToolTip(str(item.get("pak_path", "")))
                table.setItem(row, 2, pak_item)
                size_item = QtWidgets.QTableWidgetItem(_human_file_size(item.get("pak_size")))
                size_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, 3, size_item)
                table.setItem(row, 4, QtWidgets.QTableWidgetItem(str(item.get("gametype", "Unknown"))))

                actions = QtWidgets.QWidget()
                actions.setObjectName("mapRowActions")
                action_l = QtWidgets.QHBoxLayout(actions)
                action_l.setContentsMargins(0, 0, 0, 0)
                action_l.setSpacing(theme_int("maps.action_gap", 8))
                action_l.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                primary_w = theme_int("maps.primary_action_width", 116)
                delete_w = theme_int("maps.delete_action_width", 40)
                actions.setFixedWidth(primary_w + theme_int("maps.action_gap", 8) + delete_w)

                play = MaterialPushButton("PLAY")
                play.setObjectName("mapPlayButton")
                play.setFixedWidth(primary_w)
                play.setProperty("bsp", str(item.get("map", "")))
                play.clicked.connect(lambda checked=False, b=play: self.play_local_map(str(b.property("bsp") or "")))
                action_l.addWidget(play, 1)

                delete = MaterialPushButton("")
                delete.setObjectName("mapDeleteIconButton")
                delete.setFixedSize(delete_w, 38)
                delete.setProperty("pak_path", str(item.get("pak_path", "")))
                protected = self._map_pak_is_protected(Path(str(item.get("pak_path", "") or "")))
                delete.setEnabled(not protected)
                delete.setToolTip("Protected Q3/Q3Elite content cannot be deleted." if protected else "Delete this PK3 from disk. Extracted levelshots are kept.")
                if qta is not None:
                    try:
                        delete.setIcon(qta.icon("ph.trash", color=theme_value("colors.semantic.danger", "#E54C4C")))
                    except Exception:
                        delete.setText("×")
                else:
                    delete.setText("×")
                delete.clicked.connect(lambda checked=False, b=delete: self.delete_map_pak(Path(str(b.property("pak_path") or ""))))
                action_l.addWidget(delete)
                table.setCellWidget(row, 5, actions)
        finally:
            table.setUpdatesEnabled(True)
        table.viewport().update()

        if stop < len(items):
            self.mapsStatus.setText(f"Loading local maps… {stop}/{len(items)}")
            QtCore.QTimer.singleShot(0, lambda s=serial, r=stop: self._populate_maps_table_batch(s, r))
            return

        self._filter_maps_table(self.mapsSearch.text())
        if table.rowCount() and table.currentRow() < 0:
            table.selectRow(0)
        self.mapsStatus.setText(f"{len(self.localMaps)} local map(s) • ready")

    def _filter_maps_table(self, query):
        if not hasattr(self, "mapsTable"):
            return
        q = str(query or "").strip().casefold()
        for row in range(self.mapsTable.rowCount()):
            data_item = self.mapsTable.item(row, 0)
            data = data_item.data(QtCore.Qt.ItemDataRole.UserRole) if data_item else {}
            haystack = " ".join(str(data.get(k, "")) for k in ("name", "map", "pak", "gametype", "location")).casefold()
            search_hidden = bool(q and q not in haystack)
            location_hidden = (
                self.mapsLocationFilter != "All"
                and data.get("location") != self.mapsLocationFilter
            )
            self.mapsTable.setRowHidden(row, search_hidden or location_hidden)

    def focus_maps_search(self):
        self.mapsSearch.setFocus()
        self.mapsSearch.selectAll()

    def cycle_maps_gametype_sort(self):
        self.mapsGametypeSort = (self.mapsGametypeSort + 1) % 3
        labels = {0: "SORT: NAME", 1: "GAMETYPE ↑", 2: "GAMETYPE ↓"}
        self.mapsSortButton.setText(labels[self.mapsGametypeSort])
        self.mapsSortButton.setProperty("active", self.mapsGametypeSort != 0)
        self.mapsSortButton.style().unpolish(self.mapsSortButton)
        self.mapsSortButton.style().polish(self.mapsSortButton)
        self._populate_maps_table()

    def cycle_maps_location_filter(self):
        modes = ("All", "Preinstalled", "Downloaded")
        try:
            index = modes.index(self.mapsLocationFilter)
        except ValueError:
            index = 0
        self.mapsLocationFilter = modes[(index + 1) % len(modes)]
        self.mapsLocationButton.setText(self.mapsLocationFilter.upper())
        self.mapsLocationButton.setProperty("active", self.mapsLocationFilter != "All")
        self.mapsLocationButton.style().unpolish(self.mapsLocationButton)
        self.mapsLocationButton.style().polish(self.mapsLocationButton)
        # Instant filter: do not rebuild rows or reload levelshots.
        self._filter_maps_table(self.mapsSearch.text())

    def _selected_map_data(self):
        row = self.mapsTable.currentRow()
        if row < 0:
            return None
        item = self.mapsTable.item(row, 0)
        data = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        return data if isinstance(data, dict) else None

    def _step_map(self, direction):
        if not hasattr(self, "mapsTable") or self.mapsTable.rowCount() <= 0:
            return
        visible = [row for row in range(self.mapsTable.rowCount()) if not self.mapsTable.isRowHidden(row)]
        if not visible:
            return
        current = self.mapsTable.currentRow()
        try:
            pos = visible.index(current)
            pos = (pos + int(direction)) % len(visible)
        except ValueError:
            pos = 0 if direction >= 0 else len(visible) - 1
        row = visible[pos]
        self.mapsTable.selectRow(row)
        self.mapsTable.scrollToItem(
            self.mapsTable.item(row, 1),
            QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    @staticmethod
    def _map_pak_is_protected(pak_path):
        return Path(pak_path).name.casefold() in PROTECTED_MAP_PAKS

    def delete_selected_map_pak(self):
        data = self._selected_map_data()
        if data:
            self.delete_map_pak(Path(str(data.get("pak_path", "") or "")))

    def delete_map_pak(self, pak):
        pak = Path(pak)
        if not pak.is_file():
            return
        if self._map_pak_is_protected(pak):
            self.mapsStatus.setText(f"Protected: {pak.name}")
            return

        try:
            pak.unlink()
        except OSError as error:
            self.qerror(f"Could not delete map PAK:\n{error}")
            return

        # Del is intentionally immediate. Persistent AppData levelshots stay cached.
        self.mapsStatus.setText(f"Deleted {pak.name}")
        self.refresh_maps(force=False)

    def play_selected_map(self):
        data = self._selected_map_data()
        if data:
            self.play_local_map(data.get("map", ""))

    def open_selected_map_location(self):
        data = self._selected_map_data()
        if not data:
            return
        pak = Path(str(data.get("pak_path", "") or ""))
        if not pak.is_file():
            return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer.exe", "/select,", str(pak)])
            else:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(pak.parent)))
        except Exception:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(pak.parent)))

    def play_local_map(self, bsp_name):
        bsp_name = str(bsp_name or "").strip()
        if not bsp_name:
            return
        if not q3elite_is_installed():
            self.qerror("Install Quake 3 Elite before launching a map.")
            return
        launcher_bat = GAME_ROOT / "Q3Elite" / "Engines" / "Q3Elite (Vulkan) - Cinematic.bat"
        if not launcher_bat.is_file():
            self.qerror(f"Q3Elite Vulkan launcher was not found:\n{launcher_bat}")
            return
        try:
            import subprocess
            subprocess.Popen(
                [str(launcher_bat), "+devmap", bsp_name],
                cwd=str(launcher_bat.parent),
                shell=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.mapsStatus.setText(f"Launching {bsp_name}…")
        except Exception as error:
            self.qerror(f"Could not launch map:\n{error}")

    def _build_servers(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = MetallicLabel("SERVERS")
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
        nav.setSpacing(8)
        nav.addStretch(1)
        carousel_size = theme_int("servers.carousel_button", 44)
        self.serverPrevButton = ThemedIconButton("ph.caret-left", "servers.carousel_icon", "servers.carousel_icon_hover")
        self.serverPrevButton.setObjectName("serverCarouselButton")
        self.serverPrevButton.setFixedSize(carousel_size, carousel_size)
        self.serverPrevButton.setToolTip("Previous servers")
        if qta is not None: self.serverPrevButton.setIconSize(QtCore.QSize(20,20))
        else: self.serverPrevButton.setText("‹")
        self.serverPrevButton.clicked.connect(lambda: self.scroll_server_monitors(-1))
        nav.addWidget(self.serverPrevButton)
        self.serverPositionLabel = QtWidgets.QLabel("")
        self.serverPositionLabel.setObjectName("serverCarouselPosition")
        self.serverPositionLabel.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.serverPositionLabel.setMinimumWidth(88)
        nav.addWidget(self.serverPositionLabel)
        self.serverNextButton = ThemedIconButton("ph.caret-right", "servers.carousel_icon", "servers.carousel_icon_hover")
        self.serverNextButton.setObjectName("serverCarouselButton")
        self.serverNextButton.setFixedSize(carousel_size, carousel_size)
        self.serverNextButton.setToolTip("Next servers")
        if qta is not None: self.serverNextButton.setIconSize(QtCore.QSize(20,20))
        else: self.serverNextButton.setText("›")
        self.serverNextButton.clicked.connect(lambda: self.scroll_server_monitors(1))
        nav.addWidget(self.serverNextButton)
        nav.addStretch(1)
        root.addLayout(nav)

        self.serverConfigPath = USER_SERVERS_FILE
        if not self.serverConfigPath.is_file() and DEFAULT_SERVERS_FILE.is_file():
            self.serverConfigPath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DEFAULT_SERVERS_FILE, self.serverConfigPath)
        self.serverLevelshotsDir = MAP_LEVELSHOTS_DIR
        self.serverCards = []
        self.serverQueryWorker = None
        self._serverScrollAccum = 0
        self._serverCarouselOffset = 0
        self._serverVisibleCount = 3
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
                try:
                    timer = getattr(widget, "_bg_timer", None)
                    if timer is not None:
                        timer.stop()
                    for view in widget.findChildren(QtWidgets.QAbstractItemView):
                        delegate = view.itemDelegate()
                        if delegate is not None:
                            try:
                                view.viewport().removeEventFilter(delegate)
                            except RuntimeError:
                                pass
                    widget.removeEventFilter(self)
                    for child in widget.findChildren(QtWidgets.QWidget):
                        child.removeEventFilter(self)
                except RuntimeError:
                    pass
                widget.setParent(None)
                widget.deleteLater()
        self.serverCards = []
        self.configuredServers = server_monitor.load_servers(self.serverConfigPath)
        for server in self.configuredServers:
            card = ServerCard(server, self.serverLevelshotsDir, self.serversContent)
            card.connectRequested.connect(self.connect_to_server)
            card.copyRequested.connect(self.copy_server_address)
            card.removeRequested.connect(self.remove_custom_server)
            card.moveRequested.connect(self.move_server)
            card.aliasRequested.connect(self.alias_server)
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
        # Aim for ~390–440 logical px cards instead of stretching three cards to
        # absurd widths on 2K/4K monitors. More cards simply become visible.
        visible = max(3, min(6, viewport_w // 390))
        self._serverVisibleCount = min(max(1, len(self.serverCards)), visible)
        divisor = max(1, self._serverVisibleCount)
        card_w = (viewport_w - spacing * max(0, divisor - 1)) // divisor
        card_w = max(300, min(470, card_w))
        viewport_h = self.serversScroll.viewport().height()
        height = max(430, viewport_h - 2)
        for card in self.serverCards:
            card.setFixedWidth(card_w)
            card.setFixedHeight(height - 6)
        width = count * card_w + max(0, count - 1) * spacing
        self.serversContent.setFixedSize(max(viewport_w, width), height)
        self._update_server_position_label()

    def set_server_edit_mode(self, enabled):
        for card in self.serverCards:
            card.set_edit_mode(enabled)
        if hasattr(self, "editServersButton"):
            self.editServersButton.setText("DONE" if enabled else "EDIT")

    def scroll_server_monitors(self, direction):
        """Infinite carousel: rotate the visible card queue by one position."""
        visible = max(1, int(getattr(self, "_serverVisibleCount", 3)))
        if len(self.serverCards) <= visible:
            return
        direction = 1 if int(direction) > 0 else -1
        self._serverCarouselOffset = (getattr(self, "_serverCarouselOffset", 0) + direction) % len(self.serverCards)
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
        cards = getattr(self, "serverCards", [])
        visible = max(1, int(getattr(self, "_serverVisibleCount", 3)))
        enabled = len(cards) > visible
        if hasattr(self, "serverPrevButton"):
            self.serverPrevButton.setEnabled(enabled)
            self.serverNextButton.setEnabled(enabled)
        if hasattr(self, "serverPositionLabel"):
            if not cards:
                text = "0 SERVERS"
            elif enabled:
                text = f"{visible} / {len(cards)}"
            else:
                text = f"{len(cards)} SERVER{'S' if len(cards) != 1 else ''}"
            self.serverPositionLabel.setText(text)

    def refresh_servers(self):
        for card in getattr(self, "serverCards", []):
            card.update_install_state()
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

    def alias_server(self, address):
        current = server_monitor.load_servers(self.serverConfigPath)
        index = next((i for i, x in enumerate(current)
                      if str(x.get("address","")).lower() == str(address).lower()), -1)
        if index < 0:
            return
        old = str(current[index].get("name", "") or "")
        alias, ok = QtWidgets.QInputDialog.getText(
            self, "Server alias", "Custom server name:",
            QtWidgets.QLineEdit.EchoMode.Normal, old
        )
        if not ok:
            return
        alias = _q3_plain_ascii(alias)
        current[index]["name"] = alias
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


    def _latest_telegram_sources(self, limit=None):
        limit = max(1, min(6, int(limit or theme_int("performance.changelog_latest_posts", 3))))
        entries = read_changelog_entries()
        from datetime import datetime
        def _key(item):
            value = str(item.get("date", item.get("release_date", "")))
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
                try:
                    return datetime.strptime(value[:10], fmt)
                except ValueError:
                    pass
            return datetime.min
        urls = []
        for release in sorted(entries, key=_key, reverse=True):
            source = str(release.get("telegram", release.get("text_source", "")) or "").strip()
            if source and source not in urls:
                urls.append(source)
                if len(urls) >= limit:
                    break
        return urls

    def _configure_detached_telegram_renderer(self, renderer):
        """Make a TelegramTextView a real top-level off-screen render target."""
        if renderer is None:
            return None
        try:
            renderer.setParent(None)
            renderer.setWindowFlags(
                QtCore.Qt.WindowType.Tool
                | QtCore.Qt.WindowType.FramelessWindowHint
                | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
            )
            renderer.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            renderer.setAttribute(QtCore.Qt.WidgetAttribute.WA_QuitOnClose, False)
            renderer.setFixedWidth(max(720, theme_int("performance.telegram_render_width", 900)))
            renderer.move(-30000, -30000)
            renderer.setWindowTitle("Q3Elite Telegram Renderer")
            renderer._q3_detached_renderer = True
            renderers = getattr(self, "_telegramDetachedRenderers", None)
            if renderers is None:
                self._telegramDetachedRenderers = []
                renderers = self._telegramDetachedRenderers
            if renderer not in renderers:
                renderers.append(renderer)
        except RuntimeError:
            return None
        return renderer

    def _create_detached_telegram_renderer(self, source):
        if not TELEGRAM_WEBENGINE_AVAILABLE or QWebEngineView is None:
            return None
        try:
            renderer = TelegramTextView(str(source or ""), None)
            return self._configure_detached_telegram_renderer(renderer)
        except Exception as error:
            print(f"[changelog] Could not create detached Telegram renderer: {error}")
            return None

    def _prewarm_latest_telegram_posts(self):
        """Render the newest Telegram posts off-screen, one at a time.

        These are top-level Tool windows parked far outside the desktop rather
        than children of the launcher. Their Chromium surfaces therefore never
        enter/leave the launcher's translucent HWND when CHANGELOG is selected.
        """
        if (not TELEGRAM_WEBENGINE_AVAILABLE or QWebEngineView is None
                or not theme_bool("performance.telegram_webengine_prewarm", True)):
            return
        if getattr(self, "_telegramPrewarmActive", False):
            return
        urls = self._latest_telegram_sources()
        if not urls:
            return
        self._telegramPrewarmActive = True
        self._telegramPrewarmQueue = list(urls)
        self._telegramPreloadedRenderers = getattr(self, "_telegramPreloadedRenderers", {})
        self._telegramPrewarmCurrent = None
        self._telegram_prewarm_next()

    def _telegram_prewarm_next(self):
        queue = getattr(self, "_telegramPrewarmQueue", None) or []
        if not queue:
            self._telegramPrewarmActive = False
            self._telegramPrewarmCurrent = None
            # Attach only cheap QLabel snapshots while Changelog is still hidden.
            # When the user later clicks CHANGELOG there is no widget-tree swap
            # and no new Chromium child surface to make DWM flash the launcher.
            QtCore.QTimer.singleShot(0, lambda: self._activate_changelog_renderers(allow_hidden=True))
            return

        source = queue.pop(0)
        if source in getattr(self, "_telegramPreloadedRenderers", {}):
            QtCore.QTimer.singleShot(0, self._telegram_prewarm_next)
            return

        renderer = self._create_detached_telegram_renderer(source)
        if renderer is None:
            QtCore.QTimer.singleShot(220, self._telegram_prewarm_next)
            return

        state = {"source": source, "renderer": renderer, "claimed": False}
        self._telegramPrewarmCurrent = state

        def ready():
            current = getattr(self, "_telegramPrewarmCurrent", None)
            if current is not state:
                return
            if not state.get("claimed", False):
                self._telegramPreloadedRenderers[source] = renderer
            self._telegramPrewarmCurrent = None
            QtCore.QTimer.singleShot(220, self._telegram_prewarm_next)

        def failed(reason=""):
            current = getattr(self, "_telegramPrewarmCurrent", None)
            if current is not state:
                return
            self._telegramPrewarmCurrent = None
            if not state.get("claimed", False):
                try:
                    renderer.close()
                    renderer.deleteLater()
                except RuntimeError:
                    pass
            print(f"[changelog] Telegram prewarm failed for {source}: {reason}")
            QtCore.QTimer.singleShot(220, self._telegram_prewarm_next)

        renderer.ready.connect(ready)
        renderer.failed.connect(failed)
        # TelegramTextView will show itself after the styled DOM is ready; its
        # top-level geometry is already parked at -30000/-30000.

    def _take_prewarmed_telegram_renderer(self, source_url):
        """Return the retained/in-flight detached renderer without refetching."""
        source = str(source_url or "").strip()
        if not source:
            return None

        renderers = getattr(self, "_telegramPreloadedRenderers", None) or {}
        renderer = renderers.pop(source, None)
        if renderer is not None:
            return renderer

        current = getattr(self, "_telegramPrewarmCurrent", None)
        if current and current.get("source") == source:
            current["claimed"] = True
            return current.get("renderer")

        queue = getattr(self, "_telegramPrewarmQueue", None)
        if isinstance(queue, list):
            self._telegramPrewarmQueue = [u for u in queue if u != source]

        # User opened Changelog before low-priority warm-up reached this post.
        # Start the one and only navigation now, still in a detached top-level.
        return self._create_detached_telegram_renderer(source)

    def _build_changelog(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = MetallicLabel("CHANGELOG")
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
            latest_limit = max(1, min(6, theme_int("performance.changelog_latest_posts", 3)))
            auto_telegram_urls = []
            for release in sorted_entries:
                source = str(release.get("telegram", release.get("text_source", "")) or "").strip()
                if source and source not in auto_telegram_urls:
                    auto_telegram_urls.append(source)
                    if len(auto_telegram_urls) >= latest_limit:
                        break
            auto_telegram_urls = set(auto_telegram_urls)
            changelog_cards = []
            page._changelog_cards = changelog_cards
            initial_cards = max(1, theme_int("performance.changelog_initial_cards", 3))
            batch_size = max(1, theme_int("performance.changelog_card_batch", 3))
            step_ms = max(8, theme_int("performance.changelog_card_step_ms", 24))

            def append_card(index):
                release = sorted_entries[index]
                source = str(release.get("telegram", release.get("text_source", "")) or "").strip()
                auto_web = bool(
                    source and source in auto_telegram_urls
                    and TELEGRAM_WEBENGINE_AVAILABLE
                    and theme_bool("performance.changelog_auto_webengine", True)
                )
                auto_fetch = bool(source and source in auto_telegram_urls and not auto_web)
                card = ChangelogCard(
                    release, content, expanded=(index < 3), defer_telegram=True,
                    telegram_fetcher=self._queue_telegram_fetch,
                    auto_fetch_telegram=auto_fetch,
                    telegram_web_only=auto_web,
                    telegram_page_provider=self._take_prewarmed_telegram_renderer,
                )
                card._telegram_auto_web = auto_web
                if auto_fetch:
                    self._queue_telegram_fetch(source)
                changelog_cards.append(card)
                # Once the stretch is present, keep new cards above it.
                insert_at = max(0, feed.count() - 1) if getattr(page, "_changelog_has_stretch", False) else feed.count()
                feed.insertWidget(insert_at, card)

            for index in range(min(initial_cards, len(sorted_entries))):
                append_card(index)

            page._changelog_pending_entries = sorted_entries
            page._changelog_next_index = min(initial_cards, len(sorted_entries))
            page._changelog_batch_size = batch_size
            page._changelog_step_ms = step_ms
            page._changelog_append_card = append_card
        else:
            page._changelog_cards = []
            empty = QtWidgets.QLabel("No changelog entries found.")
            empty.setObjectName("muted")
            feed.addWidget(empty)

        feed.addStretch(1)
        page._changelog_has_stretch = True

        def append_changelog_batch():
            entries_local = getattr(page, "_changelog_pending_entries", [])
            index = int(getattr(page, "_changelog_next_index", len(entries_local)))
            batch = int(getattr(page, "_changelog_batch_size", 3))
            append_one = getattr(page, "_changelog_append_card", None)
            if append_one is None or index >= len(entries_local):
                return
            stop = min(len(entries_local), index + batch)
            for i in range(index, stop):
                append_one(i)
            page._changelog_next_index = stop
            if stop < len(entries_local):
                QtCore.QTimer.singleShot(
                    int(getattr(page, "_changelog_step_ms", 24)),
                    append_changelog_batch,
                )

        if getattr(page, "_changelog_next_index", 0) < len(getattr(page, "_changelog_pending_entries", [])):
            QtCore.QTimer.singleShot(0, append_changelog_batch)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        return page

    def _activate_changelog_card(self, card, page, allow_hidden=False):
        try:
            card._telegram_activation_scheduled = False
            if not allow_hidden and self.pages.currentWidget() is not page:
                return
            if not allow_hidden and not card.isVisibleTo(page):
                return
            card.activate_telegram_renderer()
        except RuntimeError:
            pass

    def _activate_changelog_renderers(self, allow_hidden=False):
        """Attach cheap Qt snapshots for the newest Telegram cards.

        Chromium itself stays detached/off-screen. Hidden activation is used
        after prewarm so CHANGELOG navigation performs no renderer creation.
        """
        page = getattr(self, "changelogPage", None)
        if page is None:
            return
        if not allow_hidden and self.pages.currentWidget() is not page:
            return
        real_page = getattr(page, "_q3_real_page", page)
        cards = list(getattr(real_page, "_changelog_cards", []) or [])
        visible = [
            card for card in cards
            if getattr(card, "_expanded", False) and getattr(card, "_telegram_auto_web", False)
        ][:max(1, min(3, theme_int("performance.changelog_latest_posts", 3)))]
        step = 0 if allow_hidden else max(80, theme_int("performance.changelog_webengine_step_ms", 180))
        initial = 0 if allow_hidden else max(20, theme_int("performance.changelog_webengine_delay_ms", 60))
        for index, card in enumerate(visible):
            if card.telegramView is not None or not getattr(card, "_telegram_source_url", ""):
                continue
            if getattr(card, "_telegram_activation_scheduled", False):
                continue
            card._telegram_activation_scheduled = True
            QtCore.QTimer.singleShot(
                initial + index * step,
                lambda c=card, p=page, h=allow_hidden: self._activate_changelog_card(c, p, h),
            )

    def rebuild_changelog_page(self):
        """Rebuild changelog cards without replacing the current stack page."""
        host = self.changelogPage
        mounted = getattr(host, "_q3_real_page", None)
        if mounted is not None and host.layout() is not None:
            layout = host.layout()
            layout.removeWidget(mounted)
            mounted.deleteLater()
            new_page = self._build_changelog()
            new_page.setParent(host)
            layout.addWidget(new_page)
            host._q3_real_page = new_page
            host._changelog_cards = getattr(new_page, "_changelog_cards", [])
            _install_obsidian_scrollbars(new_page)
            host.updateGeometry()
            host.update()
            return

        old_page = host
        old_index = self.pages.indexOf(old_page)
        was_current = self.pages.currentWidget() is old_page
        new_page = self._build_changelog()
        self.pages.insertWidget(old_index, new_page)
        self.pages.removeWidget(old_page)
        old_page.deleteLater()
        self.changelogPage = new_page
        if was_current:
            self.pages.setCurrentWidget(new_page)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Type.Wheel and hasattr(self, "serverCards"):
            # Server monitor uses contextual wheel routing: PLAYER list scrolls
            # itself, while every other point inside a server card rotates the
            # server carousel. Guard stale wrappers during theme/card rebuilds.
            try:
                widget = obj if isinstance(obj, QtWidgets.QWidget) else None
                probe = widget
                inside_player_list = False
                card = None
                while probe is not None:
                    if isinstance(probe, ServerPlayerTable):
                        inside_player_list = True
                        break
                    if isinstance(probe, ServerCard):
                        card = probe
                        break
                    probe = probe.parentWidget()
                if inside_player_list:
                    return False
                if card is not None and card in self.serverCards:
                    delta = event.angleDelta().y() or event.angleDelta().x()
                    if delta and len(self.serverCards) > max(1, int(getattr(self, "_serverVisibleCount", 3))):
                        self.scroll_server_monitors(1 if delta < 0 else -1)
                        event.accept()
                        return True
            except RuntimeError:
                pass

        if (getattr(self, "_renameScreenshotActive", False)
                and event.type() == QtCore.QEvent.Type.MouseButtonPress):
            self._commit_screenshot_rename()
            return False
        if (getattr(self, "_renameScreenshotActive", False)
                and obj is getattr(self, "_renameScreenshotEditor", None)
                and event.type() == QtCore.QEvent.Type.KeyPress
                and event.key() == QtCore.Qt.Key.Key_Escape):
            self._renameScreenshotCancelled = True
            self._cancel_screenshot_rename()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "screenshotPreview"):
            QtCore.QTimer.singleShot(0, self._rescale_screenshot_preview)

    def _sync_media_shortcuts(self, page):
        screenshots_active = page == "screenshots"
        demos_active = page == "demos"
        maps_active = page == "maps"
        statistics_active = page == "statistics"
        if hasattr(self, "statisticsFilterShortcut"):
            self.statisticsFilterShortcut.setEnabled(statistics_active)
        for shortcut in getattr(self, "screenshotShortcuts", []):
            shortcut.setEnabled(screenshots_active)
        for shortcut in getattr(self, "demoShortcuts", []):
            shortcut.setEnabled(demos_active)
        for shortcut in getattr(self, "mapShortcuts", []):
            shortcut.setEnabled(maps_active)

    def _finish_deferred_page_navigation(self, page):
        try:
            self._ensure_page_built(page)
        except Exception as error:
            print(f"[ui] Could not build {page} page: {error}")
            traceback.print_exc()
            return
        self.show_page(page)

    def show_page(self, page):
        page = str(page)
        current_is_matchmaking = (
            "matchmaking" in getattr(self, "_built_pages", set())
            and self.pages.currentWidget() is getattr(self, "matchmakingPage", None)
        )
        if current_is_matchmaking and page != "matchmaking":
            # Matchmaking editors are staged. Leaving without SAVE RULE discards
            # widget-only edits instead of making them look auto-saved.
            self._rebuild_matchmaking_rules()

        nav_mapping = {
            "home": self.homeNav,
            "addons": self.addonsNav,
            "statistics": self.statisticsNav,
            "servers": self.serversNav,
            "maps": self.mapsNav,
            "matchmaking": self.matchmakingNav,
            "screenshots": self.screenshotsNav,
            "demos": self.demosNav,
            "settings": self.settingsNav,
            "changelog": self.changelogNav,
        }
        active = nav_mapping[page]
        attr = self._page_attr_names.get(page, f"{page}Page")
        widget = getattr(self, attr)
        changed = self.pages.currentWidget() is not widget

        # If the user beats the idle warm-up to a page, switch to its cheap
        # placeholder immediately so navigation itself never waits. Construct
        # the real QWidget tree on the next event-loop turn, then re-enter this
        # method for page-specific refreshes.
        if page in self._page_builders and page not in self._built_pages:
            self.pages.setCurrentWidget(widget)
            for b in nav_mapping.values():
                b.setChecked(b is active)
            self._refresh_nav_icons()
            self._move_nav_indicator(active, animate=changed)
            self._sync_media_shortcuts(page)
            QtCore.QTimer.singleShot(0, lambda p=page: self._finish_deferred_page_navigation(p))
            return

        self._sync_media_shortcuts(page)
        self.pages.setCurrentWidget(widget)

        # Deliberately no QGraphicsOpacityEffect here. Fading a complete page
        # forces Qt to composite the whole widget tree into an offscreen surface
        # every animation frame; that is disproportionately expensive for the
        # textured/supersampled Obsidian material. The small nav indicator keeps
        # motion feedback while content switches at native speed.
        if page == "statistics" and not self._statistics_payload and self.statisticsNickname.text().strip():
            QtCore.QTimer.singleShot(0, self.lookup_statistics)
        if page == "matchmaking":
            # Opening Matchmaking marks current badge notifications as read for one minute.
            self.matchmakingBadgesHiddenUntil = time.monotonic() + 60.0
            self._update_matchmaking_status_ui()
            QtCore.QTimer.singleShot(60000, self._update_matchmaking_status_ui)

        for b in nav_mapping.values():
            b.setChecked(b is active)
        self._refresh_nav_icons()
        self._move_nav_indicator(active, animate=changed)

        if page == "addons":
            refresh_component_gui()
        if page == "screenshots":
            if (not getattr(self, "_screenshots_loaded", False)
                    or getattr(self, "_screenshots_dirty", True)):
                self.refresh_screenshots()
            QtCore.QTimer.singleShot(0, self.screenshotList.setFocus)
        if page == "demos":
            if (not getattr(self, "_demos_loaded", False)
                    or getattr(self, "_demos_dirty", True)):
                self.refresh_demos()
            QtCore.QTimer.singleShot(0, self.demoList.setFocus)
        if page == "maps":
            self.refresh_maps(force=False)
        if page == "changelog" and theme_bool("performance.changelog_auto_webengine", False):
            # Optional compatibility mode. The default V4 path stays native and
            # creates no Chromium surfaces simply by opening Changelog.
            QtCore.QTimer.singleShot(0, self._activate_changelog_renderers)
        if page == "servers":
            self.serverRefreshTimer.start()
            QtCore.QTimer.singleShot(0, self._resize_servers_content)
            self.refresh_servers()
        elif hasattr(self, "serverRefreshTimer"):
            self.serverRefreshTimer.stop()

    def set_navigation_enabled(self, enabled):
        for b in (self.homeNav, self.addonsNav, self.statisticsNav, self.serversNav, self.mapsNav, self.matchmakingNav, self.screenshotsNav, self.demosNav, self.settingsNav, self.changelogNav, self.refreshButton):
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
        self.startMinimizedBox.setChecked(launcher_settings.get("start_minimized", False))
        self.startMinimizedBox.setEnabled(self.startWindowsBox.isChecked())
        self.trayBox.setChecked(launcher_settings.get("minimize_to_tray", False))
        self.vulkanLayerBox.setChecked(reshade_layer_enabled())
        self.changelogMediaBox.setChecked(
            launcher_settings.get("show_changelog_media", False)
        )
        self.pauseServerRefreshBox.setChecked(
            launcher_settings.get("pause_server_refresh_unfocused", False)
        )
        self.suppressStartupSoundBox.setChecked(
            launcher_settings.get("suppress_startup_notification_sound", True)
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

    def _remove_cache_path(self, path):
        path = Path(path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        elif path.exists():
            path.unlink()

    def clear_launcher_cache(self):
        try:
            # UI/media cache and disposable temporary launcher state.
            self._remove_cache_path(CACHE_DIR / "Changelog")
            self._remove_cache_path(TEMP_DIR)
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            # Also release in-memory raster caches so this button genuinely
            # returns RAM during a long launcher session. Hot surfaces rebuild
            # lazily on the next paint.
            _PIXMAP_FILE_CACHE.clear()
            _clear_obsidian_render_cache()
            try:
                OBSIDIAN_MATERIAL.reset_caches()
            except Exception:
                pass
            self.settingsMessage.setText("Launcher cache cleared (disk + memory).")
        except Exception as error:
            self.settingsMessage.setText(f"Could not clear launcher cache: {error}")

    def clear_download_cache(self):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for path in list(CACHE_DIR.iterdir()):
                if path.name.casefold() == "changelog":
                    continue
                self._remove_cache_path(path)
            self.settingsMessage.setText("Download cache cleared.")
        except Exception as error:
            self.settingsMessage.setText(f"Could not clear download cache: {error}")

    def repair_launcher(self):
        if getattr(self, "_repair_running", False):
            return
        self._repair_running = True
        self.repairLauncherButton.setEnabled(False)
        self.settingsMessage.setText("Checking Launcher files...")
        QtWidgets.QApplication.processEvents()
        try:
            repair_info = check_launcher_repair()
            if not repair_info:
                self.settingsMessage.setText("Launcher files are OK. No repair needed.")
                return
            self.settingsMessage.setText("Repairing Launcher files...")
            QtWidgets.QApplication.processEvents()
            stage_launcher_update(
                repair_info,
                control=download_control,
                progress_callback=download_progress_callback,
            )
            launch_apply_helper(os.getpid())
            self.settingsMessage.setText("Repair staged. Restarting Launcher...")
            QtCore.QTimer.singleShot(100, QtWidgets.QApplication.instance().quit)
        except Exception as error:
            self.settingsMessage.setText(f"Launcher repair failed: {error}")
        finally:
            self._repair_running = False
            self.repairLauncherButton.setEnabled(True)

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
        executor = getattr(self, "_preload_executor", None)
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._preload_executor = None
        for renderer in list(getattr(self, "_telegramDetachedRenderers", []) or []):
            try:
                renderer.close()
                renderer.deleteLater()
            except RuntimeError:
                pass
        self._telegramDetachedRenderers = []
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
                cached = _load_pixmap_file(key)
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
        cropped = scaled.copy(x, y, target.width(), target.height())
        self.heroImage.setPixmap(_rounded_pixmap(cropped, theme_float("home.hero_image_radius", 12.0)))

    def _apply_native_backdrop(self):
        """Best-effort Windows Acrylic/Mica. Theme remains correct without it."""
        if os.name != "nt" or pywinstyles is None or not theme_bool("effects.native_backdrop", True):
            return
        style = str(theme_value("effects.native_backdrop_style", "acrylic") or "acrylic")
        try:
            pywinstyles.apply_style(self, style)
        except Exception as error:
            print(f"[ui] Native backdrop unavailable ({style}): {error}")

    def _sync_window_controls(self):
        if hasattr(self, "maxButton"):
            maximized = self.isMaximized() or self.isFullScreen()
            self.maxButton.setToolTip(
                "Restore  •  F11 exits fullscreen" if maximized
                else "Maximize / Restore  •  F11 Fullscreen"
            )
            if qta is not None:
                try:
                    icon_name = "ph.corners-in" if maximized else "ph.square"
                    self.maxButton.setIcon(qta.icon(icon_name, color=theme_value("colors.text.muted", "#868A90")))
                    icon_px = theme_int("window_controls.restore_icon_size", 22) if maximized else theme_int("window_controls.icon_size", 18)
                    self.maxButton.setIconSize(QtCore.QSize(icon_px, icon_px))
                    self.maxButton.setText("")
                except Exception:
                    self.maxButton.setText("❐" if maximized else "□")
            else:
                self.maxButton.setText("❐" if maximized else "□")
        if hasattr(self, "sizeGrip"):
            self.sizeGrip.setVisible(not self.isMaximized() and not self.isFullScreen())

    def toggle_maximize(self):
        if self.isFullScreen() or self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        QtCore.QTimer.singleShot(0, self._sync_window_controls)
        QtCore.QTimer.singleShot(80, self._apply_native_backdrop)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            if self._fullscreen_was_maximized:
                self.showMaximized()
        else:
            self._fullscreen_was_maximized = self.isMaximized()
            self.showFullScreen()
        QtCore.QTimer.singleShot(0, self._sync_window_controls)
        QtCore.QTimer.singleShot(80, self._apply_native_backdrop)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and event.position().y() < 70:
            self.toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _apply_window_mask(self):
        """Keep smooth anti-aliased corners; never use a 1-bit QRegion mask.

        QRegion masks caused the staircase visible in V3. The shell and left
        rail now clip their own painting with QPainterPath. On Windows 11 we
        additionally request native DWM rounded corners when available.
        """
        self.clearMask()
        if os.name != "nt" or not theme_bool("layout.window.native_rounding", False):
            return
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = wintypes.HWND(int(self.winId()))
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(DWMWCP_ROUND),
                ctypes.sizeof(DWMWCP_ROUND),
            )
        except Exception:
            # Windows 10 / old DWM: custom anti-aliased painting remains valid.
            pass

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.isMaximized() or self.isFullScreen() or not theme_bool("effects.window_shadow", True):
            return
        outer = theme_int("layout.window.outer_margin", 13)
        if outer <= 1:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        radius = float(theme_value("radius.window", 18))
        layers = max(3, theme_int("effects.window_shadow_layers", 10))
        strength = max(0.0, min(1.0, theme_float("effects.window_shadow_strength", 0.42)))
        base = QtCore.QRectF(outer, outer, max(1, self.width()-outer*2), max(1, self.height()-outer*2))
        for i in range(layers, 0, -1):
            spread = (i / layers) * max(2.0, outer - 1.0)
            alpha = int(58 * strength * (1.0 - (i-1)/layers) ** 1.65)
            rect = base.adjusted(-spread, -spread, spread, spread)
            painter.setPen(QtGui.QPen(QtGui.QColor(0,0,0,alpha), 1.35))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, radius + spread * 0.55, radius + spread * 0.55)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_window_mask()
        self._sync_window_controls()
        if hasattr(self, "serversScroll"):
            QtCore.QTimer.singleShot(0, self._resize_servers_content)
        if hasattr(self, "settingsScroll") or hasattr(self, "matchmakingScroll"):
            QtCore.QTimer.singleShot(0, self._sync_scroll_clearance)
        if hasattr(self, "demoKeys") and self.demoKeys.isVisible():
            self._position_demo_keys_overlay()
        if hasattr(self, "shell"):
            outer = 0 if (self.isMaximized() or self.isFullScreen()) else theme_int("layout.window.outer_margin", 13)
            self.shell.setGeometry(outer, outer, max(1, self.width() - outer * 2), max(1, self.height() - outer * 2))
        if hasattr(self, "windowControls"):
            self.windowControls.adjustSize()
            self.windowControls.move(max(0, self.shell.width() - self.windowControls.width()), 0)
            self.windowControls.raise_()
        if hasattr(self, "sizeGrip"):
            outer = 0 if (self.isMaximized() or self.isFullScreen()) else theme_int("layout.window.outer_margin", 13)
            self.sizeGrip.move(self.width() - outer - self.sizeGrip.width() - 5,
                               self.height() - outer - self.sizeGrip.height() - 5)
            self.sizeGrip.raise_()
        if hasattr(self, "_hero_urls") and self._hero_urls:
            if getattr(self, "_hero_local", None):
                key = str(self._hero_local[self._hero_index % len(self._hero_local)])
            else:
                key = self._hero_urls[self._hero_index]
            pixmap = self._hero_pixmaps.get(key)
            if pixmap is not None: self._set_hero_pixmap(pixmap)

    def _sync_scroll_clearance(self):
        """Align fixed/floating cards with the scroll viewport content edge."""
        def extent(scroll):
            try:
                bar = scroll.verticalScrollBar()
                return max(bar.sizeHint().width(), self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent))
            except Exception:
                return 0
        if hasattr(self, "settingsFooterWrapLayout") and hasattr(self, "settingsScroll"):
            self.settingsFooterWrapLayout.setContentsMargins(4, 0, 20 + extent(self.settingsScroll), 4)
        if hasattr(self, "matchmakingIntroWrapLayout") and hasattr(self, "matchmakingScroll"):
            self.matchmakingIntroWrapLayout.setContentsMargins(0, 0, 18 + extent(self.matchmakingScroll), 0)

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
            if self.isMaximized() or self.isFullScreen():
                self._drag_pos = None
                return
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
        # Q3Elite is the first serial installation stage. If it fails, PAK
        # verification has not started yet, so waiting for base_done here leaves
        # the GUI permanently on INSTALLING. Fail immediately instead.
        if hasattr(window, "firstInstallCard"):
            window.firstInstallCard.setEnabled(True)
        hide_download_controls()
        set_gui_error("RETRY")
        window.qerror(
            "Q3Elite Basic installation failed.\n"
            "Check the launcher log for the exact file/error."
        )
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
    global app, window, download_timer, ui_theme
    global launcher_self_update, q3elite_update, fdownload, q3elite_download, post_update

    try:
        # Qt 6 renders text/widgets at the monitor's native device-pixel ratio.
        # PassThrough avoids extra rounding at fractional Windows scaling (125/150%).
        try:
            QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
        except Exception:
            pass
        app = QApplication(sys.argv)
        icon_path = APP_ICON_ICO if APP_ICON_ICO.is_file() else APP_ICON_PNG
        if icon_path.is_file():
            app.setWindowIcon(QtGui.QIcon(str(icon_path)))

        # Prevent a second launcher process from starting.
        instance_dir = LAUNCHER_DATA_DIR
        instance_dir.mkdir(parents=True, exist_ok=True)
        instance_lock = QLockFile(str(instance_dir / "Q3EliteLauncher.lock"))
        instance_lock.setStaleLockTime(0)
        if not instance_lock.tryLock(100):
            print("Q3Elite Launcher is already running.")
            return 0

        # Identity layer -----------------------------------------------------
        # `Themes/active.json` chooses the current theme. Use --ui-dev for
        # Ctrl+S hot reload while tuning tokens/QSS.
        ui_theme = ThemeManager(
            app,
            launcher_dir=LAUNCHER_DIR,
            assets_dir=ASSETS_DIR,
            dev_mode=("--ui-dev" in sys.argv),
        )
        ui_theme.apply()

        window = ModernLauncherWindow()
        ui_theme.polish_widget_tree(window)
        window._apply_window_mask()
        ui_theme.themeReloaded.connect(window.apply_runtime_theme)

        autostart_mode = "--autostart" in sys.argv
        start_minimized = launcher_settings.get("start_minimized", False)
        if autostart_mode and start_minimized and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            window.trayIcon.show()
            window.hide()
        else:
            window.show()
            # ThemeManager.apply() + pre-show font polish already established the
            # theme. A second full apply_runtime_theme() here used to clear all
            # Obsidian caches and walk/repaint the complete widget tree immediately
            # after the first frame, producing the visible freeze/restart effect.
            # Only the HWND-dependent native backdrop needs a post-show pass.
            QtCore.QTimer.singleShot(0, window._apply_native_backdrop)

        download_timer = QtCore.QTimer(window)
        download_timer.timeout.connect(update_download_overlay)
        download_timer.start(200)

        # Low-priority Telegram warm-up: detached top-level WebEngine renderers
        # load the newest three posts sequentially after the launcher has been
        # idle for a few seconds. The launcher itself never embeds Chromium; it
        # receives ordinary Qt pixmap snapshots, avoiding DWM's translucent-HWND
        # restart/redraw effect while preserving real animated custom emoji.
        if TELEGRAM_WEBENGINE_AVAILABLE and theme_bool("performance.telegram_webengine_prewarm", True):
            QtCore.QTimer.singleShot(
                max(1500, theme_int("performance.telegram_webengine_prewarm_delay_ms", 3500)),
                window._prewarm_latest_telegram_posts,
            )

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
