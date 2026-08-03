from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

from .config import LOGGER
from .hooks import hooks_installed, install_hooks, uninstall_hooks, user_hooks_path
from .win32_helpers import _win32_round_window_region


def _rounded_rectangle(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    *,
    fill: str,
    outline: str = "",
    width: int = 1,
    tags: str | tuple[str, ...] = (),
) -> None:
    """Draw a crisp rounded rectangle using native Canvas primitives."""
    radius = max(1, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    diameter = radius * 2
    options = {"fill": fill, "outline": "", "width": 0, "tags": tags}
    canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, **options)
    canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, **options)
    canvas.create_arc(x1, y1, x1 + diameter, y1 + diameter, start=90, extent=90, style="pieslice", **options)
    canvas.create_arc(x2 - diameter, y1, x2, y1 + diameter, start=0, extent=90, style="pieslice", **options)
    canvas.create_arc(x2 - diameter, y2 - diameter, x2, y2, start=270, extent=90, style="pieslice", **options)
    canvas.create_arc(x1, y2 - diameter, x1 + diameter, y2, start=180, extent=90, style="pieslice", **options)
    if outline:
        line_options = {"fill": outline, "width": width, "tags": tags}
        canvas.create_line(x1 + radius, y1, x2 - radius, y1, **line_options)
        canvas.create_line(x2, y1 + radius, x2, y2 - radius, **line_options)
        canvas.create_line(x2 - radius, y2, x1 + radius, y2, **line_options)
        canvas.create_line(x1, y2 - radius, x1, y1 + radius, **line_options)
        arc_options = {
            "style": "arc", "outline": outline, "width": width, "tags": tags,
        }
        canvas.create_arc(x1, y1, x1 + diameter, y1 + diameter, start=90, extent=90, **arc_options)
        canvas.create_arc(x2 - diameter, y1, x2, y1 + diameter, start=0, extent=90, **arc_options)
        canvas.create_arc(x2 - diameter, y2 - diameter, x2, y2, start=270, extent=90, **arc_options)
        canvas.create_arc(x1, y2 - diameter, x1 + diameter, y2, start=180, extent=90, **arc_options)


class RoundedButton(tk.Canvas):
    """Small Canvas button that stays lighter than a themed widget set."""

    def __init__(
        self,
        parent,
        text: str,
        command,
        *,
        background: str,
        hover: str,
        foreground: str,
        outline: str = "",
        height: int = 30,
        width: int = 58,
        radius: int = 10,
        font=("Microsoft YaHei UI", 9),
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent.cget("bg"),
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=True,
        )
        self._text = text
        self._command = command
        self._background = background
        self._hover = hover
        self._foreground = foreground
        self._outline = outline
        self._radius = radius
        self._font = font
        self._inside = False
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonRelease-1>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<Return>", self._invoke)

    def _redraw(self, _event=None) -> None:
        self.delete("all")
        w = max(4, self.winfo_width())
        h = max(4, self.winfo_height())
        fill = self._hover if self._inside else self._background
        _rounded_rectangle(
            self, 1, 1, w - 1, h - 1, self._radius,
            fill=fill, outline=self._outline,
        )
        self.create_text(
            w / 2,
            h / 2,
            text=self._text,
            fill=self._foreground,
            font=self._font,
        )

    def _enter(self, _event=None) -> None:
        self._inside = True
        self._redraw()

    def _leave(self, _event=None) -> None:
        self._inside = False
        self._redraw()

    def _invoke(self, event=None) -> None:
        if event is None or getattr(event, "keysym", "") or self._inside:
            self._command()

    def set_style(
        self,
        *,
        text: str | None = None,
        background: str | None = None,
        hover: str | None = None,
        foreground: str | None = None,
        outline: str | None = None,
    ) -> None:
        if text is not None:
            self._text = text
        if background is not None:
            self._background = background
        if hover is not None:
            self._hover = hover
        if foreground is not None:
            self._foreground = foreground
        if outline is not None:
            self._outline = outline
        self._redraw()


