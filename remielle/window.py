from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk

from .config import (APP_DIR, _DEFAULT_COORDINATES, SETTINGS_PATH, CONFIG_PATH,
                     TRANSPARENT_COLOR, _TRANSPARENT_RGB, LOGGER,
                     expand_path, load_json)
from .win32_helpers import (_win32_set_clickthrough, _win32_remove_window_border,
                            _win32_clip_window_region, _get_virtual_screen_bounds,
                            _get_monitor_work_area_for_rect,
                            _native_create_menu, _native_destroy_menu,
                            _native_add_item, _native_add_check, _native_add_sep,
                            _native_add_sub, _native_track)

def _prepare_colorkey_frame(
    frame: Image.Image,
    transparent_rgb: tuple[int, int, int] = _TRANSPARENT_RGB,
    alpha_threshold: int = 96,
) -> Image.Image:
    """Convert an RGBA image to RGB, eliminating semi-transparent pixels.

    Tkinter's ``-transparentcolor`` uses ``LWA_COLORKEY`` — binary
    transparency.  Semi-transparent anti-aliased edges (alpha between
    1 and 254) don't match the exact colour key and appear as dark
    fringes.

    This function thresholds alpha:
    - ``alpha < threshold`` → exact *transparent_rgb* (will be keyed out)
    - ``alpha >= threshold`` → fully opaque original colour

    Uses Pillow channel ops instead of per-pixel loops for speed.
    *frame* must already be RGBA.
    """
    alpha = frame.getchannel("A")
    # 0 = transparent, 255 = opaque
    binary_mask = alpha.point(
        lambda v: 255 if v >= alpha_threshold else 0,
    )
    foreground = frame.convert("RGB")
    background = Image.new("RGB", frame.size, transparent_rgb)
    return Image.composite(foreground, background, binary_mask)


def _clamp_window_to_bounds(
    x: int,
    y: int,
    width: int,
    height: int,
    bounds: tuple[int, int, int, int],
    margin: int,
) -> tuple[int, int]:
    """Keep a window fully visible when it fits, otherwise keep a handle."""
    left, top, right, bottom = bounds
    area_width = max(1, right - left)
    area_height = max(1, bottom - top)
    margin = max(1, int(margin))
    if width <= area_width:
        x = max(left, min(int(x), right - width))
    else:
        x = max(left - width + margin, min(int(x), right - margin))
    if height <= area_height:
        y = max(top, min(int(y), bottom - height))
    else:
        y = max(top - height + margin, min(int(y), bottom - margin))
    return x, y


