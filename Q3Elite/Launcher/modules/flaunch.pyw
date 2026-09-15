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
        mdownload.finished.connect(mdlist_check)
        fdownload.finished.connect(window.close_terminal)
        mdownload.start()
        fdownload.start()
        sys.exit(app.exec())
    
    except Exception as error:
        message = f"""{type(error).__name__}: {error}
If after restart you see this message, write me (t.me/konstalker)
    """
    
        show_error(message, lambda: update("scripts"))
    