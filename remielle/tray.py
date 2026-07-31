"""Windows system-tray icon via ``Shell_NotifyIconW``.

Zero dependencies beyond ctypes — follows the same Win32 calling
conventions as ``win32_helpers.py``.
"""

from __future__ import annotations

import ctypes as _ctypes
from ctypes import wintypes as _w32
from collections import deque
import logging
import sys
from typing import Callable

LOGGER = logging.getLogger("remielle-codex-bridge")

# ══ Win32 constants ═══════════════════════════════════════════════

_NIM_ADD = 0x00000000
_NIM_MODIFY = 0x00000001
_NIM_DELETE = 0x00000002
_NIM_SETVERSION = 0x00000004

_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_NIF_STATE = 0x00000008
_NIF_INFO = 0x00000010
_NIF_GUID = 0x00000020
_NIF_SHOWTIP = 0x00000080

_NIS_HIDDEN = 0x00000001
_NIS_SHAREDICON = 0x00000002

_NOTIFYICON_VERSION_4 = 4

TRAY_CALLBACK = 0x500  # WM_USER is 0x400; offset to avoid collisions

_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_WM_CONTEXTMENU = 0x007B
_NIN_SELECT = 0x0400
_NIN_KEYSELECT = 0x0401

_GWL_WNDPROC = -4
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010
_LR_DEFAULTSIZE = 0x00000040

_WNDPROC = _ctypes.WINFUNCTYPE(
    _ctypes.c_ssize_t,   # LRESULT (= LONG_PTR on 64-bit)
    _w32.HWND,           # hwnd
    _w32.UINT,           # msg
    _w32.WPARAM,         # wparam
    _w32.LPARAM,         # lparam
)

# ══ NOTIFYICONDATAW struct ═══════════════════════════════════════

class _NOTIFYICONDATAW(_ctypes.Structure):
    _fields_ = [
        ("cbSize", _w32.DWORD),
        ("hWnd", _w32.HWND),
        ("uID", _w32.UINT),
        ("uFlags", _w32.UINT),
        ("uCallbackMessage", _w32.UINT),
        ("hIcon", _w32.HICON),
        ("szTip", _w32.WCHAR * 128),
        ("dwState", _w32.DWORD),
        ("dwStateMask", _w32.DWORD),
        ("szInfo", _w32.WCHAR * 256),
        ("uTimeoutOrVersion", _w32.UINT),   # uVersion for NIM_SETVERSION
        ("szInfoTitle", _w32.WCHAR * 64),
        ("dwInfoFlags", _w32.DWORD),
        ("guidItem", _w32.BYTE * 16),
        ("hBalloonIcon", _w32.HICON),
    ]

# ══ Win32 API declarations ═══════════════════════════════════════

_user32 = _ctypes.windll.user32
_shell32 = _ctypes.windll.shell32

_user32.SetWindowLongPtrW.argtypes = [_w32.HWND, _ctypes.c_int, _w32.LPARAM]
_user32.SetWindowLongPtrW.restype = _w32.LPARAM
_user32.GetWindowLongPtrW.argtypes = [_w32.HWND, _ctypes.c_int]
_user32.GetWindowLongPtrW.restype = _w32.LPARAM
_user32.CallWindowProcW.argtypes = [_w32.LPARAM, _w32.HWND, _w32.UINT,
                                    _w32.WPARAM, _w32.LPARAM]
_user32.CallWindowProcW.restype = _w32.LPARAM
_user32.LoadImageW.argtypes = [_w32.HINSTANCE, _w32.LPCWSTR, _w32.UINT,
                               _ctypes.c_int, _ctypes.c_int, _w32.UINT]
_user32.LoadImageW.restype = _w32.HANDLE
_user32.DestroyIcon.argtypes = [_w32.HICON]
_user32.DestroyIcon.restype = _w32.BOOL