class RemielleWindow:
    def __init__(self, config: dict, on_exit: Callable[[], None],
                 on_acknowledge: Callable[[], None] | None = None) -> None:
        self.config = config
        self.on_exit = on_exit
        self.on_acknowledge = on_acknowledge or (lambda: None)
        self.asset_dir = expand_path(config["asset_dir"])
        self.coordinates = load_json(
            expand_path(config["coordinate_file"]), fallback=_DEFAULT_COORDINATES,
        )
        rendering = config["rendering"]
        self._alpha_threshold: int = int(rendering["alpha_threshold"])
        self._scale_min: float = float(rendering["scale_min"])
        self._scale_max: float = float(rendering["scale_max"])
        self._visibility_margin: int = int(rendering["visibility_margin_px"])
        self._default_offset_x: int = int(rendering["default_position_offset_x"])
        self._default_offset_y: int = int(rendering["default_position_offset_y"])
        default_scale = float(config["default_scale"])
        indicator_cfg = config.get("indicator", {})
        self.settings = {
            "x": 155,
            "y": 448,
            "scale": default_scale,
            "indicator_enabled": bool(indicator_cfg.get("enabled", True)),
            "indicator_position": str(indicator_cfg.get("position", "right")),
            "indicator_orientation": str(
                indicator_cfg.get("orientation", "horizontal")
            ),
            **load_json(SETTINGS_PATH),
        }
        self.scale = max(self._scale_min, min(self._scale_max, float(self.settings.get("scale", 1.0))))
        self.current_action: str | None = None
        self.current_status_text = "等待 AI 任务"
        self.frames: list[ImageTk.PhotoImage] = []
        self.delays: list[int] = []
        self.frame_index = 0
        self.loops_remaining: int | None = None
        self.animation_token = 0
        self._next_frame_deadline = 0.0
        self.visible = False
        self._on_end: Callable[[], None] | None = None
        self.drag_origin: tuple[int, int, int, int] | None = None
        self._drag_previous_action: str | None = None
        self._drag_previous_loops: int | None = None
        self._resume_after_tray: bool = False
        # ── GIF frame cache ──
        # Key: gif_name, Value: (frames, delays)
        self._frame_cache: dict[str, tuple[list[ImageTk.PhotoImage], list[int]]] = {}
        self._cache_order: list[str] = []               # LRU: front=recent, back=oldest
        self._scale_settle_job: str | None = None
        self._cache_max: int = int(config["frame_cache_max"])

        self._last_saved_settings: str | None = None  # dedup writes

        self.root = tk.Tk()
        self._tray = None
        self._panel = None
        self.root.withdraw()
        self.root.title("蕾米 AI 助手")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(config["topmost"]))
        if sys.platform == "win32":
            self.root.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.root.configure(bg=TRANSPARENT_COLOR)
        # Closing the window should hide it, not destroy it — the tray
        # icon stays alive so the user can bring the pet back.
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        # Retrieve the native window handle *after* the window is mapped
        # so we can toggle click-through behaviour.
        self._hwnd: int = 0
        if sys.platform == "win32":
            try:
                self._hwnd = int(self.root.frame(), 0)
            except Exception:
                self._hwnd = 0
            if self._hwnd:
                _win32_remove_window_border(self._hwnd)
                # ── System-tray icon ──
                icon_path = self.asset_dir.parent / "remielle.ico"
                if icon_path.exists():
                    from .tray import TrayIcon
                    self._tray = TrayIcon(
                        self._hwnd, self, str(icon_path), "蕾米 AI 助手")

        self.canvas = tk.Canvas(
            self.root,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            borderwidth=0,
            relief="flat",
            bd=0,
        )
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")

        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-3>", self._open_menu)

        # ── Right-click context menu (native Win32, not tkinter) ──
        # tk_popup on overrideredirect windows produces unresolvable
        # z-order flicker; TrackPopupMenu is independent of Tk's
        # window system and cannot flicker.
        self._menu_active = False  # pause frame rendering while menu is posted

        # Value holders for menu check/radio state (not attached to any widget)
        display_cfg = self.config["display"]
        self.persistent_var = tk.BooleanVar(value=display_cfg["persistent"])
        self.autohide_var = tk.BooleanVar(
            value=display_cfg["auto_hide_after_complete"])
        self.scale_var = tk.DoubleVar(value=self.scale)
        self.indicator_enabled_var = tk.BooleanVar(
            value=bool(self.settings.get("indicator_enabled", True))
        )
        self._pet_origin_x = 0
        self._pet_origin_y = 0
        self._indicator_box: tuple[int, int, int, int] | None = None
        self._indicator_layout_key: tuple[object, ...] | None = None
        self.current_status_text = "等待 AI 任务"
        # Snapshot of bridge state pushed from app._poll() for the menu header
        self._status_info: dict[str, str] = {}
        self._init_indicator_window()

        self._compute_layout()
        self._apply_geometry()

    def _init_indicator_window(self) -> None:
        """Create the tiny translucent companion window used by the HUD."""
        palette = self.config.get("ui", {})
        self._indicator_glass = palette.get("indicator_glass", "#d8c7cd")
        self._indicator_alpha = max(
            0.45, min(0.95, float(palette.get("indicator_alpha", 0.72)))
        )
        self.indicator_top = tk.Toplevel(self.root)
        self.indicator_top.withdraw()
        self.indicator_top.title("蕾米任务状态")
        self.indicator_top.overrideredirect(True)
        self.indicator_top.attributes("-topmost", bool(self.config["topmost"]))
        self.indicator_top.configure(bg=TRANSPARENT_COLOR)
        if sys.platform == "win32":
            self.indicator_top.wm_attributes(
                "-transparentcolor", TRANSPARENT_COLOR
            )
            self.indicator_top.wm_attributes("-alpha", self._indicator_alpha)
        self.indicator_canvas = tk.Canvas(
            self.indicator_top,
            bg=TRANSPARENT_COLOR,
            bd=0,
            highlightthickness=0,
        )
        self.indicator_canvas.pack(fill="both", expand=True)
        self._indicator_status_font = tkfont.Font(
            root=self.indicator_top,
            family="Microsoft YaHei UI",
            size=8,
        )
        self._indicator_token_font = tkfont.Font(
            root=self.indicator_top,
            family="Segoe UI",
            size=8,
        )
        self.indicator_top.update_idletasks()
        try:
            self._indicator_hwnd = self.indicator_top.winfo_id()
        except tk.TclError:
            self._indicator_hwnd = 0
        if self._indicator_hwnd:
            _win32_set_clickthrough(self._indicator_hwnd, True)

    def _compute_layout(self) -> None:
        min_x = 0
        min_y = 0
        max_x = 1
        max_y = 1
        for gif_name, offset in self.coordinates.items():
            path = self.asset_dir / gif_name
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except OSError:
                continue
            x = int(offset.get("x", 0))
            y = int(offset.get("y", 0))
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x + width)
            max_y = max(max_y, y + height)
        self.base_min_x = min_x
        self.base_min_y = min_y
        self.base_width = max_x - min_x
        self.base_height = max_y - min_y

    def _screen_visible_geom(self, width: int, height: int) -> tuple[int, int]:
        """Return coordinates clamped to one real monitor's work area."""
        try:
            virt = _get_virtual_screen_bounds()
            if virt:
                v_left, v_top, v_right, v_bottom = virt
            else:
                v_left, v_top = 0, 0
                v_right = self.root.winfo_screenwidth()
                v_bottom = self.root.winfo_screenheight()
        except Exception:
            return (155, 448)

        # Use saved position, or default to bottom-right of the desktop
        m = self._visibility_margin
        x = int(self.settings.get("x", v_right - width - self._default_offset_x))
        y = int(self.settings.get("y", v_bottom - height - self._default_offset_y))

        # The virtual desktop is only an outer bounding box and may include
        # empty gaps between monitors.  Clamp against the nearest monitor's
        # taskbar-excluding work area instead.
        bounds = _get_monitor_work_area_for_rect(x, y, width, height)
        if bounds is None:
            bounds = (v_left, v_top, v_right, v_bottom)
        return _clamp_window_to_bounds(x, y, width, height, bounds, m)

    def _apply_geometry(self, *, save: bool = True) -> None:
        pet_width = max(1, round(self.base_width * self.scale))
        pet_height = max(1, round(self.base_height * self.scale))
        width, height = pet_width, pet_height
        self._pet_origin_x = 0
        self._pet_origin_y = 0
        self._indicator_box = None

        if self._indicator_should_show():
            badge_width, badge_height = self._indicator_dimensions()
            gap = 5
            if self.indicator_position in {"top", "bottom"}:
                width = max(pet_width, badge_width)
                height = pet_height + gap + badge_height
                self._pet_origin_x = max(0, (width - pet_width) // 2)
                badge_x = max(0, (width - badge_width) // 2)
                if self.indicator_position == "top":
                    self._pet_origin_y = badge_height + gap
                    self._indicator_box = (
                        badge_x, 0, badge_x + badge_width, badge_height,
                    )
                else:
                    self._indicator_box = (
                        badge_x, pet_height + gap,
                        badge_x + badge_width, pet_height + gap + badge_height,
                    )
            else:
                width = pet_width + gap + badge_width
                height = max(pet_height, badge_height)
                self._pet_origin_y = max(0, (height - pet_height) // 2)
                badge_y = max(0, (height - badge_height) // 2)
                if self.indicator_position == "left":
                    self._pet_origin_x = badge_width + gap
                    self._indicator_box = (
                        0, badge_y, badge_width, badge_y + badge_height,
                    )
                else:
                    self._indicator_box = (
                        pet_width + gap, badge_y,
                        pet_width + gap + badge_width, badge_y + badge_height,
                    )
        x, y = self._screen_visible_geom(width, height)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.settings["x"] = x
        self.settings["y"] = y
        self.canvas.configure(width=width, height=height)
        if self._hwnd:
            _win32_clip_window_region(self._hwnd, width, height)
        self._draw_indicator()
        if save:
            self._save_settings()

    def _load_action(self, gif_name: str) -> None:
        path = self.asset_dir / gif_name
        frames: list[ImageTk.PhotoImage] = []
        delays: list[int] = []
        with Image.open(path) as gif:
            frame_count = getattr(gif, "n_frames", 1)
            for index in range(frame_count):
                gif.seek(index)
                frame = gif.copy().convert("RGBA")
                if self.scale != 1.0:
                    frame = frame.resize(
                        (
                            max(1, round(frame.width * self.scale)),
                            max(1, round(frame.height * self.scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                # Must be called *after* resize — Lanczos generates
                # new semi-transparent pixels at edges that need to
                # be clamped before Tk's binary colour-key sees them.
                frame = _prepare_colorkey_frame(
                    frame,
                    transparent_rgb=_TRANSPARENT_RGB,
                    alpha_threshold=self._alpha_threshold,
                )
                frames.append(ImageTk.PhotoImage(frame, master=self.root))
                delays.append(max(15, int(gif.info.get("duration", 30))))
        self.frames = frames
        self.delays = delays
        self.current_action = gif_name
        self.frame_index = 0

    # ── GIF frame cache ─────────────────────────────────────────────

    def _cache_get(self, gif_name: str) -> tuple[list[ImageTk.PhotoImage], list[int]] | None:
        """Return cached (frames, delays) for *gif_name*, or ``None``.

        Bumps *gif_name* to the front of the LRU order on a hit.
        """
        entry = self._frame_cache.get(gif_name)
        if entry is None:
            return None
        # Bump to front of LRU
        if gif_name in self._cache_order:
            self._cache_order.remove(gif_name)
        self._cache_order.insert(0, gif_name)
        return entry

    def _cache_put(self, gif_name: str,
                   frames: list[ImageTk.PhotoImage],
                   delays: list[int]) -> None:
        """Store *frames* + *delays* under *gif_name*, evicting oldest entry
        if the cache exceeds ``_cache_max``."""
        # Evict oldest if at capacity
        while len(self._cache_order) >= self._cache_max:
            old = self._cache_order.pop()
            old_frames, _old_delays = self._frame_cache.pop(old, (None, None))
            if old_frames:
                for f in old_frames:
                    try:
                        f.__del__()
                    except Exception:
                        pass
        # Insert new entry
        self._frame_cache[gif_name] = (frames, delays)
        if gif_name in self._cache_order:
            self._cache_order.remove(gif_name)
        self._cache_order.insert(0, gif_name)

    def _cache_clear(self) -> None:
        """Drop all cached frames (called on scale change)."""
        for _gif_name, (frames, _delays) in self._frame_cache.items():
            for f in frames:
                try:
                    f.__del__()
                except Exception:
                    pass
        self._frame_cache.clear()
        self._cache_order.clear()

    def set_status_text(self, text: str) -> None:
        """Store status text (shown as first disabled item in the native menu)."""
        self.current_status_text = text

    def set_status_info(self, info: dict[str, str]) -> None:
        """Receive a snapshot of bridge state for the right-click menu header.

        Called from ``BridgeApp._poll()`` each cycle.  Keys control the
        display order in the menu:

        - ``mode``     — current display mode label (e.g. "待机中")
        - ``active``   — active task count
        - ``unread``   — validated unread thread count
        - ``claude``   — Claude Code status ("空闲" / "忙碌")
        - ``reviews``  — pending review count
        """
        snapshot = dict(info)
        if snapshot == self._status_info:
            return
        self._status_info = snapshot
        layout_key = (
            self._indicator_should_show(),
            self.indicator_position,
            self.indicator_orientation,
            self._indicator_dimensions() if self._indicator_should_show() else None,
        )
        if layout_key != self._indicator_layout_key:
            self._indicator_layout_key = layout_key
            self._apply_geometry()
        else:
            self._draw_indicator()
        if self._panel is not None and self._panel.top.state() != "withdrawn":
            self._panel.refresh()

    @property
    def indicator_position(self) -> str:
        value = str(self.settings.get("indicator_position", "right"))
        if value == "side":
            value = "right"
        return value if value in {"top", "bottom", "left", "right"} else "right"

    @property
    def indicator_orientation(self) -> str:
        return (
            "horizontal"
            if self.indicator_position in {"top", "bottom"}
            else "vertical"
        )

    def _indicator_content(self) -> tuple[str, str]:
        status = (
            self._status_info.get("indicator_status")
            or self._status_info.get("mode", "")
        )
        short_status = {
            "工作中": "工作", "间歇工作中": "间歇", "思考中": "思考",
            "任务完成待查看": "完成", "有未读消息": "完成",
        }.get(status, status[:2] or "状态")
        tokens = self.format_token_count(
            self._status_info.get("token_total", "0")
        )
        return short_status, tokens

    def _indicator_dimensions(self) -> tuple[int, int]:
        """Measure real font metrics so the indicator never clips text."""
        status_width = max(
            self._indicator_status_font.measure(label)
            for label in ("工作", "思考", "间歇", "完成", "失败", "取消")
        )
        status_group = 4 + 5 + status_width
        token_width = max(
            self._indicator_token_font.measure("999.9M"),
            self._indicator_token_font.measure(self._indicator_content()[1]),
        )
        status_height = self._indicator_status_font.metrics("linespace")
        token_height = self._indicator_token_font.metrics("linespace")
        if self.indicator_orientation == "horizontal":
            return (
                8 + status_group + 14 + token_width + 8,
                max(status_height, token_height) + 8,
            )
        return (
            max(status_group, token_width) + 16,
            status_height + token_height + 16,
        )

    def _indicator_should_show(self) -> bool:
        if not bool(self.indicator_enabled_var.get()):
            return False
        mode = self._status_info.get("mode", "")
        return mode not in {"", "已隐藏", "启动中", "待机中"}

    @staticmethod
    def format_token_count(value: str | int) -> str:
        try:
            number = max(0, int(value))
        except (TypeError, ValueError):
            number = 0
        if number >= 1_000_000:
            return f"{number / 1_000_000:.1f}M".replace(".0M", "M")
        if number >= 1_000:
            return f"{number / 1_000:.1f}K".replace(".0K", "K")
        return str(number)

    def _draw_indicator(self) -> None:
        self.canvas.delete("indicator")  # clean up pre-v2 in-canvas items
        if not self._indicator_box or not self._indicator_should_show():
            self.indicator_top.withdraw()
            return
        x1, y1, x2, y2 = self._indicator_box
        palette = self.config.get("ui", {})
        border = palette.get("indicator_border", "#b89fa9")
        text = palette.get("indicator_text", "#30262a")
        muted = palette.get("indicator_muted", "#66535b")
        accent = palette.get("accent", "#a84664")
        width = max(1, round(x2 - x1))
        height = max(1, round(y2 - y1))
        self.indicator_canvas.configure(width=width, height=height)
        self.indicator_canvas.delete("all")

        # Draw the glass as a rounded Canvas shape on a colour-key window.
        # The outer corners stay truly transparent, avoiding the black corner
        # artifacts produced by SetWindowRgn + whole-window alpha.
        radius = min(13, height // 2)
        diameter = radius * 2
        fill = {"fill": self._indicator_glass, "outline": "", "width": 0}
        self.indicator_canvas.create_rectangle(
            radius, 1, width - radius, height - 1, **fill
        )
        self.indicator_canvas.create_rectangle(
            1, radius, width - 1, height - radius, **fill
        )
        self.indicator_canvas.create_arc(
            1, 1, 1 + diameter, 1 + diameter,
            start=90, extent=90, style="pieslice", **fill,
        )
        self.indicator_canvas.create_arc(
            width - 1 - diameter, 1, width - 1, 1 + diameter,
            start=0, extent=90, style="pieslice", **fill,
        )
        self.indicator_canvas.create_arc(
            width - 1 - diameter, height - 1 - diameter,
            width - 1, height - 1,
            start=270, extent=90, style="pieslice", **fill,
        )
        self.indicator_canvas.create_arc(
            1, height - 1 - diameter, 1 + diameter, height - 1,
            start=180, extent=90, style="pieslice", **fill,
        )
        line = {"fill": border, "width": 1}
        arc = {"style": "arc", "outline": border, "width": 1}
        self.indicator_canvas.create_line(radius, 1, width - radius, 1, **line)
        self.indicator_canvas.create_line(width - 1, radius, width - 1, height - radius, **line)
        self.indicator_canvas.create_line(width - radius, height - 1, radius, height - 1, **line)
        self.indicator_canvas.create_line(1, height - radius, 1, radius, **line)
        self.indicator_canvas.create_arc(1, 1, 1 + diameter, 1 + diameter, start=90, extent=90, **arc)
        self.indicator_canvas.create_arc(width - 1 - diameter, 1, width - 1, 1 + diameter, start=0, extent=90, **arc)
        self.indicator_canvas.create_arc(width - 1 - diameter, height - 1 - diameter, width - 1, height - 1, start=270, extent=90, **arc)
        self.indicator_canvas.create_arc(1, height - 1 - diameter, 1 + diameter, height - 1, start=180, extent=90, **arc)
        self.indicator_canvas.create_line(
            radius + 1, 2, width - radius - 1, 2,
            fill="#ffffff", width=1,
        )
        short_status, token_text = self._indicator_content()
        if self.indicator_orientation == "vertical":
            cx = width / 2
            status_height = self._indicator_status_font.metrics("linespace")
            token_height = self._indicator_token_font.metrics("linespace")
            status_y = 5 + status_height / 2
            divider_y = 7 + status_height
            token_y = divider_y + 4 + token_height / 2
            status_width = 4 + 5 + self._indicator_status_font.measure(short_status)
            status_x = (width - status_width) / 2
            self.indicator_canvas.create_oval(
                status_x, status_y - 2, status_x + 4, status_y + 2,
                fill=accent, outline="",
            )
            self.indicator_canvas.create_text(
                status_x + 9, status_y, text=short_status,
                anchor="w", fill=text, font=self._indicator_status_font,
            )
            self.indicator_canvas.create_line(
                10, divider_y, width - 10, divider_y,
                fill=border, width=1,
            )
            self.indicator_canvas.create_text(
                cx, token_y, text=token_text, fill=muted,
                font=self._indicator_token_font,
            )
        else:
            cy = height / 2
            status_width = self._indicator_status_font.measure(short_status)
            status_group = 4 + 5 + status_width
            divider_x = 8 + status_group + 7
            self.indicator_canvas.create_oval(
                8, cy - 2, 12, cy + 2,
                fill=accent, outline="",
            )
            self.indicator_canvas.create_text(
                17, cy, text=short_status, anchor="w", fill=text,
                font=self._indicator_status_font,
            )
            self.indicator_canvas.create_line(
                divider_x, 7, divider_x, height - 7,
                fill=border, width=1,
            )
            self.indicator_canvas.create_text(
                divider_x + 7, cy, text=token_text,
                anchor="w", fill=muted, font=self._indicator_token_font,
            )

        self.root.update_idletasks()
        screen_x = self.root.winfo_x() + round(x1)
        screen_y = self.root.winfo_y() + round(y1)
        self.indicator_top.geometry(
            f"{width}x{height}+{screen_x}+{screen_y}"
        )
        if self.visible and self.indicator_top.state() == "withdrawn":
            self.indicator_top.deiconify()
            if sys.platform == "win32":
                self.indicator_top.wm_attributes(
                    "-alpha", self._indicator_alpha
                )
            self.indicator_top.lift()

    def toggle_indicator(self) -> None:
        enabled = not bool(self.indicator_enabled_var.get())
        self.indicator_enabled_var.set(enabled)
        self.settings["indicator_enabled"] = enabled
        self._indicator_layout_key = None
        self._apply_geometry()

    def cycle_indicator_position(self) -> None:
        pet_screen_x = self.root.winfo_x() + self._pet_origin_x
        pet_screen_y = self.root.winfo_y() + self._pet_origin_y
        positions = ("right", "bottom", "left", "top")
        current = positions.index(self.indicator_position)
        self.settings["indicator_position"] = positions[(current + 1) % len(positions)]
        self._indicator_layout_key = None
        self._apply_geometry()
        # Keep the character in the same screen position when the indicator
        # moves from one side to another.
        self.settings["x"] = pet_screen_x - self._pet_origin_x
        self.settings["y"] = pet_screen_y - self._pet_origin_y
        self._apply_geometry()

    def acknowledge_results(self) -> None:
        self.on_acknowledge()
        if self._panel is not None:
            self._panel.refresh()

    def _double_click(self, _event: tk.Event) -> None:
        if self._status_info.get("reviews"):
            self.acknowledge_results()

    def play(
        self,
        gif_name: str,
        *,
        loops: int | None = None,
        status_text: str,
        min_play_ms: int = 0,
        on_end: Callable[[], None] | None = None,
    ) -> None:
        self.animation_token += 1
        token = self.animation_token
        self.loops_remaining = loops
        self._on_end = on_end
        self._play_started_at = time.monotonic()
        self._next_frame_deadline = self._play_started_at
        self._min_play_ms = max(0, min_play_ms)
        self.set_status_text(status_text)
        # ── GIF frame cache: avoid redundant disk I/O + image processing ──
        cached = self._cache_get(gif_name)
        if cached is not None:
            self.frames, self.delays = cached
            self.current_action = gif_name
            self.frame_index = 0
        else:
            self._load_action(gif_name)
            self._cache_put(gif_name, self.frames, self.delays)
        LOGGER.info(
            "play action=%s frames=%d loops=%s min_ms=%d on_end=%s",
            gif_name,
            len(self.frames),
            loops,
            self._min_play_ms,
            "yes" if on_end else "no",
        )
        self.show()
        self._render_frame(token)

    def _render_frame(self, token: int) -> None:
        if token != self.animation_token or not self.frames:
            return
        if self._menu_active:
            self._next_frame_deadline = time.monotonic() + 0.05
            self.root.after(50, lambda: self._render_frame(token))
            return
        offset = self.coordinates.get(self.current_action or "", {})
        x = self._pet_origin_x + round(
            (int(offset.get("x", 0)) - self.base_min_x) * self.scale
        )
        y = self._pet_origin_y + round(
            (int(offset.get("y", 0)) - self.base_min_y) * self.scale
        )
        frame = self.frames[self.frame_index]
        self.canvas.coords(self.image_item, x, y)
        self.canvas.itemconfigure(self.image_item, image=frame)
        delay = self.delays[self.frame_index]
        self.frame_index += 1
        if self.frame_index >= len(self.frames):
            self.frame_index = 0
            if self.loops_remaining is not None:
                self.loops_remaining -= 1
                if self.loops_remaining <= 0:
                    elapsed = (time.monotonic() - self._play_started_at) * 1000
                    if elapsed < self._min_play_ms:
                        self.loops_remaining = 1
                    else:
                        cb = self._on_end
                        self._on_end = None
                        self.root.after(delay, cb if cb is not None else self.hide)
                        return
        # Schedule against a monotonic deadline instead of repeatedly adding
        # ``delay`` after rendering.  This prevents small Tk/Python overheads
        # from accumulating into visibly slower playback over time.
        self._next_frame_deadline += delay / 1000
        wait_ms = max(1, round((self._next_frame_deadline - time.monotonic()) * 1000))
        if wait_ms == 1 and time.monotonic() - self._next_frame_deadline > 0.25:
            self._next_frame_deadline = time.monotonic()
        self.root.after(wait_ms, lambda: self._render_frame(token))

    def set_clickthrough(self, enabled: bool) -> None:
        """Toggle WS_EX_TRANSPARENT so clicks pass through the window."""
        if self._hwnd:
            _win32_set_clickthrough(self._hwnd, enabled)

    def show(self, *, clickthrough: bool = False) -> None:
        self.set_clickthrough(clickthrough)
        if not self.visible:
            self.root.deiconify()
            self.root.lift()
            self.visible = True
        self._draw_indicator()

    def hide(self) -> None:
        self.set_clickthrough(False)
        self.animation_token += 1
        self.root.withdraw()
        self.indicator_top.withdraw()
        self.visible = False
        self.set_status_text("等待 AI 任务")
        LOGGER.info("window hidden")

    def hide_to_tray(self) -> None:
        """Temporarily hide the pet while keeping animation state.

        Unlike ``hide()`` (which is used by the state machine and
        intentionally kills the animation timer), this preserves
        ``frame_index`` / ``current_action`` / ``loops_remaining``
        so ``restore_from_tray()`` can pick up where it left off.
        """
        if self.root.state() == "withdrawn":
            return
        self.set_clickthrough(False)
        self.animation_token += 1  # stop current render chain
        self._resume_after_tray = bool(self.frames)
        self.root.withdraw()
        self.indicator_top.withdraw()
        self.visible = False
        LOGGER.info("window hidden to tray")

    def restore_from_tray(self) -> None:
        """Restore the pet window and resume the paused animation."""
        if self.root.state() != "withdrawn":
            return
        self.root.deiconify()
        self.root.lift()
        self.visible = True
        self._draw_indicator()
        if self._resume_after_tray and self.frames:
            self._resume_after_tray = False
            self.animation_token += 1
            token = self.animation_token
            self._render_frame(token)
        LOGGER.info("window restored from tray")

    # ── Menu demo & self-test ────────────────────────────────────

    def _menu_demo_all(self) -> None:
        """Demonstrate all configured visual states."""
        actions = self.config["actions"]
        idle_gif = actions["idle"]
        # (gif_name, status_text, duration_ms)
        sequence: list[tuple[str, str, int]] = [
            (actions["startup"], "演示：启动", 1800),
            (idle_gif, "演示：待机", 1500),
            (actions["ready"], "演示：等待输入", 1500),
            (actions["thinking"], "演示：思考中", 1800),
            (actions["running"], "演示：工作中", 2000),
            (actions["running_intermittent"], "演示：间歇绘制", 2000),
            (actions["complete"], "演示：任务完成", 2500),
            (actions["failed"], "演示：任务失败", 1800),
            (actions["cancelled"], "演示：任务取消", 1600),
        ]

        def _play_step(idx: int) -> None:
            if idx >= len(sequence):
                # All done — return to idle
                if self.config["display"]["persistent"]:
                    self.play(idle_gif, loops=None, status_text="等待 AI 任务")
                else:
                    self.hide()
                return
            gif, text, duration = sequence[idx]
            self.play(gif, loops=None, status_text=text)
            self.root.after(duration, lambda: _play_step(idx + 1))

        _play_step(0)

    def _menu_run_selftest(self) -> None:
        """Called from right-click menu: run self-test and show results."""
        import tkinter.messagebox as mb
        from .app import inspect_assets
        report = inspect_assets(self.config)
        if report["ok"]:
            actions = report.get("actions", {})
            lines = [f"✅ 全部 {len(actions)} 个 GIF 资源就绪："]
            for state, info in actions.items():
                lines.append(
                    f"  · {state}: {info['frames']} 帧, "
                    f"{info['average_fps']} fps, "
                    f"{info['size'][0]}×{info['size'][1]}"
                )
            mb.showinfo("自检通过", "\n".join(lines))
        else:
            mb.showerror("自检失败", "\n".join(report["errors"]))

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            LOGGER.exception("could not save config")

    # ── Right-click menu handlers ───────────────────────────────

    def _open_menu(self, event: tk.Event) -> None:
        self.show_menu_at(event.x_root, event.y_root)

    def show_menu_at(self, x: int, y: int,
                      source: str = "window") -> None:
        """Post the right-click context menu at screen position (x, y).

        Uses Windows native ``TrackPopupMenu`` instead of ``tk_popup``.
        Native menus are rendered by the OS outside Tk's window system,
        so the overrideredirect / transparent-color window can never
        cause z-order flicker.

        *source* distinguishes tray-icon right-clicks from window
        right-clicks so the menu can show different items for each.
        """
        if self.config.get("ui", {}).get("menu_style") == "panel":
            if self._panel is None:
                from .panel import ControlPanel
                self._panel = ControlPanel(self)
            self._panel.show_at(x, y)
            return
        if sys.platform != "win32":
            return  # native menus are Windows-only; silently skip on macOS/Linux
        was_topmost = self.root.attributes("-topmost")
        if was_topmost:
            self.root.attributes("-topmost", False)
        self._menu_active = True
        try:
            self._native_show_menu(x, y, source=source)
        finally:
            self._menu_active = False
            if was_topmost:
                self.root.attributes("-topmost", True)

    def _native_show_menu(self, x: int, y: int,
                           source: str = "window") -> None:
        """Build a native Win32 popup menu and post it at (x, y).

        The menu is built fresh each time so it always reflects the
        current status, scale, toggle state, and autostart label.

        *source* is ``"window"`` for right-click on the pet and
        ``"tray"`` for right-click on the tray icon — the latter
        shows a "退出" item that fully terminates the process.
        """
        menu = _native_create_menu()
        submenus: list[int] = []  # track submenus for cleanup
        dispatch: dict[int, Callable[[], None]] = {}  # item-id → callback
        _id = [2000]  # mutable counter so nested helpers can bump it

        def nid() -> int:
            _id[0] += 1
            return _id[0]

        def add_item(label: str, callback: Callable[[], None], *,
                     disabled: bool = False) -> None:
            mid = nid()
            dispatch[mid] = callback
            _native_add_item(menu, label, mid, disabled=disabled)

        def add_check(label: str, callback: Callable[[], None], *,
                      checked: bool = False) -> None:
            mid = nid()
            dispatch[mid] = callback
            _native_add_check(menu, label, mid, checked=checked)

        # ── Status info (disabled / greyed out) ──────────────────
        info_lines: list[str] = []
        si = self._status_info
        if si.get("mode"):
            info_lines.append(f"状态：{si['mode']}")
        if si.get("active"):
            info_lines.append(f"活跃任务：{si['active']}")
        if si.get("reviews"):
            info_lines.append(f"待查看：{si['reviews']}")
        if si.get("unread", ""):
            info_lines.append(f"未读会话：{si['unread']}")
        if si.get("claude"):
            info_lines.append(f"Claude Code：{si['claude']}")
        if info_lines:
            for line in info_lines:
                add_item(line, lambda: None, disabled=True)
            _native_add_sep(menu)

        # ── Demo & diagnostics ────────────────────────────────
        add_item("演示全部动作", self._menu_demo_all)
        add_item("运行自检", self._menu_run_selftest)
        if self._status_info.get("reviews"):
            add_item("我已查看任务结果", self.acknowledge_results)
        _native_add_sep(menu)

        # ── Size submenu ─────────────────────────────────────
        size_menu = _native_create_menu()
        submenus.append(size_menu)
        current_pct = round(self.scale * 100)
        add_item_id = nid()
        _native_add_item(
            size_menu, f"当前大小：{current_pct}%", add_item_id, disabled=True
        )
        _native_add_sep(size_menu)
        for pct in (50, 75, 100, 150, 200):
            val = pct / 100.0
            smid = nid()
            dispatch[smid] = lambda v=val: self.set_scale(v)
            _native_add_check(size_menu, f"{pct}%", smid,
                              checked=(pct == current_pct))
        _native_add_sub(menu, "大小", size_menu)

        add_item("重置位置和大小", self.reset_geometry)
        _native_add_sep(menu)

        # ── Behaviour toggles ─────────────────────────────────
        add_check("常驻显示", self._toggle_persistent,
                  checked=self.persistent_var.get())
        add_check("查看结果后隐藏", self._toggle_autohide,
                  checked=self.autohide_var.get())
        _native_add_sep(menu)

        # ── Autostart ─────────────────────────────────────────
        add_item(self._autostart_label, self._toggle_autostart)
        _native_add_sep(menu)

        # ── Show / Hide (tray only) ──────────────────────────
        if source == "tray":
            if self.root.state() == "withdrawn":
                add_item("显示桌宠", self.restore_from_tray)
            else:
                add_item("隐藏桌宠", self.hide_to_tray)
            _native_add_sep(menu)

        # ── Exit ─────────────────────────────────────────────
        if source == "tray":
            add_item("退出蕾米 AI 助手", self._exit_app)
        else:
            add_item("退出状态桥", self.on_exit)

        # ── Show & dispatch ──────────────────────────────────
        callback: Callable[[], None] | None = None
        try:
            cmd = _native_track(menu, self._hwnd, x, y)
            LOGGER.debug(
                "native menu: source=%s cmd=%d", source, cmd,
            )
            if cmd and cmd in dispatch:
                callback = dispatch[cmd]
        finally:
            # Destroy submenus first, then the root menu
            for sm in submenus:
                _native_destroy_menu(sm)
            _native_destroy_menu(menu)

        # Callback runs *after* the menu is fully dismissed and
        # destroyed — no re-entrancy risk with the event loop.
        if callback is not None:
            callback()

    def _exit_app(self) -> None:
        """Full exit — all cleanup is handled by ``BridgeApp.stop()``."""
        self.on_exit()

    # ── Behaviour toggles ────────────────────────────────────────

    def _toggle_persistent(self) -> None:
        """Toggle the '常驻显示' checkmark and persist to config."""
        new_val = not self.persistent_var.get()
        self.persistent_var.set(new_val)
        self.config.setdefault("display", {})["persistent"] = new_val
        self._save_config()

    def _toggle_autohide(self) -> None:
        """Toggle the '查看结果后隐藏' checkmark and persist to config."""
        new_val = not self.autohide_var.get()
        self.autohide_var.set(new_val)
        self.config.setdefault("display", {})["auto_hide_after_complete"] = new_val
        self._save_config()

    # ── Autostart management ────────────────────────────────────

    @property
    def _autostart_label(self) -> str:
        from pathlib import Path as _P
        lnk = _P(os.getenv("APPDATA", "")) / (
            "Microsoft/Windows/Start Menu/Programs/Startup/蕾米Codex助手.lnk"
        )
        return "卸载开机自启" if lnk.exists() else "安装开机自启"

    def _toggle_autostart(self) -> None:
        import subprocess as _sp
        from pathlib import Path as _P
        lnk = _P(os.getenv("APPDATA", "")) / (
            "Microsoft/Windows/Start Menu/Programs/Startup/蕾米Codex助手.lnk"
        )
        if lnk.exists():
            try:
                lnk.unlink()
                LOGGER.info("autostart removed: %s", lnk)
            except OSError:
                LOGGER.exception("failed to remove autostart")
        else:
            vbs = APP_DIR / "启动蕾米Codex助手.vbs"
            if not vbs.exists():
                LOGGER.warning("autostart: VBS not found at %s", vbs)
                return
            link_text = str(lnk).replace("'", "''")
            vbs_text = str(vbs).replace("'", "''")
            app_text = str(APP_DIR).replace("'", "''")
            ps = (
                f"$w=New-Object -ComObject WScript.Shell;"
                f"$s=$w.CreateShortcut('{link_text}');"
                f"$s.TargetPath='{vbs_text}';"
                f"$s.WorkingDirectory='{app_text}';"
                f"$s.WindowStyle=7;"
                f"$s.Save()"
            )
            try:
                completed = _sp.run(
                    ["powershell.exe", "-NoProfile",
                     "-ExecutionPolicy", "Bypass", "-Command", ps],
                    capture_output=True, timeout=10, text=True,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.strip() or "PowerShell failed")
                LOGGER.info("autostart installed: %s → %s", lnk, vbs)
            except Exception:
                LOGGER.exception("failed to install autostart")
        # Label refreshes automatically next time the native menu is built.

    def reset_geometry(self) -> None:
        default_scale = float(self.config["default_scale"])
        # Reset inside the work area of the monitor nearest the pet's current
        # rectangle.  This remains valid with negative coordinates, uneven
        # monitor layouts, taskbars, and disconnected displays.
        try:
            current_x = self.root.winfo_x()
            current_y = self.root.winfo_y()
            current_width = max(1, self.root.winfo_width())
            current_height = max(1, self.root.winfo_height())
            work = _get_monitor_work_area_for_rect(
                current_x, current_y, current_width, current_height
            )
            if work:
                right, bottom = work[2], work[3]
            else:
                virt = _get_virtual_screen_bounds()
                if virt:
                    right, bottom = virt[2], virt[3]
                else:
                    right = self.root.winfo_screenwidth()
                    bottom = self.root.winfo_screenheight()
            bw = max(1, round(self.base_width * default_scale))
            bh = max(1, round(self.base_height * default_scale))
            dx = right - bw - self._default_offset_x
            dy = bottom - bh - self._default_offset_y
        except Exception:
            dx, dy = 155, 448
        self.settings.update({"x": dx, "y": dy, "scale": default_scale})
        self.set_scale(default_scale, save=False)
        self._save_settings()

    def _start_drag(self, event: tk.Event) -> None:
        self.drag_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )
        # Switch to drag animation while the user moves the window
        drag_gif = self.config["actions"].get("drag")
        if drag_gif and self.current_action != drag_gif:
            self._drag_previous_action = self.current_action
            self._drag_previous_loops = self.loops_remaining
            self.play(drag_gif, loops=None, status_text=self.current_status_text)

    def _drag(self, event: tk.Event) -> None:
        if not self.drag_origin:
            return
        sx, sy, wx, wy = self.drag_origin
        x = wx + event.x_root - sx
        y = wy + event.y_root - sy
        self.root.geometry(f"+{x}+{y}")
        self._draw_indicator()

    def _end_drag(self, _event: tk.Event) -> None:
        self.drag_origin = None
        self.settings["x"] = self.root.winfo_x()
        self.settings["y"] = self.root.winfo_y()
        self._apply_geometry()
        # Restore the animation that was playing before the drag started
        prev = getattr(self, "_drag_previous_action", None)
        if prev and self.current_action == self.config["actions"].get("drag"):
            self.play(prev, loops=getattr(self, "_drag_previous_loops", None),
                      status_text=self.current_status_text)

    def _wheel(self, event: tk.Event) -> None:
        step = float(self.config["rendering"]["scale_step"])
        direction = 1 if event.delta > 0 else -1
        new_scale = max(self._scale_min,
                        min(self._scale_max,
                            round(self.scale + direction * step, 4)))
        self.set_scale(new_scale, defer_render=True)

    def set_scale(
        self,
        new_scale: float,
        *,
        save: bool = True,
        defer_render: bool = False,
    ) -> None:
        new_scale = round(
            max(self._scale_min, min(self._scale_max, float(new_scale))), 4
        )
        if new_scale == self.scale and save:
            self.scale_var.set(new_scale)
            return
        self.scale = new_scale
        self.scale_var.set(new_scale)
        self.settings["scale"] = new_scale
        self._apply_geometry(save=save and not defer_render)

        if self._scale_settle_job is not None:
            try:
                self.root.after_cancel(self._scale_settle_job)
            except Exception:
                pass
            self._scale_settle_job = None
        if defer_render:
            self._scale_settle_job = self.root.after(
                120, lambda: self._finish_scale_change(save=save)
            )
            return
        self._finish_scale_change(save=save)

    def _finish_scale_change(self, *, save: bool) -> None:
        self._scale_settle_job = None
        self._cache_clear()  # frames are scale-dependent; invalidate cache
        if self.current_action:
            action = self.current_action
            loops = self.loops_remaining
            self.play(action, loops=loops, status_text=self.current_status_text)
        if save:
            self._save_settings()

    def _save_settings(self) -> None:
        serialized = (
            json.dumps(self.settings, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        if serialized == self._last_saved_settings:
            return
        try:
            SETTINGS_PATH.write_text(serialized, encoding="utf-8")
            self._last_saved_settings = serialized
        except OSError:
            LOGGER.exception("could not save settings")
