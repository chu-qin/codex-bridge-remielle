"""
控制面板（弹窗菜单）模块。

负责桌宠右键弹出的圆角菜单：显示状态、开关常驻/自动隐藏、
调整桌宠大小、管理 Codex Hooks、切换状态条位置、退出等。

菜单是一棵**悬停级联**树：根 Toplevel 显示一级条目，带子菜单的条目
在鼠标悬停时于其右侧（空间不足时翻到左侧）飞出独立的子面板，无需点击。
所有颜色/字体等外观参数均来自 config["ui"]（见 config.py），
可直接修改 config.json 里的 "ui" 段来换肤，无需改代码。
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

from .config import LOGGER
from .hooks import hooks_installed, install_hooks, uninstall_hooks, user_hooks_path
from .win32_helpers import (_win32_round_window_region,
                            _get_virtual_screen_bounds,
                            _get_monitor_work_area_for_rect,
                            _clamp_window_to_bounds)

# 级联菜单的时序与几何常量。开/关双计时避免悬停时的 Enter/Leave 闪烁：
# 从父按钮滑进子菜单时，父按钮的 Leave 先排一个延迟关闭，子菜单的 Enter
# 会在计时到期前取消它。
OPEN_DELAY_MS = 150      # 悬停多久后打开子菜单
CLOSE_DELAY_MS = 300     # 离开菜单树多久后关闭
MENU_MIN_WIDTH = 196     # 面板最小宽度（原硬编码 196）
MENU_RADIUS = 24         # 圆角半径（原硬编码 24）
MENU_GAP = 2             # 子菜单与父按钮之间的间隙


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
    """Small Canvas button that stays lighter than a themed widget set.
    自绘圆角按钮：不用 ttk 主题，直接画圆角矩形，颜色可随时 set_style 换。
    """

    def __init__(
        self,
        parent,
        text: str,
        command,
        *,
        background: str,   # 普通态背景色
        hover: str,        # 悬停态背景色
        foreground: str,   # 文字颜色
        outline: str = "", # 描边颜色（空=无描边）
        height: int = 30,  # 按钮高度
        width: int = 58,   # 按钮宽度
        radius: int = 10,  # 圆角半径
        font=("Microsoft YaHei UI", 9),  # 字体
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


class MenuFlyout:
    """A hover-opened cascade submenu: a withdrawn Toplevel panel.

    Holds the widget tree plus the open/close timer state.  Timing and
    positioning logic lives on ``ControlPanel`` so the flyout stays a plain
    data holder (testable with fakes).
    """

    __slots__ = ("top", "shell", "body", "body_window",
                 "parent_btn", "mapped", "open_job", "close_job")

    def __init__(self, top, shell, body, body_window) -> None:
        self.top = top
        self.shell = shell
        self.body = body
        self.body_window = body_window
        self.parent_btn = None          # RoundedButton that opens this flyout
        self.mapped = False             # flyout is currently visible
        self.open_job = None            # pending ``after`` id for open
        self.close_job = None           # pending ``after`` id for close


class ControlPanel:
    """Airy, rounded popup menu for the desktop pet.

    主面板：无边框圆角 Toplevel。一级菜单固定展示；带子菜单的条目在悬停时
    于其右侧飞出独立子面板（空间不足时翻到左侧），不再需要翻页点击。
    """

    def __init__(self, window) -> None:
        self.window = window
        self.root = window.root
        # 配色从 config["ui"] 读取：改 config.json 的 ui 段即可换肤
        palette = window.config["ui"]
        self.bg = palette["background"]        # 面板底色
        self.surface = palette["surface"]      # 普通按钮底色
        self.hover = palette["surface_hover"]  # 按钮悬停色
        self.accent = palette["accent"]        # 强调色（主按钮/选中态）
        self.text = palette["text"]            # 主文字色
        self.muted = palette["muted"]          # 次要文字色
        self.border = palette.get("border", "#eadde1")            # 描边
        self.accent_soft = palette.get("accent_soft", "#f6e3e9")  # 浅强调底
        self.danger = palette.get("danger", "#b85c6d")            # 危险/退出
        self._drag: tuple[int, int, int, int] | None = None
        self._status = "等待状态同步"
        self._open_flyouts: list[MenuFlyout] = []

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

        # 一级菜单固定展示；子菜单是独立的悬停飞出面板。
        self.main_page = tk.Frame(self.body, bg=self.bg)
        self.main_page.pack(fill="both", expand=True)

        self.ack_btn = self._button(
            self.main_page,
            "✓  已查看任务结果",
            lambda: (self.window.acknowledge_results(), self.hide()),
            accent=True,
            height=32,
        )

        self._flyouts: dict[str, MenuFlyout] = {}
        self._build_flyouts()
        self._build_root_menu()

    def _layout_shell(self, event) -> None:
        self._layout_round_shell(self.shell, event, self._body_window, "panel-shape")

    def _layout_round_shell(self, canvas, event, body_window, tag) -> None:
        """Draw the rounded background/border on ``canvas`` and keep the
        content ``body_window`` inset.  Shared by the root and the flyouts."""
        canvas.delete(tag)
        _rounded_rectangle(
            canvas,
            1,
            1,
            max(2, event.width - 1),
            max(2, event.height - 1),
            MENU_RADIUS,
            fill=self.bg,
            outline=self.border,
            width=1,
            tags=tag,
        )
        canvas.tag_lower(tag)
        canvas.coords(body_window, 8, 7)
        canvas.itemconfigure(
            body_window,
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
            text="蕾米埃尔 AI 助手",
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

    # ── Root menu (level 1) ──────────────────────────────────────────

    def _build_root_menu(self) -> None:
        # 一级：常驻 / 查看后隐藏 / 桌宠大小 › / 状态与 Token › / 更多设置 › / 隐藏 / 退出
        self.persistent_btn = self._menu_item(
            self.main_page, "", self._toggle_persistent,
        )
        self.autohide_btn = self._menu_item(
            self.main_page, "", self._toggle_autohide,
        )
        self.size_btn = self._cascade_item(
            self.main_page, "size", "桌宠大小  ›",
        )
        self.status_btn = self._cascade_item(
            self.main_page, "status", "状态与 Token  ›",
        )
        self.more_btn = self._cascade_item(
            self.main_page, "more", "更多设置  ›",
        )
        self._action_item(
            self.main_page, "隐藏", self._hide_pet, foreground=self.muted,
        )
        self._action_item(
            self.main_page, "退出", self.window.on_exit, foreground=self.danger,
        )

    def _cascade_item(self, parent, key: str, text: str) -> RoundedButton:
        """Build a root item that opens a hover flyout.

        ``add="+"`` keeps ``RoundedButton``'s own hover-highlight bindings
        while adding the cascade open/close handlers.
        """
        flyout = self._flyouts[key]
        button = self._menu_item(
            parent, text, lambda: self._open_flyout(flyout),
        )
        button.bind(
            "<Enter>",
            lambda _e, f=flyout: self._on_cascade_enter(f),
            add="+",
        )
        button.bind(
            "<Leave>",
            lambda _e, f=flyout: self._on_cascade_leave(f),
            add="+",
        )
        flyout.parent_btn = button
        return button

    # ── Flyout construction ──────────────────────────────────────────

    def _build_flyouts(self) -> None:
        for key, builder in (
            ("size", self._build_size_flyout),
            ("status", self._build_status_flyout),
            ("more", self._build_more_flyout),
        ):
            flyout = self._make_flyout()
            builder(flyout)
            self._flyouts[key] = flyout

    def _make_flyout(self) -> MenuFlyout:
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=self.bg)
        top.title("蕾米子菜单")
        shell = tk.Canvas(top, bg=self.bg, bd=0, highlightthickness=0)
        shell.pack(fill="both", expand=True)
        body = tk.Frame(shell, bg=self.bg, padx=6, pady=5)
        body_window = shell.create_window(8, 8, anchor="nw", window=body)
        shell.bind(
            "<Configure>",
            lambda event, c=shell, bw=body_window: self._layout_round_shell(
                c, event, bw, "flyout-shape"
            ),
        )
        flyout = MenuFlyout(top, shell, body, body_window)
        # 鼠标从父按钮滑进子菜单时，子菜单的 Enter 取消父级的延迟关闭，
        # 避免菜单闪关；离开子菜单则排一个延迟关闭。
        top.bind("<Enter>", lambda _e: self._on_flyout_enter(flyout))
        top.bind("<Leave>", lambda _e: self._on_flyout_leave(flyout))
        shell.bind("<Enter>", lambda _e: self._on_flyout_enter(flyout), add="+")
        shell.bind("<Leave>", lambda _e: self._on_flyout_leave(flyout), add="+")
        top.bind("<Escape>", lambda _e: self.hide())
        return flyout

    def _build_size_flyout(self, flyout: MenuFlyout) -> None:
        heading = tk.Frame(flyout.body, bg=self.bg)
        heading.pack(fill="x", pady=(0, 9))
        tk.Label(
            heading,
            text="桌宠大小",
            bg=self.bg,
            fg=self.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        self.size_current_label = tk.Label(
            flyout.body,
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
                flyout.body,
                f"{pct}%",
                lambda value=pct: self._set_scale(value),
            )
            self.size_buttons[pct] = button

    def _build_status_flyout(self, flyout: MenuFlyout) -> None:
        heading = tk.Frame(flyout.body, bg=self.bg)
        heading.pack(fill="x", pady=(0, 9))
        tk.Label(
            heading,
            text="状态与 Token",
            bg=self.bg,
            fg=self.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        self.indicator_toggle_btn = self._menu_item(
            flyout.body, "", self._toggle_indicator,
        )

        tk.Label(
            flyout.body,
            text="位置",
            bg=self.bg,
            fg=self.muted,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        ).pack(fill="x", padx=4, pady=(0, 2))

        # 位置单选行直接内嵌，保持层级封顶在 2 层（无需再进三级子菜单）。
        self.indicator_radio_buttons: dict[str, RoundedButton] = {}
        for pos, label in (
            ("right", "右侧 · 纵向"),
            ("bottom", "下方 · 横向"),
            ("left", "左侧 · 纵向"),
            ("top", "上方 · 横向"),
        ):
            button = self._menu_item(
                flyout.body,
                label,
                lambda value=pos: (
                    self.window.set_indicator_position(value),
                    self.refresh(),
                ),
            )
            self.indicator_radio_buttons[pos] = button

    def _build_more_flyout(self, flyout: MenuFlyout) -> None:
        heading = tk.Frame(flyout.body, bg=self.bg)
        heading.pack(fill="x", pady=(0, 9))
        tk.Label(
            heading,
            text="更多设置",
            bg=self.bg,
            fg=self.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        self._action_item(flyout.body, "演示全部动作", self.window._menu_demo_all)
        self._action_item(flyout.body, "运行资源自检", self.window._menu_run_selftest)
        self._action_item(flyout.body, "重置桌宠位置", self.window.reset_geometry)
        self.hook_btn = self._action_item(
            flyout.body, "Codex Hooks", self._toggle_hooks,
        )
        self.autostart_btn = self._action_item(
            flyout.body, self.window._autostart_label, self._toggle_autostart,
        )

    # ── Cascade timing (hover open / grace close) ────────────────────

    def _cancel_job(self, job_id: str | None) -> None:
        if job_id is None:
            return
        try:
            self.top.after_cancel(job_id)
        except tk.TclError:
            pass

    def _cancel_open(self, flyout: MenuFlyout) -> None:
        if flyout.open_job is not None:
            self._cancel_job(flyout.open_job)
            flyout.open_job = None

    def _cancel_close(self, flyout: MenuFlyout) -> None:
        if flyout.close_job is not None:
            self._cancel_job(flyout.close_job)
            flyout.close_job = None

    def _schedule_open(self, flyout: MenuFlyout) -> None:
        self._cancel_close(flyout)
        if flyout.mapped or flyout.open_job is not None:
            return
        flyout.open_job = self.top.after(
            OPEN_DELAY_MS, lambda: self._open_flyout(flyout)
        )

    def _schedule_close(self, flyout: MenuFlyout) -> None:
        self._cancel_open(flyout)
        if flyout.close_job is not None:
            return
        flyout.close_job = self.top.after(
            CLOSE_DELAY_MS, lambda: self._close_flyout(flyout)
        )

    def _on_cascade_enter(self, flyout: MenuFlyout) -> None:
        self._cancel_close(flyout)
        self._close_siblings(flyout)
        self._schedule_open(flyout)

    def _on_cascade_leave(self, flyout: MenuFlyout) -> None:
        self._cancel_open(flyout)
        self._schedule_close(flyout)

    def _on_flyout_enter(self, flyout: MenuFlyout) -> None:
        self._cancel_close(flyout)

    def _on_flyout_leave(self, flyout: MenuFlyout) -> None:
        self._schedule_close(flyout)

    # ── Flyout open/close ────────────────────────────────────────────

    def _open_flyout(self, flyout: MenuFlyout) -> None:
        self._cancel_open(flyout)
        self._cancel_close(flyout)
        if flyout.mapped or self.top.state() == "withdrawn":
            return
        self._close_siblings(flyout)
        self._fit_flyout(flyout)
        flyout.top.deiconify()
        flyout.top.update_idletasks()
        flyout.top.lift()
        flyout.mapped = True
        self._open_flyouts.append(flyout)

    def _close_flyout(self, flyout: MenuFlyout) -> None:
        self._cancel_open(flyout)
        self._cancel_close(flyout)
        if flyout in self._open_flyouts:
            self._open_flyouts.remove(flyout)
        if flyout.mapped:
            flyout.mapped = False
            try:
                flyout.top.withdraw()
            except tk.TclError:
                pass

    def _close_siblings(self, flyout: MenuFlyout) -> None:
        for other in list(self._open_flyouts):
            if other is not flyout:
                self._close_flyout(other)

    def _close_all(self) -> None:
        for flyout in list(self._open_flyouts):
            self._close_flyout(flyout)

    def _schedule_fit_flyout(self, flyout: MenuFlyout) -> None:
        if flyout.mapped:
            flyout.top.after_idle(lambda f=flyout: self._fit_flyout(f))

    def _fit_flyout(self, flyout: MenuFlyout) -> None:
        flyout.body.update_idletasks()
        width = max(MENU_MIN_WIDTH, flyout.body.winfo_reqwidth() + 16)
        height = flyout.body.winfo_reqheight() + 14
        x, y = self._flyout_origin(flyout, width, height)
        flyout.top.geometry(f"{width}x{height}+{x}+{y}")
        flyout.top.update_idletasks()
        if sys.platform == "win32":
            _win32_round_window_region(
                flyout.top.winfo_id(), width, height, MENU_RADIUS
            )
        flyout.top.lift()

    def _flyout_origin(
        self, flyout: MenuFlyout, width: int, height: int
    ) -> tuple[int, int]:
        """Place the flyout to the right of its parent button, top-aligned;
        flip to the left when it would overflow the monitor's right edge."""
        btn = flyout.parent_btn
        px = btn.winfo_rootx()
        py = btn.winfo_rooty()
        pw = btn.winfo_width()
        ph = btn.winfo_height()
        x = px + pw + MENU_GAP
        y = py
        right = self._screen_right(px, py, pw, ph)
        if right is not None and x + width > right:
            x = px - width - MENU_GAP
        return self._clamped_menu_pos(x, y, width, height)

    def _screen_right(self, px: int, py: int, pw: int, ph: int) -> int | None:
        """Right edge of the monitor nearest the button (work area first,
        virtual screen second, Tk screen as the non-Windows fallback)."""
        bounds = _get_monitor_work_area_for_rect(px, py, pw, ph)
        if bounds:
            return bounds[2]
        virt = _get_virtual_screen_bounds()
        if virt:
            return virt[2]
        return self.top.winfo_screenwidth()

    # ── Shared helpers ───────────────────────────────────────────────

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

    def _action_item(
        self,
        parent,
        text: str,
        command,
        *,
        foreground: str | None = None,
    ) -> RoundedButton:
        """A menu item that runs *command* and then closes the whole menu
        tree (actions like demo / self-test / hooks / exit)."""
        def run() -> None:
            try:
                command()
            finally:
                self.hide()
        return self._menu_item(parent, text, run, foreground=foreground)

    def _schedule_fit(self) -> None:
        if self.top.state() != "withdrawn":
            self.top.after_idle(self._fit_current_page)

    def _fit_current_page(self) -> None:
        self.body.update_idletasks()
        width = max(MENU_MIN_WIDTH, self.body.winfo_reqwidth() + 16)  # 196 = 面板最小宽度
        height = self.body.winfo_reqheight() + 14
        x, y = self._clamped_menu_pos(
            self.top.winfo_x(), self.top.winfo_y(), width, height
        )
        self.top.geometry(f"{width}x{height}+{x}+{y}")
        self.top.update_idletasks()
        if sys.platform == "win32":
            _win32_round_window_region(self.top.winfo_id(), width, height, MENU_RADIUS)

    def _clamped_menu_pos(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        x_shift: int = 0,
        y_shift: int = 0,
    ) -> tuple[int, int]:
        """把菜单矩形钳制到最近显示器的工作区。

        ``winfo_screenwidth()`` 只反映主显示器，桌宠位于副屏/负坐标屏时
        会被错误钳回主屏。这里与 ``window.py:_screen_visible_geom`` 走同一
        套解析链：最近显示器工作区 → 虚拟桌面外接矩形 → Tk 屏幕（非
        Windows 兜底）。
        """
        x = int(x) + int(x_shift)
        y = int(y) + int(y_shift)
        bounds = _get_monitor_work_area_for_rect(x, y, width, height)
        if bounds is None:
            bounds = _get_virtual_screen_bounds()
        if bounds is None:
            left, top = 0, 0
            right = self.top.winfo_screenwidth()
            bottom = self.top.winfo_screenheight()
        else:
            left, top, right, bottom = bounds
        # 四周留 8 px；底部额外留 48 px，避免菜单贴住任务栏/屏幕下缘。
        inner = (left + 8, top + 8, right - 8, bottom - 48)
        return _clamp_window_to_bounds(x, y, width, height, inner, margin=8)

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
        # 拖动根菜单时让已打开的子菜单跟随锚定到父按钮。
        for flyout in list(self._open_flyouts):
            self._schedule_fit_flyout(flyout)

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
                    "Hooks 已卸载，重启 Codex 后生效。",
                    parent=self.top,
                )
            else:
                install_hooks()
                messagebox.showinfo(
                    "Codex Hooks 已安装",
                    f"配置位置：\n{user_hooks_path()}\n\n"
                    "请重启 Codex，并在 /hooks 中信任两个命令。",
                    parent=self.top,
                )
        except Exception as error:
            LOGGER.exception("could not toggle Codex hooks")
            messagebox.showerror("Codex Hooks", str(error), parent=self.top)
        self.refresh()

    def _toggle_indicator(self) -> None:
        self.window.toggle_indicator()
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
        self.size_btn.set_style(
            text=f"桌宠大小  {round(self.window.scale * 100)}%  ›"
        )
        self.hook_btn.set_style(
            text="Hooks ✓" if hook_enabled else "Hooks",
            foreground=self.accent if hook_enabled else self.text,
            background=self.accent_soft if hook_enabled else self.bg,
            outline=self.accent_soft if hook_enabled else self.border,
        )
        self.autostart_btn.set_style(text=self.window._autostart_label)

        indicator_enabled = bool(self.window.indicator_enabled_var.get())
        self._set_toggle_style(
            self.indicator_toggle_btn, indicator_enabled, "显示状态与 Token"
        )

        position = self.window.indicator_position
        for pos, button in self.indicator_radio_buttons.items():
            selected = pos == position
            button.set_style(
                foreground=self.accent if selected else self.muted,
                background=self.accent_soft if selected else self.surface,
            )

        current_pct = round(self.window.scale * 100)
        self.size_current_label.configure(
            text=f"当前 {current_pct}%"
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

        # 状态/勾选变化可能改变条目宽度，重排已打开的子菜单。
        for flyout in list(self._open_flyouts):
            self._schedule_fit_flyout(flyout)
        self._schedule_fit()

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
        self._close_all()
        self.refresh()
        self.body.update_idletasks()
        width = max(MENU_MIN_WIDTH, self.body.winfo_reqwidth() + 16)
        height = self.body.winfo_reqheight() + 14
        x, y = self._clamped_menu_pos(
            x, y, width, height,
            x_shift=-width + 18,  # 光标右侧留一小段
            y_shift=-18,          # 菜单出现在光标上方一点
        )
        self.top.geometry(f"{width}x{height}+{x}+{y}")
        self.top.deiconify()
        self.top.update_idletasks()
        if sys.platform == "win32":
            _win32_round_window_region(
                self.top.winfo_id(), width, height, MENU_RADIUS
            )
        self.top.lift()
        self.top.focus_force()

    def hide(self) -> None:
        self._close_all()
        try:
            self.top.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._close_all()
        for flyout in self._flyouts.values():
            try:
                flyout.top.destroy()
            except tk.TclError:
                pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass
