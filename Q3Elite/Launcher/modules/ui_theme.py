from __future__ import annotations

import json
import os
import re
import hashlib
import time
from pathlib import Path
from typing import Any, Iterable

from PyQt6 import QtCore, QtGui, QtWidgets

_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
_TOKEN_CACHE_MISS = object()
_TOKEN_CACHE_MISSING = object()


class ThemeError(RuntimeError):
    pass


class ThemeManager(QtCore.QObject):
    """Token/QSS theme manager for Q3Elite.

    Identity remains external to launch.pyw:
      Themes/active.json
      Themes/<theme>/tokens.json
      Themes/<theme>/style.qss

    --ui-dev or Q3ELITE_UI_DEV=1 enables live reload after Ctrl+S.

    V3 also performs a *font rendering polish* after QSS has resolved each
    widget's family/size/weight. This matters on Windows at 11–14 px: it keeps
    the semantic QSS typography while requesting deterministic DirectWrite
    hinting/quality from QFont.
    """

    themeReloaded = QtCore.pyqtSignal()

    def __init__(self, app, launcher_dir: Path, assets_dir: Path, *, dev_mode=False):
        super().__init__(app)
        self.app = app
        self.launcher_dir = Path(launcher_dir)
        self.assets_dir = Path(assets_dir)
        self.themes_dir = self.launcher_dir / "Themes"
        self.active_file = self.themes_dir / "active.json"
        self.dev_mode = bool(dev_mode or os.environ.get("Q3ELITE_UI_DEV") == "1")

        self.theme_name = "Steel"
        self.theme_dir = self.themes_dir / self.theme_name
        self.tokens_file = self.theme_dir / "tokens.json"
        self.style_file = self.theme_dir / "style.qss"
        self.tokens: dict[str, Any] = {}
        self.font_family = "Segoe UI"
        self.font_files_loaded: list[str] = []
        self.font_styles: list[str] = []
        self._registered_fonts: set[Path] = set()

        # Hot-path caches. Material widgets ask for theme tokens many times per
        # paint; resolving a dotted path by repeatedly splitting/traversing the
        # JSON tree becomes measurable in the Obsidian theme.  These caches are
        # invalidated atomically whenever tokens are reloaded.
        self._value_cache: dict[str, Any] = {}
        self._flat_tokens_cache: dict[str, Any] | None = None
        self._rendered_qss_cache: tuple[tuple[int, int], str] | None = None
        self.revision = 0

        self._watcher: QtCore.QFileSystemWatcher | None = None
        self._reload_timer = QtCore.QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.timeout.connect(self.reload)

        # QFileSystemWatcher can lose a file watch when an editor saves by
        # replacing the file atomically. A tiny fingerprint poll makes live
        # editing reliable in Notepad++, VS Code and JetBrains editors.
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(350)
        self._poll_timer.timeout.connect(self._poll_for_changes)
        self._fingerprints: dict[Path, tuple] = {}
        self._reload_pending = False
        self._ignore_watcher_until = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(self):
        self._load_theme_selection()
        self._load_tokens()
        self._register_fonts()
        self._apply_application_font()
        self.app.setStyleSheet(self._render_qss())
        if self.dev_mode:
            self._setup_watcher()
            if not self._poll_timer.isActive():
                self._poll_timer.start()

    def reload(self):
        try:
            self.apply()
            self.themeReloaded.emit()
            print(f"[ui] Theme reloaded: {self.theme_name}")
        except Exception as error:
            # Editors can briefly leave JSON half-written while saving. Keep the
            # previous valid style instead of killing a live design session.
            print(f"[ui] Theme reload failed: {error}")

    def value(self, dotted_path: str, default=None):
        key = str(dotted_path)
        cached = self._value_cache.get(key, _TOKEN_CACHE_MISS)
        if cached is not _TOKEN_CACHE_MISS:
            return default if cached is _TOKEN_CACHE_MISSING else cached

        node: Any = self.tokens
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                self._value_cache[key] = _TOKEN_CACHE_MISSING
                return default
            node = node[part]
        self._value_cache[key] = node
        return node

    def int(self, dotted_path: str, default: int) -> int:
        try:
            return int(self.value(dotted_path, default))
        except (TypeError, ValueError):
            return int(default)

    def float(self, dotted_path: str, default: float) -> float:
        try:
            return float(self.value(dotted_path, default))
        except (TypeError, ValueError):
            return float(default)

    def bool(self, dotted_path: str, default: bool = False) -> bool:
        value = self.value(dotted_path, default)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(value)

    def color(self, dotted_path: str, default="#ffffff") -> QtGui.QColor:
        color = self._parse_color(str(self.value(dotted_path, default)))
        if color.isValid():
            return color
        fallback = self._parse_color(str(default))
        return fallback if fallback.isValid() else QtGui.QColor("#ffffff")

    def asset_path(self, dotted_path: str, default: Path | None = None) -> Path | None:
        raw = self.value(dotted_path)
        if not raw:
            return Path(default) if default is not None else None
        path = Path(str(raw))
        if not path.is_absolute():
            path = self.launcher_dir / path
        return path

    def polish_widget_tree(self, root: QtWidgets.QWidget) -> None:
        """Apply rendering-only QFont flags after QSS chose sizes/weights.

        This deliberately does NOT change the semantic font size or weight. It
        asks Qt/DirectWrite for the requested hinting/quality on the actual font
        each widget ended up with.
        """
        widgets: Iterable[QtWidgets.QWidget] = [root, *root.findChildren(QtWidgets.QWidget)]
        for widget in widgets:
            try:
                original = widget.font()
                font = QtGui.QFont(original)
                self._polish_font(font)
                if font != original:
                    widget.setFont(font)
            except (RuntimeError, TypeError):
                # Native/embedded widgets may disappear during a hot reload.
                continue

    def font_diagnostics(self) -> str:
        styles = ", ".join(self.font_styles) if self.font_styles else "unknown"
        loaded = ", ".join(self.font_files_loaded) if self.font_files_loaded else "system font / none bundled"
        return f"family={self.font_family}; styles={styles}; files={loaded}"

    def active_tokens_path(self) -> Path:
        return self.tokens_file

    def active_style_path(self) -> Path:
        return self.style_file

    @staticmethod
    def _theme_version_key(value: Any) -> tuple[int, ...]:
        """Return a sortable numeric key for theme generation metadata.

        Older development builds used values such as "19.1" while released
        presets use integers such as 20.  Theme discovery must tolerate both;
        a malformed/empty value simply sorts as generation 0 instead of
        crashing Settings -> Theme Hub.
        """
        if value is None or isinstance(value, bool):
            return (0,)
        if isinstance(value, int):
            return (max(0, value),)
        if isinstance(value, float):
            # Preserve decimal generations (19.1 -> (19, 1)) without relying
            # on binary floating-point ordering.
            value = format(value, "g")

        text = str(value).strip()
        if not text:
            return (0,)

        # Accept forms such as 19, 19.1, v19.1, 20-beta2.  Numeric components
        # are enough for generation ordering; textual suffixes do not make a
        # legacy preset newer than its numeric generation.
        parts = [int(piece) for piece in re.findall(r"\d+", text)]
        return tuple(parts) if parts else (0,)

    def available_themes(self) -> list[dict[str, Any]]:
        """Return installed theme presets with lightweight metadata."""
        themes = []
        if not self.themes_dir.is_dir():
            return themes
        for directory in sorted(self.themes_dir.iterdir(), key=lambda x: x.name.casefold()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            tokens_file = directory / "tokens.json"
            style_file = directory / "style.qss"
            if not tokens_file.is_file() or not style_file.is_file():
                continue
            try:
                data = json.loads(tokens_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            meta = data.get("meta") if isinstance(data, dict) else {}
            if not isinstance(meta, dict):
                meta = {}

            raw_version = meta.get("version") or 0
            themes.append({
                "id": directory.name,
                "name": str(meta.get("display_name") or meta.get("name") or directory.name),
                "description": str(meta.get("description") or ""),
                "preview": dict(meta.get("preview") or {}),
                # Preserve the author's metadata for display/debugging while
                # keeping a separate robust key for generation comparisons.
                "version": raw_version,
                "_version_key": self._theme_version_key(raw_version),
            })

        # Show only the newest installed theme generation. Update archives are
        # safe to extract over old V19.x/V20 folders without legacy presets
        # reappearing. Decimal versions such as 19.1 are supported.
        newest = max((item.get("_version_key", (0,)) for item in themes), default=(0,))
        current = [item for item in themes if item.get("_version_key", (0,)) == newest]
        result = current or themes
        for item in result:
            item.pop("_version_key", None)
        return result

    def set_theme(self, theme_name: str, *, persist: bool = True) -> None:
        """Switch theme immediately and optionally persist it in active.json.

        ThemeHub writes active.json and then performs one explicit reload.  The
        file watcher used by --ui-dev must not immediately trigger a second
        rebuild while old ServerCard widgets are being deleted.
        """
        theme_name = str(theme_name or "").strip()
        target = self.themes_dir / theme_name
        if not theme_name or not (target / "tokens.json").is_file() or not (target / "style.qss").is_file():
            raise ThemeError(f"Theme not found: {theme_name}")
        if theme_name == self.theme_name and persist:
            return

        self._reload_timer.stop()
        self._ignore_watcher_until = time.monotonic() + 0.75
        if persist:
            self.active_file.parent.mkdir(parents=True, exist_ok=True)
            self.active_file.write_text(
                json.dumps({"theme": theme_name}, indent=2) + "\n",
                encoding="utf-8",
            )
        if os.environ.get("Q3ELITE_THEME"):
            os.environ["Q3ELITE_THEME"] = theme_name
        self.reload()
        # Synchronize fingerprints with the just-applied files so the poller
        # cannot interpret ThemeHub's own write as another developer edit.
        for path in (self.active_file, self.tokens_file, self.style_file):
            self._fingerprints[path] = self._fingerprint(path)
        if self.dev_mode:
            self._setup_watcher()

    # ------------------------------------------------------------------
    # Loading / QSS rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_color(value: str) -> QtGui.QColor:
        value = str(value).strip()
        match = re.fullmatch(
            r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
            r"(?:\s*,\s*([0-9.]+))?\s*\)",
            value,
            flags=re.I,
        )
        if match:
            r, g, b = (max(0, min(255, int(match.group(i)))) for i in (1, 2, 3))
            alpha_raw = match.group(4)
            alpha = 255
            if alpha_raw is not None:
                raw = float(alpha_raw)
                alpha = round(raw * 255) if raw <= 1.0 else round(raw)
                alpha = max(0, min(255, alpha))
            return QtGui.QColor(r, g, b, alpha)
        return QtGui.QColor(value)

    def _load_theme_selection(self):
        name = os.environ.get("Q3ELITE_THEME", "").strip()
        if not name and self.active_file.is_file():
            try:
                raw = json.loads(self.active_file.read_text(encoding="utf-8"))
                name = str(raw.get("theme", "")).strip()
            except Exception as error:
                print(f"[ui] active.json: {error}")
        if not name:
            name = "Steel"

        # Seamless update migration: preserve the user's Black/White choice
        # while moving an older generation id to the V20 preset folder.
        # Seamless update migration: preserve the user's dark/light choice
        # while moving older theme ids to the V20 presets.
        legacy_map = {
            "q3elite-v22-obsidian": "Steel",
            "q3elite-v22-white-paper": "Paper",
            "q3elite-v21-obsidian": "Steel",
            "q3elite-v21-white-paper": "Paper",
            "q3elite-v20-obsidian": "Steel",
            "q3elite-v20-white-paper": "Paper",
            "q3elite-v19-obsidian": "Steel",
            "q3elite-v19-white-paper": "Paper",
            "q3elite-v17-obsidian": "Steel",
            "q3elite-v18-obsidian": "Steel",
            "q3elite-v17-white-paper": "Paper",
            "q3elite-v18-white-paper": "Paper",
            "q3elite-v8-noctra-black": "Steel",
            "q3elite-v9-noctra-black": "Steel",
            "q3elite-v10-noctra-black": "Steel",
            "q3elite-v11-noctra-black": "Steel",
            "q3elite-v12-noctra-black": "Steel",
            "q3elite-v12-dark-nova": "Steel",
            "q3elite-v13-dark-nova": "Steel",
            "q3elite-v14-dark-nova": "Steel",
            "q3elite-v15-obsidian": "Steel",
            "q3elite-v16-obsidian": "Steel",
            "q3elite-v8-paper-cut": "Paper",
            "q3elite-v9-white-paper": "Paper",
            "q3elite-v10-white-paper": "Paper",
            "q3elite-v11-white-paper": "Paper",
            "q3elite-v12-white-paper": "Paper",
            "q3elite-v13-white-paper": "Paper",
            "q3elite-v14-white-paper": "Paper",
            "q3elite-v15-white-paper": "Paper",
            "q3elite-v16-white-paper": "Paper",
        }
        migrated = legacy_map.get(name)
        if migrated and (self.themes_dir / migrated / "tokens.json").is_file():
            name = migrated
            if not os.environ.get("Q3ELITE_THEME"):
                try:
                    self.active_file.write_text(json.dumps({"theme": name}, indent=2) + "\n", encoding="utf-8")
                except OSError:
                    pass

        # V20.2 recovery: an update may have written active.json before the
        # corresponding theme folder was copied into Launcher/themes.  Do not
        # abort the whole launcher in that case.  Prefer the same visual family
        # (paper vs dark), then the newest valid installed generation.
        requested_name = name
        if not self._theme_files_exist(name):
            fallback = self._find_fallback_theme(name)
            if fallback:
                print(f"[ui] Theme files missing for {name!r}; falling back to {fallback!r}")
                name = fallback
                if not os.environ.get("Q3ELITE_THEME"):
                    try:
                        self.active_file.parent.mkdir(parents=True, exist_ok=True)
                        self.active_file.write_text(
                            json.dumps({"theme": name}, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    except OSError:
                        pass
            else:
                raise ThemeError(
                    f"Theme files not found for {requested_name!r} and no valid installed theme was found in {self.themes_dir}"
                )

        self.theme_name = name
        self.theme_dir = self.themes_dir / name
        self.tokens_file = self.theme_dir / "tokens.json"
        self.style_file = self.theme_dir / "style.qss"

    def _theme_files_exist(self, name: str) -> bool:
        directory = self.themes_dir / str(name or "")
        return (directory / "tokens.json").is_file() and (directory / "style.qss").is_file()

    def _find_fallback_theme(self, requested_name: str) -> str | None:
        """Find the newest valid installed preset, preferring the same family.

        This primarily protects in-place upgrades where active.json already
        points at the new generation but the new theme directory was not copied.
        """
        requested = str(requested_name or "").casefold()
        wants_paper = any(tag in requested for tag in ("paper", "white", "light"))
        candidates: list[tuple[tuple[int, ...], str, bool]] = []
        if not self.themes_dir.is_dir():
            return None

        for directory in self.themes_dir.iterdir():
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            if not self._theme_files_exist(directory.name):
                continue
            try:
                data = json.loads((directory / "tokens.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            meta = data.get("meta") if isinstance(data, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            version_key = self._theme_version_key(meta.get("version") or 0)
            folded = directory.name.casefold()
            is_paper = any(tag in folded for tag in ("paper", "white", "light"))
            candidates.append((version_key, directory.name, is_paper))

        if not candidates:
            return None
        same_family = [item for item in candidates if item[2] == wants_paper]
        pool = same_family or candidates
        pool.sort(key=lambda item: (item[0], item[1].casefold()), reverse=True)
        return pool[0][1]

    def _load_tokens(self):
        if not self.tokens_file.is_file():
            raise ThemeError(f"Theme tokens not found: {self.tokens_file}")
        data = json.loads(self.tokens_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ThemeError("tokens.json root must be an object")
        self.tokens = data
        self.revision += 1
        self._value_cache.clear()
        self._flat_tokens_cache = None
        self._rendered_qss_cache = None

    def _flatten(self, node: Any, prefix=""):
        out = {}
        if isinstance(node, dict):
            for key, value in node.items():
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                out.update(self._flatten(value, next_prefix))
        else:
            out[prefix] = node
        return out

    def _render_qss(self):
        if not self.style_file.is_file():
            raise ThemeError(f"Theme stylesheet not found: {self.style_file}")

        stat = self.style_file.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._rendered_qss_cache
        if cached is not None and cached[0] == signature:
            return cached[1]

        source = self.style_file.read_text(encoding="utf-8")
        if self._flat_tokens_cache is None:
            self._flat_tokens_cache = self._flatten(self.tokens)
        flat = dict(self._flat_tokens_cache)
        flat["typography.resolved_family"] = self.font_family
        missing = set()

        def repl(match):
            key = match.group(1)
            if key not in flat:
                missing.add(key)
                return match.group(0)
            value = flat[key]
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        rendered = _TOKEN_RE.sub(repl, source)
        if missing:
            print("[ui] Missing theme tokens: " + ", ".join(sorted(missing)))
        self._rendered_qss_cache = (signature, rendered)
        return rendered

    # ------------------------------------------------------------------
    # Font loading and rendering
    # ------------------------------------------------------------------

    def _font_family_dirs(self, requested: str) -> list[Path]:
        """Return candidate bundled-font folders for a requested family.

        ThemeLab font packs may live under ``assets/Fonts``/``assets/fonts``
        or directly under ``Launcher/Fonts``/``Launcher/fonts``. Windows treats
        case variants equivalently, but accepting both keeps packs portable on
        case-sensitive development systems too.
        """
        family = str(requested or "").strip()
        if not family:
            return []

        roots = [
            self.assets_dir / "fonts",
            self.assets_dir / "Fonts",
            self.launcher_dir / "fonts",
            self.launcher_dir / "Fonts",
        ]
        candidates: list[Path] = []
        seen: set[Path] = set()

        def add(path: Path) -> None:
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key not in seen and path.is_dir():
                seen.add(key)
                candidates.append(path)

        # Fast path: the folder name is the same as typography.family.
        for root in roots:
            add(root / family)

        # Case-insensitive fallback is useful on case-sensitive development
        # systems when a Windows-created pack has different capitalization.
        family_cf = family.casefold()
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for child in root.iterdir():
                    if child.is_dir() and child.name.casefold() == family_cf:
                        add(child)
            except OSError:
                continue

        return candidates

    @staticmethod
    def _font_files_in_family_dir(family_dir: Path, *, prefer_static: bool = True) -> list[Path]:
        """Discover ThemeLab font files in the two supported folder layouts.

        Supported:
          Fonts/<family>/*.ttf
          Fonts/<family>/*.otf
          Fonts/<family>/static/*.ttf
          Fonts/<family>/static/*.otf

        Discovery is intentionally limited to the family root and its ``static``
        child. That prevents unrelated files in arbitrary nested folders from
        silently becoming part of the application font set.
        """
        static_dir = family_dir / "static"

        def has_fonts(directory: Path) -> bool:
            if not directory.is_dir():
                return False
            try:
                return any(
                    p.is_file() and p.suffix.casefold() in {".ttf", ".otf"}
                    for p in directory.iterdir()
                )
            except OSError:
                return False

        # With prefer_static_files enabled, a populated static/ directory is the
        # authoritative face set. This preserves deterministic weight matching
        # when the family root also contains a variable font. If static/ is empty
        # or absent, root-level TTF/OTF files are used instead.
        if prefer_static and has_fonts(static_dir):
            groups = [static_dir]
        else:
            groups = [family_dir]
            if static_dir.is_dir():
                groups.append(static_dir)

        found: list[Path] = []
        seen: set[Path] = set()
        for directory in groups:
            try:
                entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
            except OSError:
                continue
            for path in entries:
                if not path.is_file() or path.suffix.casefold() not in {".ttf", ".otf"}:
                    continue
                try:
                    key = path.resolve()
                except OSError:
                    key = path
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
        return found

    def _register_fonts(self):
        requested = str(self.value("typography.family", "Figtree")).strip() or "Figtree"
        fallback = str(self.value("typography.fallback", "Segoe UI Variable Text")).strip()
        fallback_legacy = str(self.value("typography.fallback_legacy", "Segoe UI")).strip() or "Segoe UI"
        prefer_static = self.bool("typography.rendering.prefer_static_files", True)

        # ThemeLab V23: a font family is now a folder, not a Figtree-specific
        # filename list. Load every TTF/OTF face directly inside that family and
        # inside its optional static/ directory.
        preferred: list[Path] = []
        seen: set[Path] = set()
        for family_dir in self._font_family_dirs(requested):
            for font_path in self._font_files_in_family_dir(
                family_dir, prefer_static=prefer_static
            ):
                try:
                    key = font_path.resolve()
                except OSError:
                    key = font_path
                if key not in seen:
                    seen.add(key)
                    preferred.append(font_path)

        loaded_families: list[str] = []
        current_paths: list[str] = []
        for font_path in preferred:
            try:
                font_path = font_path.resolve()
            except OSError:
                pass

            if font_path not in self._registered_fonts:
                font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
                if font_id < 0:
                    print(f"[ui] WARNING: failed to register font: {font_path}")
                    continue
                self._registered_fonts.add(font_path)
                loaded_families.extend(QtGui.QFontDatabase.applicationFontFamilies(font_id))
            current_paths.append(str(font_path))

        # Keep diagnostics scoped to the active family. Registered application
        # fonts remain available in Qt for the process lifetime, but old theme
        # files should not be reported as if the current theme loaded them.
        self.font_file_paths = current_paths
        self.font_files_loaded = [Path(x).name for x in current_paths]

        available = loaded_families + list(QtGui.QFontDatabase.families())
        requested_cf = requested.casefold()
        selected = next((f for f in available if f.casefold() == requested_cf), None)
        if selected is None:
            selected = next((f for f in available if requested_cf in f.casefold()), None)
        if selected is None and fallback:
            selected = next((f for f in available if f.casefold() == fallback.casefold()), None)
        self.font_family = selected or fallback_legacy

        try:
            self.font_styles = list(QtGui.QFontDatabase.styles(self.font_family))
        except Exception:
            self.font_styles = []

        if requested_cf not in self.font_family.casefold():
            searched = ", ".join(str(p) for p in self._font_family_dirs(requested)) or "no matching bundled folder"
            print(
                f"[ui] WARNING: requested font '{requested}' not detected; "
                f"using '{self.font_family}'. Searched: {searched}"
            )
        else:
            faces = ", ".join(self.font_files_loaded) if self.font_files_loaded else "system-installed font"
            print(f"[ui] Font: {self.font_family} | bundled faces: {faces}")

    def _apply_application_font(self):
        size = self.int("typography.size.body", 14)
        weight = self.int("typography.weight.medium", 500)
        font = QtGui.QFont(self.font_family)
        # Pixel size keeps the token scale aligned with QSS's `px` values.
        font.setPixelSize(max(1, size))
        try:
            font.setWeight(QtGui.QFont.Weight(max(100, min(900, weight))))
        except ValueError:
            font.setWeight(QtGui.QFont.Weight.Medium)
        self._polish_font(font)
        self.app.setFont(font)

    def _polish_font(self, font: QtGui.QFont) -> None:
        hinting = str(self.value("typography.rendering.hinting", "full")).casefold()
        hint_map = {
            "default": QtGui.QFont.HintingPreference.PreferDefaultHinting,
            "none": QtGui.QFont.HintingPreference.PreferNoHinting,
            "vertical": QtGui.QFont.HintingPreference.PreferVerticalHinting,
            "full": QtGui.QFont.HintingPreference.PreferFullHinting,
        }
        try:
            font.setHintingPreference(hint_map.get(hinting, QtGui.QFont.HintingPreference.PreferFullHinting))
        except Exception:
            pass

        strategy = QtGui.QFont.StyleStrategy.PreferDefault
        if self.bool("typography.rendering.antialias", True):
            strategy |= QtGui.QFont.StyleStrategy.PreferAntialias
        if self.bool("typography.rendering.quality", True):
            strategy |= QtGui.QFont.StyleStrategy.PreferQuality
        try:
            font.setStyleStrategy(strategy)
        except Exception:
            pass

        try:
            font.setKerning(self.bool("typography.rendering.kerning", True))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Live reload
    # ------------------------------------------------------------------

    def _fingerprint(self, path: Path) -> tuple:
        try:
            data = path.read_bytes()
            stat = path.stat()
            digest = hashlib.blake2b(data, digest_size=8).hexdigest()
            return (stat.st_mtime_ns, stat.st_size, digest)
        except OSError:
            return (None, None, None)

    def _setup_watcher(self):
        if self._watcher is None:
            self._watcher = QtCore.QFileSystemWatcher(self)
            self._watcher.fileChanged.connect(self._file_changed)
            self._watcher.directoryChanged.connect(self._directory_changed)

        wanted_files = [self.active_file, self.tokens_file, self.style_file]
        wanted_dirs = [self.themes_dir, self.theme_dir]

        current_files = set(self._watcher.files())
        for old in current_files:
            if Path(old) not in wanted_files:
                self._watcher.removePath(old)
        for path in wanted_files:
            if path.is_file() and str(path) not in self._watcher.files():
                self._watcher.addPath(str(path))
            self._fingerprints[path] = self._fingerprint(path)

        current_dirs = set(self._watcher.directories())
        for old in current_dirs:
            if Path(old) not in wanted_dirs:
                self._watcher.removePath(old)
        for path in wanted_dirs:
            if path.is_dir() and str(path) not in self._watcher.directories():
                self._watcher.addPath(str(path))

    def _queue_reload(self):
        if not self.dev_mode or time.monotonic() < self._ignore_watcher_until:
            return
        self._reload_timer.start(120)

    def _file_changed(self, _path):
        self._queue_reload()

    def _directory_changed(self, _path):
        # Re-add file watches after atomic-save replace operations.
        self._setup_watcher()
        self._queue_reload()

    def _poll_for_changes(self):
        if not self.dev_mode:
            return
        # active.json can redirect the theme; check it first using the previous
        # selection, then reload if any watched file fingerprint changed.
        watched = [self.active_file, self.tokens_file, self.style_file]
        for path in watched:
            new_fp = self._fingerprint(path)
            old_fp = self._fingerprints.get(path)
            if old_fp is None:
                self._fingerprints[path] = new_fp
                continue
            if new_fp != old_fp:
                self._fingerprints[path] = new_fp
                self._queue_reload()
                break

