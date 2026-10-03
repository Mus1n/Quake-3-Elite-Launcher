"""Q3Elite ThemeLab

Standalone theme/font preview utility for the Q3Elite launcher.

Features
--------
* Loads the real ThemeManager and active QSS/tokens without starting updater,
  server browser, downloads, or game logic.
* Switches between installed themes for preview; APPLY TO LAUNCHER persists the selection to active.json.
* Discovers bundled font families in:
    assets/Fonts/<family>/*.(ttf|otf)
    assets/Fonts/<family>/static/*.(ttf|otf)
    assets/fonts/<family>/*.(ttf|otf)
    assets/fonts/<family>/static/*.(ttf|otf)
    Fonts/<family>/*.(ttf|otf)
    Fonts/<family>/static/*.(ttf|otf)
* Live preview and optional save of typography.family to tokens.json.
* --ui-dev style/token reload is always enabled.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ui_theme import ThemeManager  # noqa: E402

try:
    from obsidian_material import OBSIDIAN_MATERIAL  # noqa: E402
except Exception:
    OBSIDIAN_MATERIAL = None


FONT_EXTENSIONS = {".ttf", ".otf"}


def find_launcher_root(start: Path) -> Path:
    """Find the directory containing both themes/ and assets/."""
    candidates: list[Path] = []
    for candidate in (start, *start.parents):
        candidates.append(candidate)
        # Common source layout: this utility lives one or two levels below root.
        if candidate.parent not in candidates:
            candidates.append(candidate.parent)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            pass
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "themes").is_dir() and (candidate / "assets").is_dir():
            return candidate

    # Keep the error actionable rather than silently pointing at a wrong folder.
    raise RuntimeError(
        "Could not locate the Q3Elite launcher root. ThemeLab expects a parent "
        "directory containing both 'themes' and 'assets'."
    )


def family_has_fonts(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    for location in (directory, directory / "static"):
        if not location.is_dir():
            continue
        try:
            if any(p.is_file() and p.suffix.casefold() in FONT_EXTENSIONS for p in location.iterdir()):
                return True
        except OSError:
            pass
    return False


def collect_font_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for location in (directory, directory / "static"):
        if not location.is_dir():
            continue
        try:
            items = sorted(location.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            continue
        for path in items:
            if not path.is_file() or path.suffix.casefold() not in FONT_EXTENSIONS:
                continue
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return files


class MaterialPreview(QtWidgets.QFrame):
    """Uses the real Obsidian painter when that module is present."""

    def __init__(self, theme: ThemeManager, title: str, subtitle: str, seed: int, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.seed = seed
        self.setObjectName("featureCard")
        self.setMinimumHeight(142)
        self.setMouseTracking(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)

        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("featureTitle")
        title_font = title_label.font()
        title_font.setPixelSize(18)
        title_font.setWeight(QtGui.QFont.Weight.Bold)
        title_label.setFont(title_font)

        subtitle_label = QtWidgets.QLabel(subtitle)
        subtitle_label.setObjectName("body")
        subtitle_label.setWordWrap(True)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(8)
        for text in ("NORMAL", "HOVER", "PRESSED"):
            button = QtWidgets.QPushButton(text)
            button.setMinimumHeight(34)
            if text == "HOVER":
                button.setObjectName("applyButton")
            elif text == "PRESSED":
                button.setObjectName("dangerButton")
            buttons.addWidget(button)

        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        layout.addStretch(1)
        layout.addLayout(buttons)

    def paintEvent(self, event):
        # Let the real theme stylesheet paint first. Obsidian's custom material is
        # then drawn as an additional real-launcher preview when available.
        super().paintEvent(event)
        if OBSIDIAN_MATERIAL is None:
            return
        try:
            if not OBSIDIAN_MATERIAL.enabled(self.theme):
                return
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            OBSIDIAN_MATERIAL.paint_panel(
                painter,
                QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5),
                13.0,
                self.theme,
                seed=self.seed,
                hover=1.0 if self.underMouse() else 0.0,
            )
            painter.end()
        except Exception:
            # ThemeLab must stay useful even while material tokens are half-edited.
            pass

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


class ThemeLabWindow(QtWidgets.QMainWindow):
    def __init__(self, app: QtWidgets.QApplication, launcher_dir: Path):
        super().__init__()
        self.app = app
        self.launcher_dir = launcher_dir
        self.assets_dir = launcher_dir / "assets"
        self.themes_dir = launcher_dir / "themes"
        self.preview_font_override: str | None = None

        self.setObjectName("launcherWindow")
        self.setWindowTitle("Q3Elite ThemeLab")
        self.resize(1366, 820)
        self.setMinimumSize(1040, 700)

        # Start with active.json when possible. Theme switching later uses the
        # environment override so ThemeLab never edits the user's active theme.
        initial_theme = self._active_theme_name()
        if initial_theme:
            os.environ["Q3ELITE_THEME"] = initial_theme

        self.theme = ThemeManager(
            app,
            self.launcher_dir,
            self.assets_dir,
            dev_mode=True,
        )
        self.theme.apply()
        self.theme.themeReloaded.connect(self._theme_reloaded)

        self._build_ui()
        self._populate_themes()
        self._populate_fonts()
        self._sync_controls_from_theme()
        self._refresh_diagnostics()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _active_theme_name(self) -> str:
        path = self.themes_dir / "active.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("theme", "")).strip()
        except Exception:
            return ""

    def _installed_themes(self) -> list[dict]:
        result: list[dict] = []
        if not self.themes_dir.is_dir():
            return result
        for directory in sorted(self.themes_dir.iterdir(), key=lambda p: p.name.casefold()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            tokens = directory / "tokens.json"
            style = directory / "style.qss"
            if not tokens.is_file() or not style.is_file():
                continue
            try:
                raw = json.loads(tokens.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            result.append({
                "id": directory.name,
                "name": str(meta.get("display_name") or meta.get("name") or directory.name),
                "version": meta.get("version", "?"),
                "description": str(meta.get("description") or ""),
            })
        return result

    def _font_roots(self) -> list[Path]:
        roots = [
            self.assets_dir / "Fonts",
            self.assets_dir / "fonts",
            self.launcher_dir / "Fonts",
            self.launcher_dir / "fonts",
        ]
        unique: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            try:
                key = root.resolve()
            except OSError:
                key = root
            if key not in seen and root.is_dir():
                seen.add(key)
                unique.append(root)
        return unique

    def _font_families(self) -> list[tuple[str, Path]]:
        found: dict[str, tuple[str, Path]] = {}
        for root in self._font_roots():
            try:
                children = sorted(root.iterdir(), key=lambda p: p.name.casefold())
            except OSError:
                continue
            for child in children:
                if child.is_dir() and family_has_fonts(child):
                    found.setdefault(child.name.casefold(), (child.name, child))
        return [found[key] for key in sorted(found)]

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        central.setObjectName("homePage")
        self.setCentralWidget(central)

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 22)
        root.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)

        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(2)
        self.title_label = QtWidgets.QLabel("THEMELAB")
        self.title_label.setObjectName("heroTitle")
        f = self.title_label.font()
        f.setPixelSize(28)
        f.setWeight(QtGui.QFont.Weight.Bold)
        self.title_label.setFont(f)

        subtitle = QtWidgets.QLabel("Q3Elite theme + typography preview — no updater, servers or game startup")
        subtitle.setObjectName("homeMetaCaption")
        title_box.addWidget(self.title_label)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.reload_button = QtWidgets.QPushButton("RELOAD")
        self.reload_button.setObjectName("homeRefreshButton")
        self.reload_button.clicked.connect(self.reload_theme)
        self.open_theme_button = QtWidgets.QPushButton("OPEN THEME")
        self.open_theme_button.clicked.connect(self.open_theme_folder)
        self.open_fonts_button = QtWidgets.QPushButton("OPEN FONTS")
        self.open_fonts_button.clicked.connect(self.open_fonts_folder)
        header.addWidget(self.reload_button)
        header.addWidget(self.open_theme_button)
        header.addWidget(self.open_fonts_button)
        root.addLayout(header)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # Left controls -------------------------------------------------
        left = QtWidgets.QFrame()
        left.setObjectName("settingsCard")
        left.setMinimumWidth(315)
        left.setMaximumWidth(390)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(18, 18, 18, 18)
        left_layout.setSpacing(11)

        theme_heading = QtWidgets.QLabel("THEME")
        theme_heading.setObjectName("settingsCardTitle")
        left_layout.addWidget(theme_heading)

        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.currentIndexChanged.connect(self.change_theme)
        left_layout.addWidget(self.theme_combo)

        self.theme_description = QtWidgets.QLabel()
        self.theme_description.setWordWrap(True)
        self.theme_description.setObjectName("homeMetaCaption")
        left_layout.addWidget(self.theme_description)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        left_layout.addWidget(line)

        font_heading = QtWidgets.QLabel("FONT FAMILY")
        font_heading.setObjectName("settingsCardTitle")
        left_layout.addWidget(font_heading)

        self.font_combo = QtWidgets.QComboBox()
        self.font_combo.currentIndexChanged.connect(self.preview_selected_font)
        left_layout.addWidget(self.font_combo)

        self.font_path_label = QtWidgets.QLabel("—")
        self.font_path_label.setWordWrap(True)
        self.font_path_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.font_path_label.setObjectName("homeMetaCaption")
        left_layout.addWidget(self.font_path_label)

        buttons = QtWidgets.QHBoxLayout()
        self.preview_font_button = QtWidgets.QPushButton("PREVIEW")
        self.preview_font_button.clicked.connect(self.preview_selected_font)
        self.save_font_button = QtWidgets.QPushButton("APPLY TO LAUNCHER")
        self.save_font_button.setObjectName("applyButton")
        self.save_font_button.clicked.connect(self.apply_to_launcher)
        buttons.addWidget(self.preview_font_button)
        buttons.addWidget(self.save_font_button)
        left_layout.addLayout(buttons)

        self.static_info = QtWidgets.QLabel(
            "Supported font layouts:\n"
            "Fonts/<family>/*.ttf | *.otf\n"
            "Fonts/<family>/static/*.ttf | *.otf"
        )
        self.static_info.setWordWrap(True)
        self.static_info.setObjectName("homeMetaCaption")
        left_layout.addWidget(self.static_info)

        line2 = QtWidgets.QFrame()
        line2.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        left_layout.addWidget(line2)

        diagnostics_heading = QtWidgets.QLabel("DIAGNOSTICS")
        diagnostics_heading.setObjectName("settingsCardTitle")
        left_layout.addWidget(diagnostics_heading)

        self.diagnostics = QtWidgets.QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMinimumHeight(190)
        left_layout.addWidget(self.diagnostics, 1)

        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("homeStatusDetail")
        left_layout.addWidget(self.status_label)

        splitter.addWidget(left)

        # Right preview -------------------------------------------------
        preview_scroll = QtWidgets.QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        preview_host = QtWidgets.QWidget()
        preview_host.setObjectName("settingsPage")
        preview = QtWidgets.QVBoxLayout(preview_host)
        preview.setContentsMargins(18, 4, 18, 18)
        preview.setSpacing(14)

        hero = QtWidgets.QFrame()
        hero.setObjectName("homeHeroCard")
        hero_layout = QtWidgets.QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        hero_layout.setSpacing(8)
        eyebrow = QtWidgets.QLabel("TYPOGRAPHY / LIVE PREVIEW")
        eyebrow.setObjectName("homeEyebrow")
        h1 = QtWidgets.QLabel("Quake 3 Elite")
        h1.setObjectName("heroTitle")
        h1_font = h1.font(); h1_font.setPixelSize(38); h1_font.setWeight(QtGui.QFont.Weight.Bold); h1.setFont(h1_font)
        body = QtWidgets.QLabel(
            "The quick brown fox jumps over the lazy dog. 0123456789 — "
            "Aa Bb Cc Dd Ee Ff Gg. Polish: Zażółć gęślą jaźń."
        )
        body.setObjectName("body")
        body.setWordWrap(True)
        hero_layout.addWidget(eyebrow)
        hero_layout.addWidget(h1)
        hero_layout.addWidget(body)
        preview.addWidget(hero)

        specimens = QtWidgets.QFrame()
        specimens.setObjectName("featureCard")
        specs_layout = QtWidgets.QGridLayout(specimens)
        specs_layout.setContentsMargins(20, 18, 20, 18)
        specs_layout.setHorizontalSpacing(18)
        specs_layout.setVerticalSpacing(9)
        sizes = [("HERO", 38, 700), ("PAGE", 28, 700), ("HEADING", 22, 600), ("SECTION", 16, 600), ("BODY", 14, 400), ("CAPTION", 12, 400)]
        for row, (name, px, weight) in enumerate(sizes):
            label = QtWidgets.QLabel(name)
            label.setObjectName("homeMetaCaption")
            sample = QtWidgets.QLabel(f"Q3Elite / {name.title()} / {weight}")
            sf = sample.font(); sf.setPixelSize(px); sf.setWeight(QtGui.QFont.Weight(max(100, min(900, weight)))); sample.setFont(sf)
            specs_layout.addWidget(label, row, 0)
            specs_layout.addWidget(sample, row, 1)
        preview.addWidget(specimens)

        material_row = QtWidgets.QHBoxLayout()
        material_row.setSpacing(14)
        material_row.addWidget(MaterialPreview(self.theme, "MATTE GRAPHITE", "Panel, text and button rendering using the selected theme.", 11), 1)
        material_row.addWidget(MaterialPreview(self.theme, "COATED METAL", "Hover this card to test the real material painter when available.", 27), 1)
        preview.addLayout(material_row)

        controls = QtWidgets.QFrame()
        controls.setObjectName("featureCard")
        controls_layout = QtWidgets.QGridLayout(controls)
        controls_layout.setContentsMargins(20, 18, 20, 18)
        controls_layout.setHorizontalSpacing(12)
        controls_layout.setVerticalSpacing(10)

        controls_layout.addWidget(QtWidgets.QLabel("Buttons"), 0, 0)
        normal = QtWidgets.QPushButton("NORMAL BUTTON")
        primary = QtWidgets.QPushButton("PRIMARY ACTION"); primary.setObjectName("applyButton")
        danger = QtWidgets.QPushButton("DANGER"); danger.setObjectName("dangerButton")
        controls_layout.addWidget(normal, 0, 1)
        controls_layout.addWidget(primary, 0, 2)
        controls_layout.addWidget(danger, 0, 3)

        controls_layout.addWidget(QtWidgets.QLabel("Input"), 1, 0)
        edit = QtWidgets.QLineEdit("Player name / server address")
        combo = QtWidgets.QComboBox(); combo.addItems(["High quality", "Balanced", "Performance"])
        spin = QtWidgets.QSpinBox(); spin.setRange(0, 999); spin.setValue(125)
        controls_layout.addWidget(edit, 1, 1, 1, 2)
        controls_layout.addWidget(combo, 1, 3)
        controls_layout.addWidget(spin, 1, 4)

        controls_layout.addWidget(QtWidgets.QLabel("Selection"), 2, 0)
        check = QtWidgets.QCheckBox("Enabled checkbox"); check.setChecked(True)
        check2 = QtWidgets.QCheckBox("Disabled checkbox"); check2.setEnabled(False)
        radio = QtWidgets.QRadioButton("Radio option"); radio.setChecked(True)
        controls_layout.addWidget(check, 2, 1)
        controls_layout.addWidget(check2, 2, 2)
        controls_layout.addWidget(radio, 2, 3)

        progress = QtWidgets.QProgressBar(); progress.setValue(68); progress.setFormat("THEME RENDER  %p%")
        controls_layout.addWidget(progress, 3, 0, 1, 5)
        preview.addWidget(controls)

        table_card = QtWidgets.QFrame()
        table_card.setObjectName("featureCard")
        table_layout = QtWidgets.QVBoxLayout(table_card)
        table_layout.setContentsMargins(18, 18, 18, 18)
        table = QtWidgets.QTableWidget(4, 4)
        table.setHorizontalHeaderLabels(["SERVER", "MAP", "PLAYERS", "PING"])
        rows = [
            ("Q3MSK Freeze", "q3dm6", "14 / 20", "42"),
            ("OSP2-BE Arena", "pro-q3dm13", "8 / 16", "31"),
            ("Q3Elite CTF", "q3ctf4", "10 / 16", "55"),
            ("Localhost", "q3dm17", "1 / 8", "0"),
        ]
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                table.setItem(r, c, QtWidgets.QTableWidgetItem(value))
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.selectRow(1)
        table_layout.addWidget(table)
        preview.addWidget(table_card)

        preview.addStretch(1)
        preview_scroll.setWidget(preview_host)
        splitter.addWidget(preview_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([350, 980])

    # ------------------------------------------------------------------
    # Theme / font operations
    # ------------------------------------------------------------------

    def _populate_themes(self):
        current = self.theme.theme_name
        self.theme_combo.blockSignals(True)
        self.theme_combo.clear()
        for item in self._installed_themes():
            label = f"{item['name']}  ·  v{item['version']}"
            self.theme_combo.addItem(label, item)
            if item["id"] == current:
                self.theme_combo.setCurrentIndex(self.theme_combo.count() - 1)
        self.theme_combo.blockSignals(False)
        self._update_theme_description()

    def _populate_fonts(self):
        current_requested = str(self.theme.value("typography.family", self.theme.font_family) or self.theme.font_family)
        entries = self._font_families()
        self.font_combo.blockSignals(True)
        self.font_combo.clear()
        found_current = False
        for family, path in entries:
            self.font_combo.addItem(family, str(path))
            if family.casefold() == current_requested.casefold():
                self.font_combo.setCurrentIndex(self.font_combo.count() - 1)
                found_current = True
        if not found_current:
            self.font_combo.insertItem(0, current_requested + "  [system/theme]", "")
            self.font_combo.setCurrentIndex(0)
        self.font_combo.blockSignals(False)
        self._update_font_path()

    def _sync_controls_from_theme(self):
        current = self.theme.theme_name
        for i in range(self.theme_combo.count()):
            data = self.theme_combo.itemData(i)
            if isinstance(data, dict) and data.get("id") == current:
                self.theme_combo.blockSignals(True)
                self.theme_combo.setCurrentIndex(i)
                self.theme_combo.blockSignals(False)
                break
        self._update_theme_description()

    def _update_theme_description(self):
        data = self.theme_combo.currentData()
        if isinstance(data, dict):
            self.theme_description.setText(f"{data.get('id', '')}\n{data.get('description', '')}".strip())
        else:
            self.theme_description.setText("")

    def _update_font_path(self):
        path = str(self.font_combo.currentData() or "")
        if not path:
            self.font_path_label.setText("System font or family provided by the active theme.")
            return
        directory = Path(path)
        files = collect_font_files(directory)
        static = directory / "static"
        details = [str(directory)]
        if static.is_dir():
            details.append("static/: detected")
        details.append(f"TTF/OTF files: {len(files)}")
        self.font_path_label.setText("\n".join(details))

    def change_theme(self):
        data = self.theme_combo.currentData()
        if not isinstance(data, dict):
            return
        name = str(data.get("id", "")).strip()
        if not name:
            return
        os.environ["Q3ELITE_THEME"] = name
        self.preview_font_override = None
        try:
            self.theme.reload()
            self._populate_fonts()
            self._sync_controls_from_theme()
            self.status_label.setText(f"Previewing theme: {name} (active.json unchanged)")
        except Exception as error:
            self.status_label.setText(f"Theme load failed: {error}")
        self._update_theme_description()

    def reload_theme(self):
        try:
            self.theme.reload()
            self._populate_fonts()
            self._refresh_diagnostics()
            self.status_label.setText("Theme reloaded from disk.")
        except Exception as error:
            self.status_label.setText(f"Reload failed: {error}")

    def preview_selected_font(self):
        if self.font_combo.count() == 0:
            return
        family = self.font_combo.currentText().split("  [", 1)[0].strip()
        if not family:
            return
        try:
            typography = self.theme.tokens.setdefault("typography", {})
            if not isinstance(typography, dict):
                raise RuntimeError("typography token is not an object")
            typography["family"] = family
            self.preview_font_override = family
            self.theme._register_fonts()
            self.theme._apply_application_font()
            self.app.setStyleSheet(self.theme._render_qss())
            self.theme.polish_widget_tree(self)
            self._refresh_diagnostics()
            self._update_font_path()
            self.status_label.setText(f"Previewing font: {family} (tokens.json unchanged)")
            self.update()
        except Exception as error:
            self.status_label.setText(f"Font preview failed: {error}")

    def apply_to_launcher(self):
        """Persist the ThemeLab selection for the real launcher.

        The selected font is written into the selected theme's tokens.json and
        themes/active.json is updated to that theme id.  launch.pyw creates its
        ThemeManager without a forced theme override, so the next launcher start
        reads this exact active.json selection.
        """
        data = self.theme_combo.currentData()
        if not isinstance(data, dict):
            self.status_label.setText("Could not apply: no theme is selected.")
            return

        theme_name = str(data.get("id", "")).strip()
        family = self.font_combo.currentText().split("  [", 1)[0].strip()
        if not theme_name:
            self.status_label.setText("Could not apply: selected theme has no id.")
            return
        if not family:
            self.status_label.setText("Could not apply: no font family is selected.")
            return

        theme_dir = self.themes_dir / theme_name
        tokens_path = theme_dir / "tokens.json"
        active_path = self.themes_dir / "active.json"

        try:
            if not tokens_path.is_file():
                raise FileNotFoundError(f"Theme tokens not found: {tokens_path}")

            tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
            if not isinstance(tokens, dict):
                raise RuntimeError("tokens.json root must be an object")

            typography = tokens.setdefault("typography", {})
            if not isinstance(typography, dict):
                raise RuntimeError("tokens.json: typography must be an object")
            typography["family"] = family

            tokens_path.write_text(
                json.dumps(tokens, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text(
                json.dumps({"theme": theme_name}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            # Keep the lab preview on the same persisted selection.
            os.environ["Q3ELITE_THEME"] = theme_name
            self.preview_font_override = None
            self.theme.reload()
            self._populate_fonts()
            self._sync_controls_from_theme()
            self._refresh_diagnostics()
            self.status_label.setText(
                f"Applied to launcher: {theme_name} | font: {family}. "
                f"launch.pyw will use this theme on next start."
            )
        except Exception as error:
            self.status_label.setText(f"Could not apply to launcher: {error}")

    def _theme_reloaded(self):
        # ThemeManager's watcher may fire after an external Ctrl+S.
        self._refresh_diagnostics()
        self._update_theme_description()
        self.theme.polish_widget_tree(self)
        self.update()

    def _refresh_diagnostics(self):
        roots = self._font_roots()
        lines = [
            f"Theme: {self.theme.theme_name}",
            f"Resolved font: {self.theme.font_family}",
            f"Styles: {', '.join(self.theme.font_styles) if self.theme.font_styles else 'unknown'}",
            "Loaded files:",
        ]
        if self.theme.font_files_loaded:
            lines.extend(f"  • {name}" for name in self.theme.font_files_loaded)
        else:
            lines.append("  • none bundled / system font")
        lines.append("Font roots:")
        if roots:
            lines.extend(f"  • {root}" for root in roots)
        else:
            lines.append("  • no Fonts/fonts directory found")
        lines.append(f"tokens.json: {self.theme.active_tokens_path()}")
        lines.append(f"style.qss: {self.theme.active_style_path()}")
        self.diagnostics.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------
    # Folder helpers
    # ------------------------------------------------------------------

    def _open_path(self, path: Path):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def open_theme_folder(self):
        self._open_path(self.theme.theme_dir)

    def open_fonts_folder(self):
        data = str(self.font_combo.currentData() or "")
        if data:
            self._open_path(Path(data))
            return
        roots = self._font_roots()
        if roots:
            self._open_path(roots[0])
            return
        # Default new-pack location used by ThemeManager.
        self._open_path(self.assets_dir / "Fonts")


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Q3Elite ThemeLab")
    app.setOrganizationName("Q3Elite")

    try:
        launcher_dir = find_launcher_root(HERE)
    except Exception as error:
        QtWidgets.QMessageBox.critical(None, "Q3Elite ThemeLab", str(error))
        return 2

    try:
        window = ThemeLabWindow(app, launcher_dir)
    except Exception as error:
        QtWidgets.QMessageBox.critical(
            None,
            "Q3Elite ThemeLab",
            f"ThemeLab could not start:\n\n{error}",
        )
        return 3

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
