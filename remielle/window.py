from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk

from .config import (_DEFAULT_COORDINATES, SETTINGS_PATH, CONFIG_PATH,
                     TRANSPARENT_COLOR, _TRANSPARENT_RGB, LOGGER,
                     expand_path, load_json)
from .win32_helpers import (_win32_set_clickthrough, _win32_remove_window_border,
                            _win32_clip_window_region, _get_virtual_screen_bounds,
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


class RemielleWindow:
    def __init__(self, config: dict, on_exit: Callable[[], None]) -> None:
        self.config = config
        self.on_exit = on_exit
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
        self.settings = {
            "x": 155,
            "y": 448,
            "scale": default_scale,
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
        self.visible = False
        self._on_end: Callable[[], None] | None = None
        self.drag_origin: tuple[int, int, int, int] | None = None
        self._drag_previous_action: str | None = None
        self._drag_previous_loops: int | None = None
        # ── GIF frame cache ──
        # Key: gif_name, Value: (frames, delays)
        self._frame_cache: dict[str, tuple[list[ImageTk.PhotoImage], list[int]]] = {}
        self._cache_order: list[str] = []               # LRU: front=recent, back=oldest
        self._cache_max: int = int(config["frame_cache_max"])

        self._last_saved_settings: str | None = None  # dedup writes

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("蕾米 AI 助手")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(config["topmost"]))
        if sys.platform == "win32":
            self.root.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.root.configure(bg=TRANSPARENT_COLOR)
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
        self.current_status_text = "等待 AI 任务"
        # Snapshot of bridge state pushed from app._poll() for the menu header
        self._status_info: dict[str, str] = {}

        self._compute_layout()
        self._apply_geometry()

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
        """Return (x, y) clamped to the **virtual** desktop (all monitors).

        On Windows the clamp uses ``GetSystemMetrics`` to obtain the
        bounding rectangle of the entire virtual screen, so the window can
        be placed on any monitor.  On other platforms it falls back to
        ``winfo_screenwidth/height`` (primary monitor only).
        """
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

        # Clamp to the virtual desktop — keep at least *m* px visible so
        # the user can always grab and drag the window back.
        x = max(v_left - width + m, min(x, v_right - m))
        y = max(v_top - height + m, min(y, v_bottom - m))
        return x, y

    def _apply_geometry(self) -> None:
        width = max(1, round(self.base_width * self.scale))
        height = max(1, round(self.base_height * self.scale))
        x, y = self._screen_visible_geom(width, height)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.settings["x"] = x
        self.settings["y"] = y
        self.canvas.configure(width=width, height=height)
        if self._hwnd:
            _win32_clip_window_region(self._hwnd, width, height)
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
        self._status_info = dict(info)

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
            self.root.after(50, lambda: self._render_frame(token))
            return
        offset = self.coordinates.get(self.current_action or "", {})
        x = round((int(offset.get("x", 0)) - self.base_min_x) * self.scale)
        y = round((int(offset.get("y", 0)) - self.base_min_y) * self.scale)
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
        self.root.after(delay, lambda: self._render_frame(token))

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

    def hide(self) -> None:
        self.set_clickthrough(False)
        self.animation_token += 1
        self.root.withdraw()
        self.visible = False
        self.set_status_text("等待 AI 任务")
        LOGGER.info("window hidden")

    # ── Menu demo & self-test ────────────────────────────────────

    def _menu_demo_all(self) -> None:
        """Demonstrate all 7 states in sequence, ending back at idle."""
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
        ]

        def _play_step(idx: int) -> None:
            if idx >= len(sequence):
                # All done — return to idle
                self.play(idle_gif, loops=None, status_text="等待 AI 任务")
                return
            gif, text, duration = sequence[idx]
            self.play(gif, loops=None, status_text=text)
            self.root.after(duration, lambda: _play_step(idx + 1))

        _play_step(0)

    def _menu_run_selftest(self) -> None:
        """Called from right-click menu: run self-test and show results."""
        import tkinter.messagebox as mb
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

    def show_menu_at(self, x: int, y: int) -> None:
        """Post the right-click context menu at screen position (x, y).

        Uses Windows native ``TrackPopupMenu`` instead of ``tk_popup``.
        Native menus are rendered by the OS outside Tk's window system,
        so the overrideredirect / transparent-color window can never
        cause z-order flicker.
        """
        if sys.platform != "win32":
            return  # native menus are Windows-only; silently skip on macOS/Linux
        was_topmost = self.root.attributes("-topmost")
        if was_topmost:
            self.root.attributes("-topmost", False)
        self._menu_active = True
        try:
            self._native_show_menu(x, y)
        finally:
            self._menu_active = False
            if was_topmost:
                self.root.attributes("-topmost", True)

    def _native_show_menu(self, x: int, y: int) -> None:
        """Build a native Win32 popup menu and post it at (x, y).

        The menu is built fresh each time so it always reflects the
        current status, scale, toggle state, and autostart label.
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
        _native_add_sep(menu)

        # ── Size submenu ─────────────────────────────────────
        size_menu = _native_create_menu()
        submenus.append(size_menu)
        current_pct = round(self.scale * 100)
        for pct in (50, 75, 100, 125, 150):
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
        add_check("完成后自动隐藏", self._toggle_autohide,
                  checked=self.autohide_var.get())
        _native_add_sep(menu)

        # ── Autostart ─────────────────────────────────────────
        add_item(self._autostart_label, self._toggle_autostart)
        _native_add_sep(menu)

        # ── Exit ─────────────────────────────────────────────
        add_item("退出状态桥", self.on_exit)

        # ── Show & dispatch ──────────────────────────────────
        callback: Callable[[], None] | None = None
        try:
            cmd = _native_track(menu, self._hwnd, x, y)
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

    # ── Behaviour toggles ────────────────────────────────────────

    def _toggle_persistent(self) -> None:
        """Toggle the '常驻显示' checkmark and persist to config."""
        new_val = not self.persistent_var.get()
        self.persistent_var.set(new_val)
        self.config.setdefault("display", {})["persistent"] = new_val
        self._save_config()

    def _toggle_autohide(self) -> None:
        """Toggle the '完成后自动隐藏' checkmark and persist to config."""
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
            ps = (
                f"$w=New-Object -ComObject WScript.Shell;"
                f"$s=$w.CreateShortcut('{lnk}');"
                f"$s.TargetPath='{vbs}';"
                f"$s.WorkingDirectory='{APP_DIR}';"
                f"$s.WindowStyle=7;"
                f"$s.Save()"
            )
            try:
                _sp.run(
                    ["powershell.exe", "-NoProfile",
                     "-ExecutionPolicy", "Bypass", "-Command", ps],
                    capture_output=True, timeout=10,
                )
                LOGGER.info("autostart installed: %s → %s", lnk, vbs)
            except Exception:
                LOGGER.exception("failed to install autostart")
        # Label refreshes automatically next time the native menu is built.

    def reset_geometry(self) -> None:
        default_scale = float(self.config["default_scale"])
        # Compute a sensible default position on the virtual desktop
        try:
            virt = _get_virtual_screen_bounds()
            if virt:
                v_right, v_bottom = virt[2], virt[3]
            else:
                v_right = self.root.winfo_screenwidth()
                v_bottom = self.root.winfo_screenheight()
            bw = max(1, round(self.base_width * default_scale))
            bh = max(1, round(self.base_height * default_scale))
            dx = v_right - bw - self._default_offset_x
            dy = v_bottom - bh - self._default_offset_y
        except Exception:
            dx, dy = 155, 448
        self.settings = {"x": dx, "y": dy, "scale": default_scale}
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
                            round(self.scale + direction * step, 1)))
        self.set_scale(new_scale)

    def set_scale(self, new_scale: float, *, save: bool = True) -> None:
        new_scale = max(self._scale_min, min(self._scale_max, float(new_scale)))
        if new_scale == self.scale and save:
            self.scale_var.set(new_scale)
            return
        self.scale = new_scale
        self.scale_var.set(new_scale)
        self.settings["scale"] = new_scale
        self._compute_layout()
        self._apply_geometry()
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
