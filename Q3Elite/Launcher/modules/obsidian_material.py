from __future__ import annotations

import math
import os
import random
from pathlib import Path
from typing import Optional

from PyQt6 import QtCore, QtGui


class ObsidianMaterialEngine:
    """Reusable material painter for the Q3Elite Obsidian theme.

    The visual model is intentionally photographic rather than neon:

        black base -> micro texture -> directional light -> vignette ->
        contact shadow -> bevel -> irregular specular edge.

    All randomness is deterministic. Nothing flickers frame-to-frame.
    User textures are optional and the theme ships with empty paths.
    """

    def __init__(self) -> None:
        self._noise_cache: dict[tuple[int, int, int], QtGui.QPixmap] = {}
        self._texture_cache: dict[str, QtGui.QPixmap] = {}
        self._data_uri_cache: dict[tuple, str] = {}
        # Supersampled edge/rim layers are cached because the same card geometry
        # is repainted frequently while only hover intensity changes.
        self._edge_cache: dict[tuple, QtGui.QPixmap] = {}

    def reset_caches(self) -> None:
        """Drop generated material caches after a live theme/token reload."""
        self._noise_cache.clear()
        self._texture_cache.clear()
        self._data_uri_cache.clear()
        self._edge_cache.clear()

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _value(theme, path: str, default=None):
        return theme.value(path, default) if theme is not None else default

    @staticmethod
    def _float(theme, path: str, default: float) -> float:
        try:
            return float(theme.value(path, default)) if theme is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _int(theme, path: str, default: int) -> int:
        try:
            return int(theme.value(path, default)) if theme is not None else int(default)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _bool(theme, path: str, default: bool = False) -> bool:
        if theme is None:
            return bool(default)
        value = theme.value(path, default)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _color(theme, path: str, default: str) -> QtGui.QColor:
        if theme is None:
            return QtGui.QColor(default)
        return theme.color(path, default)

    def enabled(self, theme) -> bool:
        return self._bool(theme, "material.enabled", False)

    # ------------------------------------------------------------------
    # Deterministic noise / texture helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _hash01(value: int) -> float:
        value = (value ^ 0x45D9F3B) & 0xFFFFFFFF
        value = ((value ^ (value >> 16)) * 0x45D9F3B) & 0xFFFFFFFF
        value = ((value ^ (value >> 16)) * 0x45D9F3B) & 0xFFFFFFFF
        value ^= value >> 16
        return (value & 0xFFFF) / 65535.0

    def _noise_pixmap(self, size: int, strength: int, seed: int) -> QtGui.QPixmap:
        size = max(48, min(384, int(size)))
        strength = max(0, min(96, int(strength)))
        key = (size, strength, int(seed))
        cached = self._noise_cache.get(key)
        if cached is not None:
            return cached

        image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        rng = random.Random(int(seed))
        # Sparse alpha noise looks more like coated metal/stone than a solid TV grain.
        for y in range(size):
            for x in range(size):
                if rng.random() > 0.42:
                    continue
                polarity = 255 if rng.random() > 0.48 else 0
                alpha = int(rng.random() * strength)
                image.setPixelColor(x, y, QtGui.QColor(polarity, polarity, polarity, alpha))

        pixmap = QtGui.QPixmap.fromImage(image)
        self._noise_cache[key] = pixmap
        return pixmap

    def _texture_pixmap(self, theme, path_key: str) -> QtGui.QPixmap:
        raw = str(self._value(theme, path_key, "") or "").strip()
        if not raw:
            return QtGui.QPixmap()
        path = Path(raw)
        if not path.is_absolute() and theme is not None:
            path = theme.launcher_dir / path
        key = str(path.resolve()) if path.exists() else str(path)
        if key in self._texture_cache:
            return self._texture_cache[key]
        pixmap = QtGui.QPixmap(str(path)) if path.is_file() else QtGui.QPixmap()
        self._texture_cache[key] = pixmap
        return pixmap


    def texture_data_uri(self, theme, path_key: str, *, opacity: float = 1.0, base_color: Optional[QtGui.QColor] = None, max_size: int = 640) -> str:
        """Return a small composited PNG data URI for WebEngine/CSS surfaces.

        Chromium cannot paint a QWidget/QPainter texture behind its own document
        surface.  V18 pre-composites the same theme texture into a compact PNG so
        Telegram changelog content can use the exact Obsidian material too.
        """
        pm = self._texture_pixmap(theme, path_key)
        if pm.isNull():
            return ""
        opacity = max(0.0, min(1.0, float(opacity)))
        max_size = max(96, min(1024, int(max_size)))
        bg = QtGui.QColor(base_color) if base_color is not None else QtGui.QColor(0, 0, 0, 0)
        key = (str(self._value(theme, path_key, "")), round(opacity, 3), bg.rgba(), max_size)
        cached = self._data_uri_cache.get(key)
        if cached is not None:
            return cached
        target = pm
        if max(pm.width(), pm.height()) > max_size:
            target = pm.scaled(max_size, max_size, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
        image = QtGui.QImage(target.size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(bg)
        qp = QtGui.QPainter(image)
        qp.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        qp.setOpacity(opacity)
        qp.drawPixmap(0, 0, target)
        qp.end()
        data = QtCore.QByteArray()
        buffer = QtCore.QBuffer(data)
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        buffer.close()
        uri = "data:image/png;base64," + bytes(data.toBase64()).decode("ascii")
        if len(self._data_uri_cache) > 24:
            self._data_uri_cache.clear()
        self._data_uri_cache[key] = uri
        return uri

    @staticmethod
    def _cover(pm: QtGui.QPixmap, target: QtCore.QSize) -> QtGui.QPixmap:
        if pm.isNull() or target.width() <= 0 or target.height() <= 0:
            return QtGui.QPixmap()
        scaled = pm.scaled(
            target,
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - target.width()) // 2)
        y = max(0, (scaled.height() - target.height()) // 2)
        return scaled.copy(x, y, target.width(), target.height())

    # ------------------------------------------------------------------
    # Common primitives
    # ------------------------------------------------------------------
    @staticmethod
    def _rounded_path(rect: QtCore.QRectF, radius: float) -> QtGui.QPainterPath:
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    @staticmethod
    def _with_alpha(color: QtGui.QColor, alpha: int) -> QtGui.QColor:
        c = QtGui.QColor(color)
        c.setAlpha(max(0, min(255, int(alpha))))
        return c

    def _paint_micro_texture(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        theme,
        *,
        seed: int,
        path_key: Optional[str] = None,
        opacity_key: str = "material.noise.opacity",
    ) -> None:
        if path_key:
            external = self._texture_pixmap(theme, path_key)
            if not external.isNull():
                opacity = max(0.0, min(1.0, self._float(theme, opacity_key, 0.08)))
                painter.save()
                painter.setOpacity(opacity)
                target = self._cover(external, rect.size().toSize())
                if not target.isNull():
                    painter.drawPixmap(rect, target, QtCore.QRectF(target.rect()))
                painter.restore()
                return

        if not self._bool(theme, "material.noise.enabled", True):
            return
        opacity = max(0.0, min(1.0, self._float(theme, opacity_key, 0.035)))
        if opacity <= 0:
            return
        tile = self._noise_pixmap(
            self._int(theme, "material.noise.tile_size", 160),
            self._int(theme, "material.noise.alpha", 18),
            seed,
        )
        painter.save()
        painter.setOpacity(opacity)
        painter.drawTiledPixmap(rect.toRect(), tile)
        painter.restore()

    def _paint_contact_shadow(self, painter, rect, radius, theme, *, strength=1.0) -> None:
        if not self._bool(theme, "material.shadow.enabled", True):
            return
        base_alpha = self._int(theme, "material.shadow.contact_alpha", 150)
        ambient_alpha = self._int(theme, "material.shadow.ambient_alpha", 48)
        # Multiple inside-offset outlines mimic the dense shadow at the contact point
        # without QGraphicsDropShadowEffect compositing artifacts.
        for i, alpha in ((5, ambient_alpha), (3, int(base_alpha * 0.45)), (1, base_alpha)):
            rr = QtCore.QRectF(rect).adjusted(1.0 + i * 0.2, 1.0 + i * 0.55, -1.0 - i * 0.2, -1.0)
            c = QtGui.QColor(0, 0, 0, max(0, min(255, int(alpha * strength))))
            painter.setPen(QtGui.QPen(c, max(0.8, i * 0.72)))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rr, max(2.0, radius - 0.5), max(2.0, radius - 0.5))

    def _paint_imperfect_edge_direct(self, painter, rect, radius, theme, *, seed=0, intensity=1.0, occlusion=True) -> None:
        if not self._bool(theme, "material.edge.enabled", True):
            return
        base = self._color(theme, "material.edge.specular", "rgba(255,255,255,80)")
        base_alpha = max(0, min(255, int(base.alpha() * intensity)))
        if base_alpha <= 0:
            return

        # V18: build the base rim as a filled rounded ring, not a 1px QPen.
        # The previous pen + straight highlight segments became visibly thinner
        # exactly where the top/left lines entered the rounded corner.  A ring
        # keeps identical coverage through every corner, while the directional
        # gradient and later segments still make the light deliberately imperfect.
        is_circle = abs(rect.width() - rect.height()) < 1.5 and radius >= min(rect.width(), rect.height()) * 0.44
        rim_width = max(0.55, self._float(
            theme,
            "material.edge.circle_rim_width" if is_circle else "material.edge.rim_width",
            1.55 if is_circle else 1.15,
        ))
        outer = self._rounded_path(rect, radius)
        inner_rect = rect.adjusted(rim_width, rim_width, -rim_width, -rim_width)
        inner_radius = max(0.0, radius - rim_width)
        ring = QtGui.QPainterPath(outer)
        if inner_rect.width() > 1 and inner_rect.height() > 1:
            ring = ring.subtracted(self._rounded_path(inner_rect, inner_radius))
        outline = self._color(theme, "material.edge.base", "rgba(255,255,255,22)")
        bright = self._color(theme, "material.edge.specular", "rgba(255,255,255,80)")
        ring_grad = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
        a0 = int(base_alpha * max(0.0, min(1.4, self._float(theme, "material.edge.rim_bright_alpha", 0.78))))
        a1 = int(base_alpha * max(0.0, min(1.4, self._float(theme, "material.edge.rim_mid_alpha", 0.46))))
        a2 = int(max(outline.alpha(), base_alpha * max(0.0, min(1.0, self._float(theme, "material.edge.rim_low_alpha", 0.18)))))
        ring_grad.setColorAt(0.0, self._with_alpha(bright, a0))
        ring_grad.setColorAt(0.30, self._with_alpha(bright, a1))
        ring_grad.setColorAt(0.64, self._with_alpha(outline, a2))
        ring_grad.setColorAt(1.0, QtGui.QColor(0, 0, 0, min(220, 110 + int(70 * intensity))))
        painter.fillPath(ring, ring_grad)

        segment = max(12.0, self._float(theme, "material.edge.segment_length", 30.0))
        variance = max(0.0, min(0.8, self._float(theme, "material.edge.variance", 0.30)))
        width = max(0.45, self._float(
            theme,
            "material.edge.circle_specular_width" if is_circle else "material.edge.specular_width",
            1.18 if is_circle else 0.85,
        ))
        offset_variance = max(0.0, min(0.8, self._float(theme, "material.edge.offset_variance", 0.24)))

        # Straight top edge with slightly varying intensity/position.
        left = rect.left() + radius
        right = rect.right() - radius
        i = 0
        x = left
        while x < right:
            x2 = min(right, x + segment)
            n = self._hash01(seed * 131 + i * 17 + 5)
            alpha = int(base_alpha * (1.0 - variance + n * variance))
            y = rect.top() + (self._hash01(seed * 47 + i * 29 + 11) - 0.5) * offset_variance
            pen = QtGui.QPen(self._with_alpha(base, alpha), width)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x2, y))
            x = x2
            i += 1

        # Left edge is weaker, like a studio key light grazing the material.
        top = rect.top() + radius
        bottom = rect.bottom() - radius
        i = 0
        y = top
        while y < bottom:
            y2 = min(bottom, y + segment * 1.2)
            n = self._hash01(seed * 83 + i * 23 + 19)
            alpha = int(base_alpha * 0.55 * (1.0 - variance + n * variance))
            x = rect.left() + (self._hash01(seed * 97 + i * 31 + 7) - 0.5) * offset_variance
            pen = QtGui.QPen(self._with_alpha(base, alpha), max(0.45, width * 0.8))
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x, y2))
            y = y2
            i += 1

        # Circular navigation controls receive a deliberately imperfect grazing
        # rim.  It is rendered through the supersampled wrapper below, so the
        # varying arcs remain smooth instead of turning into staircase pixels.
        if is_circle:
            chunks = max(7, self._int(theme, "material.edge.circle_segments", 11))
            for j in range(chunks):
                n = self._hash01(seed * 193 + j * 41 + 73)
                start_deg = 18.0 + j * (145.0 / chunks)
                span_deg = (145.0 / chunks) * (0.58 + n * 0.34)
                alpha = int(base_alpha * (0.34 + 0.66 * n))
                pen = QtGui.QPen(self._with_alpha(base, alpha), width * (0.82 + n * 0.34))
                pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawArc(rect.adjusted(0.15, 0.15, -0.15, -0.15), int(start_deg * 16), int(span_deg * 16))

        # Bottom/right occlusion defines thickness on dark materials. Silver
        # plates and navigation circles can suppress it; on high-contrast
        # satin surfaces it reads as a black rendering fringe rather than depth.
        if occlusion:
            dark = self._color(theme, "material.edge.occlusion", "rgba(0,0,0,185)")
            painter.setPen(QtGui.QPen(dark, self._float(theme, "material.edge.occlusion_width", 1.15)))
            painter.drawLine(
                QtCore.QPointF(rect.left() + radius, rect.bottom() - 0.4),
                QtCore.QPointF(rect.right() - radius, rect.bottom() - 0.4),
            )
            painter.drawLine(
                QtCore.QPointF(rect.right() - 0.4, rect.top() + radius),
                QtCore.QPointF(rect.right() - 0.4, rect.bottom() - radius),
            )

    def _paint_imperfect_edge(self, painter, rect, radius, theme, *, seed=0, intensity=1.0, occlusion=True) -> None:
        """Paint the material rim through a supersampled alpha layer.

        QPainter antialiasing alone still leaves visible stair-steps on thin,
        high-contrast rounded borders at 100% Windows scaling.  V17 renders the
        rim at 3x by default and downsamples it with SmoothTransformation.
        """
        if not self._bool(theme, "material.edge.enabled", True):
            return
        ss = max(1, min(4, self._int(theme, "material.edge.supersample", 3)))
        if ss <= 1 or rect.width() <= 2 or rect.height() <= 2:
            return self._paint_imperfect_edge_direct(painter, rect, radius, theme, seed=seed, intensity=intensity, occlusion=occlusion)

        pad = 3.0
        w = max(1, int(math.ceil(rect.width() + pad * 2)))
        h = max(1, int(math.ceil(rect.height() + pad * 2)))
        key = (
            w, h, round(float(radius), 2), int(seed), round(float(intensity), 2), ss,
            str(self._value(theme, "material.edge.base", "")),
            str(self._value(theme, "material.edge.specular", "")),
            str(self._value(theme, "material.edge.occlusion", "")),
            round(self._float(theme, "material.edge.base_width", 0.85), 2),
            round(self._float(theme, "material.edge.specular_width", 0.85), 2),
            round(self._float(theme, "material.edge.circle_specular_width", 1.18), 2),
            round(self._float(theme, "material.edge.rim_width", 1.15), 2),
            round(self._float(theme, "material.edge.circle_rim_width", 1.55), 2),
            round(self._float(theme, "material.edge.variance", 0.30), 2),
            bool(occlusion),
        )
        pm = self._edge_cache.get(key)
        if pm is None:
            hi = QtGui.QImage(w * ss, h * ss, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            hi.fill(QtCore.Qt.GlobalColor.transparent)
            hp = QtGui.QPainter(hi)
            hp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            hp.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            hp.scale(ss, ss)
            local = QtCore.QRectF(pad, pad, rect.width(), rect.height())
            self._paint_imperfect_edge_direct(hp, local, radius, theme, seed=seed, intensity=intensity, occlusion=occlusion)
            hp.end()
            low = hi.scaled(w, h, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            pm = QtGui.QPixmap.fromImage(low)
            if len(self._edge_cache) > 320:
                self._edge_cache.clear()
            self._edge_cache[key] = pm
        painter.drawPixmap(QtCore.QRectF(rect.left() - pad, rect.top() - pad, w, h), pm, QtCore.QRectF(pm.rect()))

    def paint_silver_surface(self, painter, path: QtGui.QPainterPath, rect: QtCore.QRectF, theme, *, opacity: float = 1.0) -> None:
        """Fill a white area like brushed/satin silver instead of flat #fff."""
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        grad = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        grad.setColorAt(0.0, self._color(theme, "material.silver.top", "#FAFAFA"))
        grad.setColorAt(0.34, self._color(theme, "material.silver.mid", "#D8D9DC"))
        grad.setColorAt(0.66, self._color(theme, "material.silver.low", "#A7A9AE"))
        grad.setColorAt(1.0, self._color(theme, "material.silver.bottom", "#F0F0F1"))
        painter.fillPath(path, grad)
        tex = self._texture_pixmap(theme, "material.silver_texture")
        if not tex.isNull():
            target = self._cover(tex, rect.size().toSize())
            if not target.isNull():
                painter.save(); painter.setClipPath(path)
                painter.setOpacity(max(0.0, min(1.0, self._float(theme, "material.silver_texture_opacity", 0.16))))
                painter.drawPixmap(rect, target, QtCore.QRectF(target.rect())); painter.restore()
        painter.restore()

    def paint_metallic_text(self, painter, rect: QtCore.QRectF, text: str, font: QtGui.QFont, theme, *, align=QtCore.Qt.AlignmentFlag.AlignCenter, enabled=True) -> None:
        """Draw a single-line label using a satin-silver gradient/texture."""
        if not text:
            return
        fm = QtGui.QFontMetricsF(font)
        width = fm.horizontalAdvance(text)
        height = fm.height()
        if align & QtCore.Qt.AlignmentFlag.AlignLeft:
            x = rect.left()
        elif align & QtCore.Qt.AlignmentFlag.AlignRight:
            x = rect.right() - width
        else:
            x = rect.center().x() - width / 2.0
        baseline = rect.center().y() + (fm.ascent() - fm.descent()) / 2.0
        if not enabled:
            painter.save()
            painter.setFont(font)
            painter.setPen(self._color(theme, "colors.text.disabled", "#55565B"))
            painter.drawText(rect, align | QtCore.Qt.AlignmentFlag.AlignVCenter, text)
            painter.restore()
            return

        # Small glyphs are much sharper through Qt/DirectWrite's normal text
        # rasterizer. Textured QPainterPath glyphs shimmer at 10-13 px, most
        # visibly in table headers. Reserve the metallic mask for larger text.
        min_px = self._float(theme, "material.text_silver.texture_min_px", 15.0)
        if height <= min_px:
            painter.save()
            painter.setFont(font)
            painter.setPen(self._color(theme, "material.text_silver.crisp_small", "#D6D7DA"))
            painter.drawText(rect, align | QtCore.Qt.AlignmentFlag.AlignVCenter, text)
            painter.restore()
            return

        path = QtGui.QPainterPath()
        path.addText(QtCore.QPointF(x, baseline), font, text)
        grad = QtGui.QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        grad.setColorAt(0.0, self._color(theme, "material.text_silver.top", "#FFFFFF"))
        grad.setColorAt(0.42, self._color(theme, "material.text_silver.mid", "#D2D2D2"))
        grad.setColorAt(0.72, self._color(theme, "material.text_silver.low", "#A5A7AC"))
        grad.setColorAt(1.0, self._color(theme, "material.text_silver.bottom", "#E6E7E9"))
        painter.fillPath(path, grad)
        tex = self._texture_pixmap(theme, "material.silver_texture")
        if not tex.isNull():
            target = self._cover(tex, rect.size().toSize())
            if not target.isNull():
                painter.save(); painter.setClipPath(path)
                painter.setOpacity(max(0.0, min(1.0, self._float(theme, "material.text_silver.texture_opacity", 0.10))))
                painter.drawPixmap(rect, target, QtCore.QRectF(target.rect())); painter.restore()

    def _paint_sparse_marks(self, painter, rect, theme, *, seed=0) -> None:
        if not self._bool(theme, "material.marks.enabled", True):
            return
        count = max(0, self._int(theme, "material.marks.count", 5))
        if count <= 0 or rect.width() < 120 or rect.height() < 50:
            return
        alpha = max(0, min(40, self._int(theme, "material.marks.alpha", 8)))
        rng = random.Random(seed * 9973 + 31)
        painter.save()
        for _ in range(count):
            x = rect.left() + rng.random() * rect.width()
            y = rect.top() + rng.random() * rect.height()
            length = 7.0 + rng.random() * 24.0
            bright = rng.random() > 0.58
            c = QtGui.QColor(255, 255, 255, alpha) if bright else QtGui.QColor(0, 0, 0, alpha + 4)
            painter.setPen(QtGui.QPen(c, 0.55))
            painter.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(min(rect.right(), x + length), y + rng.uniform(-0.7, 0.7)))
        painter.restore()

    # ------------------------------------------------------------------
    # Public painting API
    # ------------------------------------------------------------------
    def _paint_shell_direct(self, painter: QtGui.QPainter, rect: QtCore.QRectF, radius: float, theme, *, phase: float = 0.0) -> None:
        if not self.enabled(theme):
            return
        painter.save()
        path = self._rounded_path(rect, radius)
        painter.setClipPath(path)

        base = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, self._color(theme, "material.shell.top", "#101012"))
        base.setColorAt(0.32, self._color(theme, "material.shell.mid", "#0A0A0B"))
        base.setColorAt(0.72, self._color(theme, "material.shell.low", "#050506"))
        base.setColorAt(1.0, self._color(theme, "material.shell.bottom", "#020203"))
        painter.fillPath(path, base)

        # Soft moving graphite body light.
        phase_r = phase * math.tau
        light_x = self._float(theme, "material.light.x", 0.26) + math.sin(phase_r) * self._float(theme, "material.light.motion_x", 0.012)
        light_y = self._float(theme, "material.light.y", -0.12) + math.cos(phase_r) * self._float(theme, "material.light.motion_y", 0.008)
        radius_px = max(rect.width(), rect.height()) * self._float(theme, "material.light.radius", 0.78)
        glow = QtGui.QRadialGradient(
            rect.left() + rect.width() * light_x,
            rect.top() + rect.height() * light_y,
            radius_px,
        )
        glow.setColorAt(0.0, self._color(theme, "material.light.key", "rgba(255,255,255,30)"))
        glow.setColorAt(0.27, self._color(theme, "material.light.mid", "rgba(255,255,255,12)"))
        glow.setColorAt(0.66, self._color(theme, "material.light.soft", "rgba(255,255,255,3)"))
        glow.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        painter.fillPath(path, glow)

        # Hard diagonal shadow cut, analogous to objects interrupting a studio light.
        cut = QtGui.QLinearGradient(rect.left() + rect.width() * 0.20, rect.top(), rect.right(), rect.bottom())
        cut.setColorAt(0.0, QtGui.QColor(0, 0, 0, 0))
        cut.setColorAt(0.55, self._color(theme, "material.shadow.cut", "rgba(0,0,0,20)"))
        cut.setColorAt(0.82, self._color(theme, "material.shadow.cut_deep", "rgba(0,0,0,110)"))
        cut.setColorAt(1.0, self._color(theme, "material.shadow.cut_bottom", "rgba(0,0,0,170)"))
        painter.fillPath(path, cut)

        self._paint_micro_texture(
            painter,
            rect,
            theme,
            seed=421,
            path_key="material.background_texture",
            opacity_key="material.background_texture_opacity",
        )
        # User background materials can be intentionally strong (V16 defaults to
        # 0.90). A neutral black film keeps UI contrast predictable without
        # baking lighting into the source texture itself.
        texture_scrim = self._color(theme, "material.background_texture_scrim", "rgba(0,0,0,118)")
        if texture_scrim.alpha() > 0:
            painter.fillPath(path, texture_scrim)

        # Strong EDC-style vignette: center stays graphite, perimeter falls into black.
        vignette = QtGui.QRadialGradient(
            rect.center().x(),
            rect.top() + rect.height() * 0.44,
            max(rect.width(), rect.height()) * self._float(theme, "material.vignette.radius", 0.72),
        )
        vignette.setColorAt(0.0, QtGui.QColor(0, 0, 0, 0))
        vignette.setColorAt(0.58, QtGui.QColor(0, 0, 0, 0))
        vignette.setColorAt(1.0, self._color(theme, "material.vignette.color", "rgba(0,0,0,185)"))
        painter.fillPath(path, vignette)

        painter.restore()

    def paint_shell(self, painter: QtGui.QPainter, rect: QtCore.QRectF, radius: float, theme, *, phase: float = 0.0) -> None:
        """Render the complete Obsidian shell at higher resolution then downsample.

        This specifically fixes the high-contrast white-on-black staircase that
        remained visible on the outer rounded corners at 100% Windows scale.
        """
        if not self.enabled(theme):
            return
        ss = max(1, min(3, self._int(theme, "material.render.shell_supersample", 2)))
        if ss <= 1 or rect.width() < 2 or rect.height() < 2:
            return self._paint_shell_direct(painter, rect, radius, theme, phase=phase)
        w = max(1, int(math.ceil(rect.width())))
        h = max(1, int(math.ceil(rect.height())))
        hi = QtGui.QImage(w * ss, h * ss, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        hi.fill(QtCore.Qt.GlobalColor.transparent)
        hp = QtGui.QPainter(hi)
        hp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        hp.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        hp.scale(ss, ss)
        self._paint_shell_direct(hp, QtCore.QRectF(0.0, 0.0, rect.width(), rect.height()), radius, theme, phase=phase)
        hp.end()
        low = hi.scaled(w, h, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
        painter.drawImage(rect, low, QtCore.QRectF(low.rect()))

    def paint_panel(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        radius: float,
        theme,
        *,
        seed: int = 0,
        hover: float = 0.0,
    ) -> None:
        if not self.enabled(theme):
            return
        painter.save()
        self._paint_contact_shadow(painter, rect, radius, theme, strength=1.0 + 0.08 * hover)

        path = self._rounded_path(rect, radius)
        painter.setClipPath(path)
        fill = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        fill.setColorAt(0.0, self._color(theme, "material.panel.top", "#151517"))
        fill.setColorAt(0.28, self._color(theme, "material.panel.mid", "#0D0D0F"))
        fill.setColorAt(0.75, self._color(theme, "material.panel.low", "#080809"))
        fill.setColorAt(1.0, self._color(theme, "material.panel.bottom", "#040405"))
        painter.fillPath(path, fill)

        # Local light catches only one corner instead of evenly brightening the card.
        local = QtGui.QRadialGradient(
            rect.left() + rect.width() * self._float(theme, "material.panel_light.x", 0.16),
            rect.top() + rect.height() * self._float(theme, "material.panel_light.y", 0.02),
            max(rect.width(), rect.height()) * self._float(theme, "material.panel_light.radius", 0.76),
        )
        local.setColorAt(0.0, self._color(theme, "material.panel_light.color", "rgba(255,255,255,18)"))
        local.setColorAt(0.52, QtGui.QColor(255, 255, 255, int(3 + 5 * hover)))
        local.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        painter.fillPath(path, local)

        self._paint_micro_texture(
            painter,
            rect,
            theme,
            seed=seed + 811,
            path_key="material.card_texture",
            opacity_key="material.card_texture_opacity",
        )
        if hover > 0.001:
            tex = self._texture_pixmap(theme, "material.card_texture")
            boost = self._float(theme, "material.panel.hover_texture_boost", 0.035) * hover
            if not tex.isNull() and boost > 0:
                target = self._cover(tex, rect.size().toSize())
                if not target.isNull():
                    painter.save(); painter.setOpacity(max(0.0, min(1.0, boost)))
                    painter.drawPixmap(rect, target, QtCore.QRectF(target.rect())); painter.restore()

        bottom = QtGui.QLinearGradient(0, rect.top() + rect.height() * 0.55, 0, rect.bottom())
        bottom.setColorAt(0.0, QtGui.QColor(0, 0, 0, 0))
        bottom.setColorAt(1.0, self._color(theme, "material.panel.bottom_occlusion", "rgba(0,0,0,118)"))
        painter.fillPath(path, bottom)
        self._paint_sparse_marks(painter, rect, theme, seed=seed)
        painter.setClipping(False)
        self._paint_imperfect_edge(painter, rect, radius, theme, seed=seed, intensity=1.0 + 0.12 * hover)
        painter.restore()

    def paint_row(self, painter, rect: QtCore.QRectF, theme, *, selected=False, hovered=False, seed=0) -> None:
        if not self.enabled(theme):
            return
        painter.save()
        strength = 1.0 if selected else (0.55 if hovered else 0.0)
        if selected or hovered:
            grad = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
            top = self._color(theme, "material.row.selected_top" if selected else "material.row.hover_top", "rgba(255,255,255,22)")
            bottom = self._color(theme, "material.row.selected_bottom" if selected else "material.row.hover_bottom", "rgba(255,255,255,6)")
            grad.setColorAt(0.0, top)
            grad.setColorAt(0.60, bottom)
            grad.setColorAt(1.0, QtGui.QColor(0, 0, 0, 10 if selected else 4))
            painter.fillRect(rect, grad)
        row_tex = self._texture_pixmap(theme, "material.card_texture")
        if not row_tex.isNull():
            key = "material.row.texture_opacity_selected" if selected else ("material.row.texture_opacity_hover" if hovered else "material.row.texture_opacity_base")
            default = 0.075 if selected else (0.035 if hovered else 0.018)
            opacity = self._float(theme, key, default)
            target = self._cover(row_tex, rect.size().toSize())
            if not target.isNull() and opacity > 0:
                painter.save(); painter.setOpacity(max(0.0, min(1.0, opacity)))
                painter.drawPixmap(rect, target, QtCore.QRectF(target.rect())); painter.restore()
        if selected or hovered:
            highlight = self._color(theme, "material.row.edge", "rgba(255,255,255,34)")
            highlight.setAlpha(int(highlight.alpha() * strength))
            pen = QtGui.QPen(highlight, 0.75); pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(rect.topLeft() + QtCore.QPointF(2, 0.5), rect.topRight() - QtCore.QPointF(2, -0.5))
        painter.restore()

    def paint_header_surface(
        self, painter, rect: QtCore.QRectF, theme, *, seed: int = 0,
        round_left: bool = False, round_right: bool = False,
    ) -> None:
        """Paint a graphite table header clipped to the real outer header shape.

        V22 fixes the square texture visible behind rounded QHeaderView corners:
        the texture, gradient and light are all clipped to one path. Internal
        sections remain square, while only the first/last visible section owns
        the external rounded corners.
        """
        if not self.enabled(theme):
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        radius = self._float(theme, "material.header.radius", 9.0)

        path = QtGui.QPainterPath()
        if round_left and round_right:
            path.addRoundedRect(rect, radius, radius)
        elif round_left:
            path.addRoundedRect(rect, radius, radius)
            # Square the internal/right side without affecting left corners.
            path.addRect(QtCore.QRectF(
                rect.left() + radius, rect.top(),
                max(0.0, rect.width() - radius + 1.0), rect.height()
            ))
        elif round_right:
            path.addRoundedRect(rect, radius, radius)
            # Square the internal/left side without affecting right corners.
            path.addRect(QtCore.QRectF(
                rect.left() - 1.0, rect.top(),
                max(0.0, rect.width() - radius + 1.0), rect.height()
            ))
        else:
            path.addRect(rect)

        painter.setClipPath(path)

        grad = QtGui.QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        grad.setColorAt(0.0, self._color(theme, "material.header.top", "#18181B"))
        grad.setColorAt(1.0, self._color(theme, "material.header.bottom", "#0A0A0C"))
        painter.fillPath(path, grad)

        tex = self._texture_pixmap(theme, "material.card_texture")
        op = self._float(theme, "material.header.texture_opacity", 0.13)
        if not tex.isNull() and op > 0:
            target = self._cover(tex, rect.size().toSize())
            if not target.isNull():
                painter.save()
                painter.setClipPath(path)
                painter.setOpacity(max(0.0, min(1.0, op)))
                painter.drawPixmap(rect, target, QtCore.QRectF(target.rect()))
                painter.restore()

        light = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
        light.setColorAt(0.0, self._color(theme, "material.header.top_light", "rgba(255,255,255,26)"))
        light.setColorAt(0.55, QtGui.QColor(255,255,255,5))
        light.setColorAt(1.0, QtGui.QColor(255,255,255,0))
        pen = QtGui.QPen(QtGui.QBrush(light), 0.8)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QtCore.QLineF(rect.left()+2, rect.top()+0.6, rect.right()-2, rect.top()+0.6))

        painter.setPen(QtGui.QPen(
            self._color(theme, "material.header.bottom_line", "rgba(0,0,0,185)"), 1.0
        ))
        painter.drawLine(QtCore.QLineF(rect.left(), rect.bottom()-0.4, rect.right(), rect.bottom()-0.4))
        painter.setClipping(False)

        # One clean outer rim only on the real external sides.
        rim = QtGui.QPen(self._color(theme, "material.header.rim", "rgba(255,255,255,35)"), 0.75)
        rim.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        rim.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(rim)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        if round_left or round_right:
            painter.drawPath(path)

        painter.restore()

    def paint_button_surface(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        radius: float,
        theme,
        *,
        hovered: bool = False,
        pressed: bool = False,
        checked: bool = False,
        enabled: bool = True,
        primary: bool = False,
        seed: int = 0,
        edge_occlusion: bool = True,
    ) -> None:
        """Paint a complete tactile button material below its label/icon."""
        if not self.enabled(theme):
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        hover = 1.0 if hovered else 0.0
        active = checked or primary
        shadow_strength = 0.72 if pressed else (1.04 if hovered else 0.90)
        self._paint_contact_shadow(painter, rect, radius, theme, strength=shadow_strength)
        path = self._rounded_path(rect, radius)
        painter.setClipPath(path)

        if active:
            top_key = "material.button.primary_top"
            low_key = "material.button.primary_bottom"
            dtop, dbottom = "#18181B", "#070708"
        else:
            top_key = "material.button.top"
            low_key = "material.button.bottom"
            dtop, dbottom = "#111113", "#050506"
        if pressed:
            top_key = "material.button.pressed_surface_top"
            low_key = "material.button.pressed_surface_bottom"
            dtop, dbottom = "#050506", "#101012"
        elif hovered:
            top_key = "material.button.hover_top"
            low_key = "material.button.hover_bottom"
            dtop, dbottom = "#202024", "#0A0A0C"

        fill = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        fill.setColorAt(0.0, self._color(theme, top_key, dtop))
        fill.setColorAt(0.62, self._color(theme, low_key, dbottom))
        fill.setColorAt(1.0, self._color(theme, "material.button.deep", "#030304"))
        painter.fillPath(path, fill)

        tex = self._texture_pixmap(theme, "material.button_texture")
        if not tex.isNull():
            opacity = max(0.0, min(1.0, self._float(theme, "material.button_texture_opacity", 0.12)))
            if hovered:
                opacity = min(1.0, opacity + self._float(theme, "material.button.hover_texture_boost", 0.035))
            if not enabled:
                opacity *= 0.35
            target = self._cover(tex, rect.size().toSize())
            if not target.isNull() and opacity > 0:
                painter.save()
                painter.setOpacity(opacity)
                painter.drawPixmap(rect, target, QtCore.QRectF(target.rect()))
                painter.restore()
                film = self._color(theme, "material.button.texture_scrim", "rgba(0,0,0,118)")
                if film.alpha() > 0:
                    painter.fillPath(path, film)
        else:
            self._paint_micro_texture(
                painter, rect, theme, seed=seed + 1701,
                opacity_key="material.button.fallback_noise_opacity",
            )

        # Hover is studio light catching the object, not a flat color swap.
        if hovered and enabled:
            local = QtGui.QRadialGradient(
                rect.left() + rect.width() * 0.18,
                rect.top() - rect.height() * 0.12,
                max(rect.width(), rect.height()) * 0.82,
            )
            local.setColorAt(0.0, self._color(theme, "material.button.hover_light", "rgba(255,255,255,28)"))
            local.setColorAt(0.48, QtGui.QColor(255,255,255,5))
            local.setColorAt(1.0, QtGui.QColor(255,255,255,0))
            painter.fillPath(path, local)

        if pressed:
            painter.fillPath(path, self._color(theme, "material.button.pressed_overlay", "rgba(0,0,0,78)"))

        painter.setClipping(False)
        intensity = 1.34 if hovered else (1.05 if active else 0.82)
        if pressed:
            intensity = 0.50
        if not enabled:
            intensity *= 0.38
        self._paint_imperfect_edge(
            painter, rect.adjusted(0.65,0.65,-0.65,-0.65), radius,
            theme, seed=seed, intensity=intensity, occlusion=edge_occlusion,
        )
        painter.restore()

    def paint_scrollbar(
        self,
        painter: QtGui.QPainter,
        outer: QtCore.QRectF,
        handle: QtCore.QRectF,
        theme,
        *,
        hovered: bool = False,
        orientation: QtCore.Qt.Orientation = QtCore.Qt.Orientation.Vertical,
        seed: int = 0,
    ) -> None:
        if not self.enabled(theme):
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        track_radius = min(outer.width(), outer.height(), self._float(theme, "material.scrollbar.radius", 7.0))
        track_path = self._rounded_path(outer.adjusted(1,1,-1,-1), track_radius)
        painter.fillPath(track_path, self._color(theme, "material.scrollbar.track", "rgba(0,0,0,128)"))
        # A low-opacity card texture gives the rail the same physical finish.
        tex = self._texture_pixmap(theme, "material.card_texture")
        if not tex.isNull():
            target = self._cover(tex, outer.size().toSize())
            painter.save(); painter.setClipPath(track_path)
            painter.setOpacity(max(0.0, min(1.0, self._float(theme, "material.scrollbar.track_texture_opacity", 0.08))))
            painter.drawPixmap(outer, target, QtCore.QRectF(target.rect()))
            painter.restore()
        if handle.width() > 0 and handle.height() > 0:
            h = handle.adjusted(1.0,1.0,-1.0,-1.0)
            hr = min(h.width(), h.height(), self._float(theme, "material.scrollbar.handle_radius", 6.0))
            hp = self._rounded_path(h, hr)
            grad = QtGui.QLinearGradient(h.topLeft(), h.bottomRight())
            grad.setColorAt(0.0, self._color(theme, "material.scrollbar.handle_top_hover" if hovered else "material.scrollbar.handle_top", "#4A4A50" if hovered else "#333338"))
            grad.setColorAt(1.0, self._color(theme, "material.scrollbar.handle_bottom_hover" if hovered else "material.scrollbar.handle_bottom", "#171719"))
            painter.fillPath(hp, grad)
            painter.setClipPath(hp)
            if not tex.isNull():
                target = self._cover(tex, h.size().toSize())
                painter.save(); painter.setOpacity(max(0.0, min(1.0, self._float(theme, "material.scrollbar.handle_texture_opacity", 0.16))))
                painter.drawPixmap(h, target, QtCore.QRectF(target.rect())); painter.restore()
            painter.setClipping(False)
            self._paint_imperfect_edge(painter, h, hr, theme, seed=seed, intensity=0.72 if not hovered else 1.05)
        painter.restore()

    def paint_button_edge(self, painter, rect: QtCore.QRectF, radius: float, theme, *, hovered=False, pressed=False, seed=0) -> None:
        if not self.enabled(theme):
            return
        painter.save()
        intensity = 1.15 if hovered else 0.72
        if pressed:
            intensity = 0.35
        self._paint_imperfect_edge(painter, rect.adjusted(0.7, 0.7, -0.7, -0.7), radius, theme, seed=seed, intensity=intensity)
        if pressed:
            # Pressed controls reverse the bevel slightly.
            painter.setPen(QtGui.QPen(self._color(theme, "material.button.pressed_top", "rgba(0,0,0,180)"), 1.0))
            painter.drawLine(QtCore.QLineF(
                rect.left() + radius,
                rect.top() + 1.0,
                rect.right() - radius,
                rect.top() + 1.0,
            ))
        painter.restore()


OBSIDIAN_MATERIAL = ObsidianMaterialEngine()
