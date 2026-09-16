import os
import sys


from pathlib import Path

LAUNCHER_DIR = Path(__file__).resolve().parent.parent
GAME_ROOT = LAUNCHER_DIR.parent.parent

os.chdir(LAUNCHER_DIR)



import download_tools as dt
from upd_tools import autoupdate
from bmods_tools import *
from base_methods import *

from gui_tools import *


if q3elite_is_current() and zip_path.exists():
    skip_download = True
else:
    skip_download = False

class Q3EliteDownload(QtCore.QThread):
    result_ready = pyqtSignal(bool)

    def run(self):
        try:
            zip_path = Q3ELITE_TEMP_DIR / "Quake 3 Elite.zip"

            print("\nChecking Quake 3 Elite version...")

            # ============================================================
            # VERSION CHECK
            # ============================================================

            if q3elite_is_current():
                print("Quake 3 Elite is up to date.")
                print("Skipping download.")

            else:
                print("New Quake 3 Elite version available.")
                print("Downloading Quake 3 Elite.zip...")

                # ========================================================
                # PCLOUD DOWNLOAD
                # ========================================================

                conf_file = (
                    DOWNLOAD_CONFS_DIR /
                    "Quake 3 Elite pcloud.dconf"
                )

                if not conf_file.exists():
                    raise FileNotFoundError(
                        f"pCloud configuration not found: {conf_file}"
                    )

                public_link = conf_file.read_text(
                    encoding="utf-8"
                ).strip()

                if not public_link:
                    raise RuntimeError(
                        "Quake 3 Elite pCloud URL is empty."
                    )

                download_url, expected_size = resolve_pcloud_file(
                    public_link,
                    "Quake 3 Elite.zip"
                )

                Q3ELITE_TEMP_DIR.mkdir(
                    parents=True,
                    exist_ok=True
                )

                # Remove outdated archive before downloading
                if zip_path.exists():
                    zip_path.unlink()

                result = dt.downloader(
                    download_url,
                    str(Q3ELITE_TEMP_DIR),
                    "Quake 3 Elite.zip",
                    skip=True
                )

                if not result:
                    raise RuntimeError(
                        "Failed to download Quake 3 Elite.zip."
                    )

                # Verify pCloud-reported size
                if zip_path.stat().st_size != expected_size:
                    raise RuntimeError(
                        "Downloaded Q3Elite archive has incorrect size."
                    )

            # ============================================================
            # INSTALL EXISTING / DOWNLOADED ARCHIVE
            # ============================================================

            install_q3elite_archive(zip_path)

            self.result_ready.emit(True)

        except Exception as error:
            print(
                f"\n[error] Quake 3 Elite installation failed: {error}"
            )

            self.result_ready.emit(False)


def install_q3elite_archive(zip_path):
    import zipfile

    install_root = Path(__file__).resolve().parents[2]

    archive_prefix = "Quake 3 Arena/Quake 3 Elite/"

    print("\nInstalling Quake 3 Elite...")
    print(f"Archive: {zip_path}")
    print(f"Destination: {install_root}")

    # ============================================================
    # VERIFY ARCHIVE
    # ============================================================

    if not zip_path.exists():
        raise FileNotFoundError(
            f"Quake 3 Elite.zip was not found: {zip_path}"
        )

    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError(
            "Quake 3 Elite.zip is not a valid ZIP archive."
        )

    # ============================================================
    # EXTRACT
    # ============================================================

    extracted_files = 0

    with zipfile.ZipFile(zip_path, "r") as archive:

        for member in archive.infolist():

            member_name = member.filename.replace("\\", "/")

            if not member_name.startswith(archive_prefix):
                continue

            relative_name = member_name[len(archive_prefix):]

            if not relative_name:
                continue

            destination = install_root / relative_name

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
                with open(destination, "wb") as target:
                    shutil.copyfileobj(
                        source,
                        target,
                        length=1024 * 1024
                    )

            extracted_files += 1

    if extracted_files == 0:
        raise RuntimeError(
            "No Q3Elite files were found in the archive."
        )

    print(
        f"\nQuake 3 Elite installation complete "
        f"({extracted_files} files)."
    )

class FDownload(QtCore.QThread):
    result_ready = pyqtSignal(bool)
    
    def run(self):
        baseq3_dir = GAME_ROOT / "baseq3"

        (baseq3_dir / "mods" / "baseq3").mkdir(
            parents=True,
            exist_ok=True
        )

        (baseq3_dir / "mods" / "osp" / "demos").mkdir(
            parents=True,
            exist_ok=True
        )
        if not os.path.exists('./cache'):
            os.mkdir('./cache')
        bmod_conf.save()
        dt.download('./download_confs/base.dconf', skip=True)
        autoupdate()

class MDownload(QtCore.QThread):
    result_ready = pyqtSignal(bool)
    
    def run(self):
        get_modlist()

def mdlist_check():
    window.upd_status(False)
    if not os.path.exists('./temp_files/modlist.json'):
        window.qerror('Internet connection error.')


if __name__ == "__main__":
    try:
        
        app = QApplication(sys.argv)
        font_id = QFontDatabase.addApplicationFont("./ui/FiraCode-Regular.ttf")
        if font_id == -1:
            print("Ошибка: не удалось загрузить шрифт :/fonts/MyFont.ttf")
            # Можно поставить запасной шрифт
            family = "Arial"
        else:
            families = QFontDatabase.applicationFontFamilies(font_id)
        app.setStyleSheet(DARK_STYLE)
        window = MainWindow()
    
        font_id = QFontDatabase.addApplicationFont("./ui/FiraCode-Regular.ttf")
        if font_id == -1:
            print("Ошибка: не удалось загрузить шрифт :/fonts/MyFont.ttf")
            # Можно поставить запасной шрифт
            family = "Arial"
        else:
            family = QFontDatabase.applicationFontFamilies(font_id)[0]
    
        window.terminal.set_font(family)
        
        window.show()
        window.open_terminal()
        fdownload = FDownload()
        mdownload = MDownload()
        q3elite_download = Q3EliteDownload()       
        mdownload.finished.connect(mdlist_check)
        fdownload.finished.connect(window.close_terminal)
        mdownload.start()
        fdownload.start()
        q3elite_download.start()
        sys.exit(app.exec())
    
    except Exception as error:
        message = f"""{type(error).__name__}: {error}
If after restart you see this message, write me (t.me/konstalker)
    """
    
        show_error(message, lambda: update("scripts"))
    