class ControlPanel:
    """Airy, rounded popup menu for the desktop pet."""

    TRANSPARENT = "#010203"

    def __init__(self, window) -> None:
        self.window = window
        self.root = window.root
        palette = window.config["ui"]
        self.bg = palette["background"]
        self.surface = palette["surface"]
        self.hover = palette["surface_hover"]
        self.accent = palette["accent"]
        self.text = palette["text"]
        self.muted = palette["muted"]
        self.border = palette.get("border", "#eadde1")
        self.accent_soft = palette.get("accent_soft", "#f6e3e9")
        self.danger = palette.get("danger", "#b85c6d")
        self._drag: tuple[int, int, int, int] | None = None
        self._status = "等待状态同步"

        self.top = tk.Toplevel(self.root)
        self.top.withdraw()
        self.top.title("蕾米 AI 助手")
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        # Keep the popup opaque and clip it with a native rounded region.
        # Transparent-colour Toplevels disable ClearType on Windows and make
        # small Chinese text visibly softer than text in ordinary apps.
        self.top.configure(bg=self.bg)
        self.top.bind("<Escape>", lambda _e: self.hide())

        self.shell = tk.Canvas(
            self.top,
            bg=self.bg,
            bd=0,
            highlightthickness=0,
        )
        self.shell.pack(fill="both", expand=True)
        self.body = tk.Frame(self.shell, bg=self.bg, padx=6, pady=5)
        self._body_window = self.shell.create_window(
            8, 8, anchor="nw", window=self.body,
        )
        self.shell.bind("<Configure>", self._layout_shell)

        self._build_header()

        # Keep the first level intentionally tiny. Less common actions live on
        # a second page in the same popup, so no extra window or module is
        # needed and the menu stays cheap to create.
        self.main_page = tk.Frame(self.body, bg=self.bg)
        self.main_page.pack(fill="both", expand=True)
        self.more_page = tk.Frame(self.body, bg=self.bg)
        self.size_page = tk.Frame(self.body, bg=self.bg)
        self.indicator_page = tk.Frame(self.body, bg=self.bg)

        self.ack_btn = self._button(
            self.main_page,
            "✓  已查看任务结果",
            self.window.acknowledge_results,
            accent=True,
            height=32,
        )
        self._build_main_page()
        self._build_more_page()
        self._build_size_page()
        self._build_indicator_page()

    def _layout_shell(self, event) -> None:
        self.shell.delete("panel-shape")
        _rounded_rectangle(
            self.shell,
            1,
            1,
            max(2, event.width - 1),
            max(2, event.height - 1),
            24,
            fill=self.bg,
            outline=self.border,
            width=1,
            tags="panel-shape",
        )
        self.shell.tag_lower("panel-shape")
        self.shell.coords(self._body_window, 8, 7)
        self.shell.itemconfigure(
            self._body_window,
            width=max(1, event.width - 16),
            height=max(1, event.height - 14),
        )

    def _build_header(self) -> None:
        header = tk.Frame(self.body, bg=self.bg, cursor="fleur")
        header.pack(fill="x", pady=(0, 6))
        self._bind_drag(header)

        self.avatar = self._load_avatar()
        avatar_canvas = tk.Canvas(
            header, width=30, height=30, bg=self.bg,
            bd=0, highlightthickness=0,
        )
        avatar_canvas.pack(side="left", padx=(0, 9))
        avatar_canvas.create_oval(1, 1, 29, 29, fill=self.accent_soft, outline="")
        if self.avatar:
            avatar_canvas.create_image(15, 15, image=self.avatar)
        self._bind_drag(avatar_canvas)

        title_box = tk.Frame(header, bg=self.bg)
        title_box.pack(side="left", fill="x", expand=True)
        self._bind_drag(title_box)
        title = tk.Label(
            title_box,
            text="蕾米",
            bg=self.bg,
            fg=self.text,
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
        )
        title.pack(fill="x")
        self.header_status = tk.Label(
            title_box,
            text="等待状态同步",
            bg=self.bg,
            fg=self.muted,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.header_status.pack(fill="x")
        self._bind_drag(title)
        self._bind_drag(self.header_status)

        self._button(
            header,
            "×",
            self.hide,
            background=self.bg,
            hover=self.accent_soft,
            foreground=self.muted,
            width=28,
            height=28,
            radius=14,
            font=("Microsoft YaHei UI", 11),
        ).pack(side="right")

    def _build_main_page(self) -> None:
        self.persistent_btn = self._menu_item(
            self.main_page, "", self._toggle_persistent,
        )
        self.autohide_btn = self._menu_item(
            self.main_page, "", self._toggle_autohide,
        )
        self.size_btn = self._menu_item(
            self.main_page, "桌宠大小  ›", self._show_sizes,
        )
        self.more_btn = self._menu_item(
            self.main_page, "更多设置  ›", self._show_more,
        )
        self._menu_item(
            self.main_page, "隐藏桌宠", self._hide_pet,
            foreground=self.muted,
        )
        self._menu_item(
            self.main_page, "退出", self.window.on_exit,
            foreground=self.danger,
        )

    def _menu_item(
        self,
        parent,
        text: str,
        command,
        *,
        foreground: str | None = None,
    ) -> RoundedButton:
        button = self._button(
            parent,
            text,
            command,
            background=self.bg,
            hover=self.surface,
            foreground=foreground or self.text,
            height=27,
            radius=13,
        )
        button.pack(fill="x", pady=(0, 2))
        return button

    def _build_more_page(self) -> None:
        heading = tk.Frame(self.more_page, bg=self.bg)
        heading.pack(fill="x", pady=(0, 9))
        self._button(
            heading,
            "‹  返回",
            self._show_main,
            background=self.bg,
            hover=self.accent_soft,
            foreground=self.accent,
            width=62,
            height=28,
        ).pack(side="left")
        tk.Label(
            heading,
            text="更多设置",
            bg=self.bg,
            fg=self.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(6, 0))

        self.indicator_settings_btn = self._menu_item(
            self.more_page, "状态与 Token  ›", self._show_indicator,
        )

        tools = (
            ("演示全部动作", self.window._menu_demo_all),
            ("运行资源自检", self.window._menu_run_selftest),
            ("重置桌宠位置", self.window.reset_geometry),
        )
        for label, command in tools:
            self._menu_item(self.more_page, label, command)
        self.hook_btn = self._menu_item(
            self.more_page, "Codex Hooks", self._toggle_hooks,
        )
        self.autostart_btn = self._menu_item(
            self.more_page, self.window._autostart_label, self._toggle_autostart,
        )

    def _build_size_page(self) -> None:
        heading = tk.Frame(self.size_page, bg=self.bg)
        heading.pack(fill="x", pady=(0, 9))
        self._button(
            heading,
            "‹  返回",
            self._show_main,
            background=self.bg,
            hover=self.accent_soft,
            foreground=self.accent,
            width=62,
            height=28,
        ).pack(side="left")
        tk.Label(
            heading,
            text="桌宠大小",
            bg=self.bg,
            fg=self.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(6, 0))

        self.size_current_label = tk.Label(
            self.size_page,
            text="",
            bg=self.bg,
            fg=self.muted,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.size_current_label.pack(fill="x", padx=4, pady=(0, 7))

        self.size_buttons: dict[int, RoundedButton] = {}
        for pct in (50, 75, 100, 150, 200):
            button = self._menu_item(
                self.size_page,
                f"{pct}%",
                lambda value=pct: self._set_scale(value),
            )
            self.size_buttons[pct] = button

    def _build_indicator_page(self) -> None:
        heading = tk.Frame(self.indicator_page, bg=self.bg)
        heading.pack(fill="x", pady=(0, 9))
        self._button(
            heading,
            "‹  返回",
            self._show_more,
            background=self.bg,
            hover=self.accent_soft,
            foreground=self.accent,
            width=62,
            height=28,
        ).pack(side="left")
        tk.Label(
            heading,
            text="辅助显示",
            bg=self.bg,
            fg=self.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(6, 0))

        self.indicator_toggle_btn = self._menu_item(
            self.indicator_page, "", self._toggle_indicator,
        )
        self.indicator_position_btn = self._menu_item(
            self.indicator_page, "", self._cycle_indicator_position,
        )

    def _show_main(self) -> None:
        self.more_page.pack_forget()
        self.size_page.pack_forget()
        self.indicator_page.pack_forget()
        self.main_page.pack(fill="both", expand=True)
        self._schedule_fit()

    def _show_more(self) -> None:
        self.main_page.pack_forget()
        self.size_page.pack_forget()
        self.indicator_page.pack_forget()
        self.more_page.pack(fill="both", expand=True)
        self._schedule_fit()

    def _show_sizes(self) -> None:
        self.main_page.pack_forget()
        self.more_page.pack_forget()
        self.indicator_page.pack_forget()
        self.size_page.pack(fill="both", expand=True)
        self._schedule_fit()

    def _show_indicator(self) -> None:
        self.main_page.pack_forget()
        self.more_page.pack_forget()
        self.size_page.pack_forget()
        self.indicator_page.pack(fill="both", expand=True)
        self._schedule_fit()

    def _schedule_fit(self) -> None:
        if self.top.state() != "withdrawn":
            self.top.after_idle(self._fit_current_page)

    def _fit_current_page(self) -> None:
        self.body.update_idletasks()
        width = max(196, self.body.winfo_reqwidth() + 16)
        height = self.body.winfo_reqheight() + 14
        x = self.top.winfo_x()
        y = self.top.winfo_y()
        screen_w = self.top.winfo_screenwidth()
        screen_h = self.top.winfo_screenheight()
        x = min(max(8, x), max(8, screen_w - width - 8))
        y = min(max(8, y), max(8, screen_h - height - 48))
        self.top.geometry(f"{width}x{height}+{x}+{y}")
        self.top.update_idletasks()
        if sys.platform == "win32":
            _win32_round_window_region(self.top.winfo_id(), width, height, 24)

    def _bind_drag(self, widget) -> None:
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._drag_window)

    def _load_avatar(self):
        try:
            path = self.window.asset_dir / self.window.config["actions"]["idle"]
            with Image.open(path) as image:
                image.seek(0)
                frame = image.convert("RGBA")
                frame.thumbnail((30, 30), Image.Resampling.LANCZOS)
                return ImageTk.PhotoImage(frame, master=self.top)
        except Exception:
            return None

    def _button(
        self,
        parent,
        text: str,
        command,
        *,
        compact: bool = False,
        accent: bool = False,
        background: str | None = None,
        hover: str | None = None,
        foreground: str | None = None,
        outline: str = "",
        height: int | None = None,
        width: int = 58,
        radius: int = 10,
        font=None,
    ) -> RoundedButton:
        bg = self.accent if accent else (background or self.surface)
        fg = self.bg if accent else (foreground or self.text)
        hover_bg = "#963b58" if accent else (hover or self.hover)
        return RoundedButton(
            parent,
            text,
            command,
            background=bg,
            hover=hover_bg,
            foreground=fg,
            outline=outline,
            height=height or (27 if compact else 30),
            width=width,
            radius=radius,
            font=font or ("Microsoft YaHei UI", 9, "bold" if accent else "normal"),
        )

    def _start_drag(self, event) -> None:
        self._drag = (event.x_root, event.y_root, self.top.winfo_x(), self.top.winfo_y())

    def _drag_window(self, event) -> None:
        if not self._drag:
            return
        sx, sy, x, y = self._drag
        self.top.geometry(f"+{x + event.x_root - sx}+{y + event.y_root - sy}")

    def _set_scale(self, pct: int) -> None:
        self.window.set_scale(pct / 100)
        self.refresh()

    def _toggle_persistent(self) -> None:
        self.window._toggle_persistent()
        self.refresh()

    def _toggle_autohide(self) -> None:
        self.window._toggle_autohide()
        self.refresh()

    def _toggle_autostart(self) -> None:
        self.window._toggle_autostart()
        self.refresh()

    def _toggle_hooks(self) -> None:
        try:
            if hooks_installed():
                uninstall_hooks()
                messagebox.showinfo(
                    "Codex Hooks",
                    "蕾米 Hooks 已卸载，重启 Codex 后生效。",
                    parent=self.top,
                )
            else:
                install_hooks()
                messagebox.showinfo(
                    "Codex Hooks 已安装",
                    f"配置位置：\n{user_hooks_path()}\n\n"
                    "请重启 Codex，并在 /hooks 中信任两个蕾米命令。",
                    parent=self.top,
                )
        except Exception as error:
            LOGGER.exception("could not toggle Codex hooks")
            messagebox.showerror("Codex Hooks", str(error), parent=self.top)
        self.refresh()

    def _toggle_indicator(self) -> None:
        self.window.toggle_indicator()
        self.refresh()

    def _cycle_indicator_position(self) -> None:
        self.window.cycle_indicator_position()
        self.refresh()

    def _hide_pet(self) -> None:
        self.hide()
        self.window.hide_to_tray()

    def refresh(self) -> None:
        info = self.window._status_info
        self._status = info.get("mode") or self.window.current_status_text
        hook_enabled = hooks_installed()
        self.header_status.configure(text=f"●  {self._status}")

        persistent = bool(self.window.persistent_var.get())
        autohide = bool(self.window.autohide_var.get())
        self._set_toggle_style(
            self.persistent_btn,
            persistent,
            "常驻",
        )
        self._set_toggle_style(
            self.autohide_btn,
            autohide,
            "查看后隐藏",
        )
        self.hook_btn.set_style(
            text="Hooks ✓" if hook_enabled else "Hooks",
            foreground=self.accent if hook_enabled else self.text,
            background=self.accent_soft if hook_enabled else self.bg,
            outline=self.accent_soft if hook_enabled else self.border,
        )
        self.autostart_btn.set_style(text=self.window._autostart_label)
        self.size_btn.set_style(text=f"桌宠大小  {round(self.window.scale * 100)}%  ›")

        indicator_enabled = bool(self.window.indicator_enabled_var.get())
        self._set_toggle_style(
            self.indicator_toggle_btn, indicator_enabled, "显示状态与 Token"
        )
        position_labels = {
            "right": "右侧 · 纵向",
            "bottom": "下方 · 横向",
            "left": "左侧 · 纵向",
            "top": "上方 · 横向",
        }
        self.indicator_position_btn.set_style(
            text=f"位置  {position_labels[self.window.indicator_position]}",
            foreground=self.text,
            background=self.surface,
        )

        current_pct = round(self.window.scale * 100)
        self.size_current_label.configure(
            text=f"当前 {current_pct}%  ·  滚轮每次 5%"
        )
        for pct, button in self.size_buttons.items():
            selected = pct == current_pct
            button.set_style(
                foreground=self.accent if selected else self.muted,
                background=self.accent_soft if selected else self.surface,
            )

        if info.get("reviews"):
            if not self.ack_btn.winfo_manager():
                self.ack_btn.pack(fill="x", pady=(0, 3), before=self.persistent_btn)
        elif self.ack_btn.winfo_manager():
            self.ack_btn.pack_forget()

    def _set_toggle_style(
        self,
        button: RoundedButton,
        enabled: bool,
        label: str,
    ) -> None:
        button.set_style(
            text=(f"✓  {label}" if enabled else label),
            foreground=self.accent if enabled else self.muted,
            background=self.accent_soft if enabled else self.surface,
        )

    def show_at(self, x: int, y: int) -> None:
        self._show_main()
        self.refresh()
        self.body.update_idletasks()
        width = max(196, self.body.winfo_reqwidth() + 16)
        height = self.body.winfo_reqheight() + 14
        screen_w = self.top.winfo_screenwidth()
        screen_h = self.top.winfo_screenheight()
        x = min(max(8, x - width + 18), max(8, screen_w - width - 8))
        y = min(max(8, y - 18), max(8, screen_h - height - 48))
        self.top.geometry(f"{width}x{height}+{x}+{y}")
        self.top.deiconify()
        self.top.update_idletasks()
        if sys.platform == "win32":
            _win32_round_window_region(
                self.top.winfo_id(), width, height, 24
            )
        self.top.lift()
        self.top.focus_force()

    def hide(self) -> None:
        self.top.withdraw()

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass
