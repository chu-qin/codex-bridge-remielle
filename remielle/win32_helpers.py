from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes as _ctypes
    from ctypes import wintypes as _w32

    _GWL_EXSTYLE = -20
    _WS_EX_TRANSPARENT = 0x00000020
    _WS_EX_TOOLWINDOW = 0x00000080

    def _win32_set_clickthrough(hwnd: int, enabled: bool) -> None:
        """When *enabled* mouse events pass straight through the window."""
        style = _ctypes.windll.user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        if enabled:
            style |= _WS_EX_TRANSPARENT
        else:
            style &= ~_WS_EX_TRANSPARENT
        _ctypes.windll.user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style)

    class _MARGINS(_ctypes.Structure):
        _fields_ = [("cxLeftWidth", _ctypes.c_int),
                    ("cxRightWidth", _ctypes.c_int),
                    ("cxTopHeight", _ctypes.c_int),
                    ("cxBottomHeight", _ctypes.c_int)]

    def _win32_remove_window_border(hwnd: int) -> None:
        """Strip every visual chrome that can cause a border around the window.

        On Windows 10 / 11, ``overrideredirect(True)`` alone is not
        enough — DWM can still render a thin frame, shadow, or rounded
        corners.  We attack the problem from five angles:

        1. Disable non-client rendering (kills the DWM drop-shadow)
        2. Disable rounded corners (Win 11)
        3. DWM negative margins (extend glass over entire window)
        4. Strip extended-style edge flags
        5. ``SetWindowPos`` with ``SWP_FRAMECHANGED`` to force a
           non-client-area recompute
        """
        # ── 1. Disable non-client rendering (removes the drop-shadow) ──
        _DWMWA_NCRENDERING_POLICY = 2
        _DWMNCRP_DISABLED = 1
        try:
            policy = _ctypes.c_int(_DWMNCRP_DISABLED)
            _ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_NCRENDERING_POLICY,
                _ctypes.byref(policy), _ctypes.sizeof(policy),
            )
        except Exception:
            pass

        # ── 2. Disable rounded corners (Win 11) ──
        _DWMWA_WINDOW_CORNER_PREFERENCE = 33
        _DWMWCP_DONOTROUND = 1
        try:
            corner = _ctypes.c_int(_DWMWCP_DONOTROUND)
            _ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE,
                _ctypes.byref(corner), _ctypes.sizeof(corner),
            )
        except Exception:
            pass

        # ── 3. DWM frame margins ──
        try:
            margins = _MARGINS(-1, -1, -1, -1)
            _ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, _ctypes.byref(margins),
            )
        except Exception:
            pass

        # ── 4. Extended style: strip window edge & friends ──
        _WS_EX_WINDOWEDGE = 0x00000100      # 3D raised border
        _WS_EX_DLGMODALFRAME = 0x00000001   # double border (dialog style)
        _WS_EX_STATICEDGE = 0x00020000        # 3D sunken border
        _WS_EX_CLIENTEDGE = 0x00000200        # same as above, Win32 variant
        _GWL_EXSTYLE = -20

        try:
            ex_style = _ctypes.windll.user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
            ex_style &= ~(_WS_EX_WINDOWEDGE | _WS_EX_DLGMODALFRAME
                          | _WS_EX_STATICEDGE | _WS_EX_CLIENTEDGE)
            _ctypes.windll.user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, ex_style)
        except Exception:
            pass

        # ── 5. Force a full frame recompute ──
        _SWP_NOMOVE = 0x0002
        _SWP_NOSIZE = 0x0001
        _SWP_NOZORDER = 0x0004
        _SWP_FRAMECHANGED = 0x0020
        _SWP_NOACTIVATE = 0x0010
        try:
            _ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER
                | _SWP_FRAMECHANGED | _SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def _win32_clip_window_region(hwnd: int, width: int, height: int) -> None:
        """Set a rectangular window region that exactly clips to ``(width, height)``.

        This is the nuclear option — it tells GDI to ignore everything
        outside the rectangle, so even if DWM renders a shadow or border,
        it is physically clipped away.
        """
        try:
            rgn = _ctypes.windll.gdi32.CreateRectRgn(0, 0, width, height)
            _ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
            # rgn ownership transfers to the window — do NOT DeleteObject it
        except Exception:
            pass

    def _get_virtual_screen_bounds() -> tuple[int, int, int, int] | None:
        """Return ``(left, top, right, bottom)`` of the virtual desktop
        spanning **all** monitors, or ``None`` on failure.

        Uses ``GetSystemMetrics`` because tkinter's ``winfo_screenwidth`` /
        ``winfo_screenheight`` only cover the primary monitor.
        """
        _SM_XVIRTUALSCREEN = 76
        _SM_YVIRTUALSCREEN = 77
        _SM_CXVIRTUALSCREEN = 78
        _SM_CYVIRTUALSCREEN = 79
        try:
            left = _ctypes.windll.user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            top = _ctypes.windll.user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            right = left + _ctypes.windll.user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
            bottom = top + _ctypes.windll.user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
            return (left, top, right, bottom)
        except Exception:
            return None

    def _get_cursor_pos() -> tuple[int, int] | None:
        """Return ``(x, y)`` of the cursor in screen coordinates.

        Used by the tray icon to position the right-click menu at the
        cursor rather than at a stale event position.
        """
        pt = _w32.POINT()
        if _ctypes.windll.user32.GetCursorPos(_ctypes.byref(pt)):
            return (pt.x, pt.y)
        return None

    # ── Native Windows popup menu (independent of tkinter's window system) ──
    # tk_popup on an overrideredirect + transparent-color window causes
    # persistent z-order flicker that cannot be fully resolved from Python.
    # The Windows native TrackPopupMenu API avoids the problem entirely.

    _MF_STRING = 0x00000000
    _MF_SEPARATOR = 0x00000800
    _MF_GRAYED = 0x00000001
    _MF_DISABLED = 0x00000002
    _MF_POPUP = 0x00000010
    _MF_CHECKED = 0x00000008
    _TPM_RETURNCMD = 0x00000100
    _TPM_NONOTIFY = 0x00000080
    _TPM_RIGHTBUTTON = 0x00000002  # (no longer used — kept for reference)
    _TPM_BOTTOMALIGN = 0x00000020
    _WM_NULL = 0x0000

    _user32 = _ctypes.windll.user32
    _user32.CreatePopupMenu.restype = _w32.HMENU
    _user32.DestroyMenu.argtypes = [_w32.HMENU]
    _user32.DestroyMenu.restype = _w32.BOOL
    _user32.AppendMenuW.argtypes = [_w32.HMENU, _w32.UINT, _w32.WPARAM, _w32.LPCWSTR]
    _user32.AppendMenuW.restype = _w32.BOOL
    _user32.TrackPopupMenu.argtypes = [
        _w32.HMENU, _w32.UINT, _ctypes.c_int, _ctypes.c_int,
        _ctypes.c_int, _w32.HWND, _w32.LPVOID,
    ]
    _user32.TrackPopupMenu.restype = _w32.UINT
    _user32.GetForegroundWindow.restype = _w32.HWND
    _user32.SetForegroundWindow.argtypes = [_w32.HWND]
    _user32.SetForegroundWindow.restype = _w32.BOOL
    _user32.PostMessageW.argtypes = [_w32.HWND, _w32.UINT, _w32.WPARAM, _w32.LPARAM]
    _user32.PostMessageW.restype = _w32.BOOL

    def _native_create_menu() -> int:
        return _user32.CreatePopupMenu()

    def _native_destroy_menu(hmenu: int) -> None:
        _user32.DestroyMenu(hmenu)

    def _native_add_item(hmenu: int, text: str, item_id: int,
                         *, disabled: bool = False) -> None:
        flags = _MF_STRING
        if disabled:
            flags |= _MF_GRAYED | _MF_DISABLED
        _user32.AppendMenuW(hmenu, flags, item_id, text)

    def _native_add_check(hmenu: int, text: str, item_id: int,
                          *, checked: bool = False) -> None:
        flags = _MF_STRING | (_MF_CHECKED if checked else 0)
        _user32.AppendMenuW(hmenu, flags, item_id, text)

    def _native_add_sep(hmenu: int) -> None:
        _user32.AppendMenuW(hmenu, _MF_SEPARATOR, 0, None)

    def _native_add_sub(hmenu: int, text: str, sub_hmenu: int) -> None:
        _user32.AppendMenuW(hmenu, _MF_POPUP, sub_hmenu, text)

    def _native_track(hmenu: int, hwnd: int, x: int, y: int) -> int:
        """Show the popup; return the selected item ID, or 0 if dismissed.

        ``TPM_RIGHTBUTTON`` is intentionally omitted — it would let the
        same right-click that opens the tray menu also select a menu
        item, causing accidental "exit" selections.
        """
        if not hwnd:
            hwnd = _user32.GetForegroundWindow() or 0
        if hwnd:
            _user32.SetForegroundWindow(hwnd)
        cmd = _user32.TrackPopupMenu(
            hmenu,
            _TPM_RETURNCMD | _TPM_NONOTIFY | _TPM_BOTTOMALIGN,
            x, y, 0, hwnd, None,
        )
        # Standard workaround: post a benign message so the tray menu
        # correctly dismisses and focus returns to the calling window.
        if hwnd:
            _user32.PostMessageW(hwnd, _WM_NULL, 0, 0)
        return int(cmd)

else:
    # Non-Windows stub — native menus aren't available; fall back gracefully.
    def _win32_set_clickthrough(hwnd: int, enabled: bool) -> None:
        pass

    def _win32_remove_window_border(hwnd: int) -> None:
        pass

    def _win32_clip_window_region(hwnd: int, width: int, height: int) -> None:
        pass

    def _get_virtual_screen_bounds() -> None:
        return None

    def _get_cursor_pos() -> None:
        return None

    def _native_create_menu() -> int:
        return 0

    def _native_destroy_menu(hmenu: int) -> None:
        pass

    def _native_add_item(hmenu: int, text: str, item_id: int,
                         *, disabled: bool = False) -> None:
        pass

    def _native_add_check(hmenu: int, text: str, item_id: int,
                          *, checked: bool = False) -> None:
        pass

    def _native_add_sep(hmenu: int) -> None:
        pass

    def _native_add_sub(hmenu: int, text: str, sub_hmenu: int) -> None:
        pass

    def _native_track(hmenu: int, hwnd: int, x: int, y: int) -> int:
        return 0
