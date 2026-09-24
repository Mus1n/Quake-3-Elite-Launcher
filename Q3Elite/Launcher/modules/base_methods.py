import os
import sys
import subprocess
import shlex
from pathlib import Path

import urllib.request
import urllib.error


# ============================================================================
# Q3ELITE PATHS
# ============================================================================

# base_methods.py:
# Quake 3 Elite\Q3Elite\Launcher\modules\base_methods.py

MODULES_DIR = Path(__file__).resolve().parent
LAUNCHER_DIR = MODULES_DIR.parent
Q3ELITE_DIR = LAUNCHER_DIR.parent
GAME_ROOT = Q3ELITE_DIR.parent


# ============================================================================
# LAUNCHER INTERNAL DIRECTORIES
# ============================================================================

UI_DIR = LAUNCHER_DIR / "ui"
ICONS_DIR = LAUNCHER_DIR / "icons"
DOWNLOAD_CONFS_DIR = MODULES_DIR / "download_confs"
PYTHON_DIR = MODULES_DIR / "python"
SETTINGS_DIR = LAUNCHER_DIR / "settings"
DEFAULT_SERVERS_FILE = SETTINGS_DIR / "servers.json"


# ============================================================================
# GAME DIRECTORIES
# ============================================================================

BASEQ3_DIR = GAME_ROOT / "baseq3"


# ============================================================================
# Q3ELITE APPDATA
# ============================================================================

APPDATA_DIR = Path(os.environ.get("APPDATA", str(Path.home())))

Q3ELITE_APPDATA_DIR = APPDATA_DIR / "Quake 3 Elite"

# Launcher persistent data
Q3ELITE_LAUNCHER_DATA_DIR = Q3ELITE_APPDATA_DIR / "Launcher"

# Download/archive cache
CACHE_DIR = Q3ELITE_LAUNCHER_DATA_DIR / "cache"

# Large temporary Q3Elite downloads
Q3ELITE_TEMP_DIR = Q3ELITE_LAUNCHER_DATA_DIR / "temp"
SERVERS_FILE = Q3ELITE_LAUNCHER_DATA_DIR / "servers.json"


# ============================================================================
# CREATE REQUIRED DIRECTORIES
# ============================================================================

Q3ELITE_LAUNCHER_DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
Q3ELITE_TEMP_DIR.mkdir(parents=True, exist_ok=True)


def check_vulkan_support() -> bool:
    """
    Честно проверяет поддержку Vulkan:
    1. Наличие системного Loader'а.
    2. Успешность создания VkInstance.
    3. Наличие хотя бы одного совместимого GPU.
    """
    instance = None
    try:
        # Минимальная спецификация приложения
        import vulkan as vk
        app_info = vk.VkApplicationInfo(
            sType=vk.VK_STRUCTURE_TYPE_APPLICATION_INFO,
            pApplicationName="VulkanCheck",
            applicationVersion=vk.VK_MAKE_VERSION(1, 0, 0),
            pEngineName="NoEngine",
            engineVersion=vk.VK_MAKE_VERSION(1, 0, 0),
            apiVersion=vk.VK_API_VERSION_1_0,
        )

        create_info = vk.VkInstanceCreateInfo(
            sType=vk.VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
            pApplicationInfo=app_info,
        )

        # 1. Попытка создания инстанса (упадет, если нет Vulkan Loader или драйвера)
        instance = vk.vkCreateInstance(create_info, None)

        # 2. Перечисление доступных видеокарт
        devices = vk.vkEnumeratePhysicalDevices(instance)
        
        has_devices = len(devices) > 0

        if instance is not None:
            vk.vkDestroyInstance(instance, None)
            instance = None

        return has_devices

    except Exception:
        # Падает с ошибками VK_ERROR_INCOMPATIBLE_DRIVER, OSError и т.д.
        return False

def check_url(url):
    try:
        response = urllib.request.urlopen(url, timeout=5)
        return response.status == 200
    except Exception:
        return False

def caption():
    print("Q3Elite Launcher")
    print("https://mus1n.github.io")

def get_relative_paths(folder_path: str) -> list[str]:
    base_dir = Path(folder_path)
    relative_paths = []
    
    for item in base_dir.rglob('*'):
        if item.is_file():
            rel_path = item.relative_to(base_dir)
            relative_paths.append(f"/{rel_path.as_posix()}")
            
    return relative_paths

    
def launch(args='', args2='', force_ogl=False):
    engines_dir = Q3ELITE_DIR / "Engines"

    vulkan_launcher = engines_dir / "Q3Elite (Vulkan) - Cinematic.bat"
    opengl_launcher = engines_dir / "Q3Elite (OpenGL) - Better Compatibility.bat"

    has_vulkan = check_vulkan_support()

    if force_ogl or not has_vulkan:
        launcher = opengl_launcher
    else:
        launcher = vulkan_launcher

    if not launcher.exists():
        print(f"[error] Launcher not found: {launcher}")
        return False

    try:
        subprocess.Popen(
            ["cmd.exe", "/c", str(launcher)],
            cwd=str(engines_dir),
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return True

    except Exception as e:
        print(f"[error] Failed to launch Q3Elite: {e}")
        return False


from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
)
import sys

class UpdateWorker(QThread):
    finished = pyqtSignal()

    def __init__(self, update_function):
        super().__init__()
        self.update_function = update_function

    def run(self):
        try:
            self.update_function()
        finally:
            self.finished.emit()


class ErrorDialog(QDialog):
    def __init__(self, error, update_function):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)

        self.setWindowTitle("Something wrong!")
        self.setFixedWidth(400)

        message = QLabel(
            f"{type(error).__name__}: {error}"
        )
        message.setWordWrap(True)

        self.button = QPushButton("Updating...")
        self.button.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.addWidget(message)
        layout.addWidget(self.button)

        # Запускаем обновление в отдельном потоке
        self.worker = UpdateWorker(update_function)
        self.worker.finished.connect(self.update_finished)
        self.worker.start()

    def update_finished(self):
        self.button.setText("Ok")
        self.button.setEnabled(True)
        self.button.clicked.connect(self.close)
        self.worker.deleteLater()


def show_error(text, update_function):
    app = QApplication.instance()

    if app is None:
        app = QApplication(sys.argv)

    dialog = ErrorDialog(text, update_function)
    dialog.exec()