_shell32.Shell_NotifyIconW.argtypes = [_w32.DWORD, _ctypes.POINTER(_NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = _w32.BOOL


# ══ Helpers ═══════════════════════════════════════════════════════

def _create_hicon_from_file(path: str) -> int:
    """Load a 32×32 icon from *path*, returning its ``HICON`` handle."""
    handle = _user32.LoadImageW(
        0, path, _IMAGE_ICON, 32, 32,
        _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
    )
    if not handle:
        LOGGER.warning("tray: LoadImageW failed for %s (err=%d)",
                       path, _ctypes.get_last_error())
        return 0
    return handle


# ══ TrayIcon ══════════════════════════════════════════════════════

class TrayIcon:
    """A Windows notification-area (system-tray) icon.

    Parameters
    ----------
    hwnd:
        Native window handle of the Tk root window.
    window_ref:
        Reference to the ``RemielleWindow`` instance, used for toggling
        visibility and showing the right-click menu.
    icon_path:
        Absolute path to a ``.ico`` file (loaded via ``LoadImageW``).
    tip:
        Initial tooltip text shown when the cursor hovers over the icon.
    """

    def __init__(self, hwnd: int, window_ref,
                 icon_path: str, tip: str = "蕾米 AI 助手") -> None:
        if sys.platform != "win32":
            return  # Tray icons are Windows-only; silently skip

        self._hwnd = hwnd
        self._window = window_ref
        self._tip = tip
        self._destroyed = False
        self._added = False
        self._pending_events: deque[int] = deque()
        self._drain_job: str | None = None
        self._version4 = False
        self._menu_pending = False

        # ── Load icon ──
        self._hicon: int = _create_hicon_from_file(icon_path)
        if not self._hicon:
            return

        # ── Subclass the window procedure ──
        # WNDPROC callback MUST be kept alive as an instance attribute
        # — Python GC on a temporary ctypes callback ⇒ use-after-free.
        self._wndproc_callback = _WNDPROC(self._wnd_proc)
        self._old_wndproc: int = 0
        self._subclass_window()

        # ── Build NOTIFYICONDATA ──
        self._nid = _NOTIFYICONDATAW()
        self._nid.cbSize = _ctypes.sizeof(_NOTIFYICONDATAW)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        self._nid.uCallbackMessage = TRAY_CALLBACK
        self._nid.hIcon = self._hicon
        self._nid.szTip = tip

        # ── Add to tray ──
        if not _shell32.Shell_NotifyIconW(_NIM_ADD, _ctypes.byref(self._nid)):
            LOGGER.error("tray: NIM_ADD failed (err=%d)",
                         _ctypes.get_last_error())
            # Rollback: undo everything we've set up so far
            self._unsubclass_window()
            _user32.DestroyIcon(self._hicon)
            self._hicon = 0
            return

        self._added = True

        # ── Set version (Win 7+) ──
        self._nid.uTimeoutOrVersion = _NOTIFYICON_VERSION_4
        self._version4 = bool(
            _shell32.Shell_NotifyIconW(_NIM_SETVERSION,
                                       _ctypes.byref(self._nid))
        )
        if not self._version4:
            LOGGER.warning(
                "tray: NIM_SETVERSION failed err=%d",
                _ctypes.get_last_error(),
            )

        # ── Start draining tray events on Tk's event loop ──
        # WndProc must NOT touch Tk objects — it only queues events.
        # This after() timer drains the queue safely from Tk's thread.
        self._drain_job = self._window.root.after(
            30,
            self._drain_tray_events,
        )

        LOGGER.info("tray: icon added tip=%r version4=%s", tip, self._version4)

    # ── Public API ────────────────────────────────────────────────

    def set_tip(self, text: str) -> None:
        """Update the tray icon tooltip."""
        if not self._hicon:
            return
        if text == self._tip:
            return
        self._tip = text
        self._nid.szTip = text
        _shell32.Shell_NotifyIconW(_NIM_MODIFY, _ctypes.byref(self._nid))

    def destroy(self) -> None:
        """Remove the tray icon and restore the original window procedure.

        Idempotent — safe to call multiple times, even if ``NIM_ADD``
        failed during ``__init__``.  Order matters:
        1. Cancel the drain timer
        2. NIM_DELETE — removes icon from the notification area
        3. Restore the original WndProc
        4. DestroyIcon — release the icon resource last
        """
        if self._destroyed:
            return

        self._destroyed = True

        if self._drain_job is not None:
            try:
                self._window.root.after_cancel(self._drain_job)
            except Exception:
                pass
            self._drain_job = None

        self._pending_events.clear()

        if self._added:
            LOGGER.info("tray: destroying icon")
            _shell32.Shell_NotifyIconW(_NIM_DELETE, _ctypes.byref(self._nid))
            self._added = False

        self._unsubclass_window()
        if self._hicon:
            _user32.DestroyIcon(self._hicon)
            self._hicon = 0

    # ── Window procedure subclassing ──────────────────────────────

    def _subclass_window(self) -> None:
        """Replace the Tk window's WndProc with our own.

        Saves the original procedure address so we can forward every
        message that we don't handle ourselves.
        """
        self._old_wndproc = _user32.SetWindowLongPtrW(
            self._hwnd, _GWL_WNDPROC,
            _ctypes.cast(self._wndproc_callback, _ctypes.c_void_p).value,
        )
        if not self._old_wndproc:
            LOGGER.error("tray: WndProc subclass failed hwnd=%#x err=%d",
                         self._hwnd, _ctypes.get_last_error())

    def _unsubclass_window(self) -> None:
        """Restore the original WndProc saved during ``_subclass_window``."""
        if self._old_wndproc and self._hwnd:
            _user32.SetWindowLongPtrW(
                self._hwnd, _GWL_WNDPROC, self._old_wndproc,
            )
            self._old_wndproc = 0

    def _wnd_proc(self, hwnd: int, msg: int,
                  wparam: int, lparam: int) -> int:
        """Subclassed window procedure — ONLY queues events, never touches Tk.

        Calling Tk methods (show/hide/menu) from inside a Win32 WndProc
        callback causes re-entrancy crashes because Tk's event loop is
        not re-entrant.  Instead we append the event to a deque and let
        ``_drain_tray_events`` (which runs on Tk's ``after`` timer)
        handle it safely.
        """
        if msg == TRAY_CALLBACK:
            event = int(lparam) & 0xFFFF
            self._pending_events.append(event)
            return 0

        if self._old_wndproc:
            return _user32.CallWindowProcW(
                self._old_wndproc, hwnd, msg, wparam, lparam,
            )
        return 0

    # ── Event draining (runs on Tk's event loop, NOT WndProc) ─────

    def _drain_tray_events(self) -> None:
        """Process queued tray events safely from Tk's main loop.

        Runs every ~30 ms via ``root.after()``.  Drains ALL pending
        events from the deque and dispatches the last meaningful action.
        This naturally deduplicates — if a single physical click fires
        multiple notifications (e.g. WM_LBUTTONUP + NIN_SELECT), only
        one action is taken.
        """
        if self._destroyed:
            return

        action: str | None = None

        while self._pending_events:
            event = self._pending_events.popleft()

            if self._version4:
                # VERSION_4 only delivers canonical notifications.
                if event in (_NIN_SELECT, _NIN_KEYSELECT):
                    action = "toggle"
                elif event == _WM_CONTEXTMENU:
                    action = "menu"
            else:
                # Legacy fallback (pre-Win7 — unlikely but harmless).
                if event == _WM_LBUTTONUP:
                    action = "toggle"
                elif event == _WM_RBUTTONUP:
                    action = "menu"

        if action == "toggle":
            self._on_left_click()
        elif action == "menu":
            # Delay so the physical right-button release that opened
            # the menu doesn't also select the first item under cursor.
            self._window.root.after(150, self._on_right_click)

        if not self._destroyed:
            self._drain_job = self._window.root.after(
                30,
                self._drain_tray_events,
            )

    # ── Click handlers ────────────────────────────────────────────

    def _on_left_click(self) -> None:
        """Toggle the pet window visible / hidden.

        Uses tray-specific hide/restore so animation state is preserved
        across show/hide cycles instead of being killed.
        """
        if self._window.root.state() == "withdrawn":
            self._window.restore_from_tray()
        else:
            self._window.hide_to_tray()

    def _on_right_click(self) -> None:
        """Post the right-click context menu at the current cursor position.

        Guards against double-open via ``_menu_pending`` and adds an
        additional 150 ms delay so the right-click release that opens
        the menu doesn't also select the item under the cursor.
        """
        if self._menu_pending:
            return
        from .win32_helpers import _get_cursor_pos
        pos = _get_cursor_pos()
        if pos is None:
            return
        self._menu_pending = True
        x, y = pos

        def _open_menu() -> None:
            try:
                self._window.show_menu_at(x, y, source="tray")
            finally:
                self._menu_pending = False

        self._window.root.after(150, _open_menu)
