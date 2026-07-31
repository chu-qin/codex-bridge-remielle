from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
import tkinter as tk
from typing import Callable

from PIL import Image, ImageTk

# Windows API helpers — make transparent areas truly click-through
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
    _TPM_RIGHTBUTTON = 0x00000002

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
    _user32.TrackPopupMenu.restype = _w32.BOOL
    _user32.GetForegroundWindow.restype = _w32.HWND  # fallback when _hwnd == 0

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
        """Show the popup; return the selected item ID, or 0 if dismissed."""
        if not hwnd:
            hwnd = _user32.GetForegroundWindow() or 0
        return _user32.TrackPopupMenu(
            hmenu,
            _TPM_RETURNCMD | _TPM_NONOTIFY | _TPM_RIGHTBUTTON,
            x, y, 0, hwnd, None,
        )

    # Track all menu handles for cleanup on exit
    _NATIVE_MENU_HANDLES: list[int] = []

    def _native_cleanup_all_menus() -> None:
        for h in _NATIVE_MENU_HANDLES:
            try:
                _user32.DestroyMenu(h)
            except Exception:
                pass
        _NATIVE_MENU_HANDLES.clear()

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

    def _native_cleanup_all_menus() -> None:
        pass

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
SETTINGS_PATH = APP_DIR / "settings.json"
LOG_PATH = APP_DIR / "remilia-bridge.log"
TRANSPARENT_COLOR = "#010203"
_TRANSPARENT_RGB = (1, 2, 3)  # (R, G, B) for pixel-level colorkey fill
TERMINAL_EVENT_TYPES = {
    "task_complete",
    "task_failed",
    "task_cancelled",
    "turn_aborted",
    "turn_cancelled",
}

# ── Embedded defaults — auto-written to disk on first run ──────
_DEFAULT_CONFIG: dict = {
    "asset_dir": "assets/gif",
    "coordinate_file": "assets/坐标配置.json",
    "codex_sessions_dir": "%USERPROFILE%/.codex/sessions",
    "codex_global_state_path": "%USERPROFILE%/.codex/.codex-global-state.json",
    "claude_sessions_dir": "%USERPROFILE%/.claude/sessions",
    "actions": {
        "startup": "期待.gif",
        "idle": "待机.gif",
        "thinking": "思考.gif",
        "ready": "拿笔待机.gif",
        "running": "连续绘制.gif",
        "running_intermittent": "间歇绘制.gif",
        "complete": "得意.gif",
    },
    "display": {
        "persistent": True,
        "auto_hide_after_complete": False,
        "clickthrough_on_idle": False,
        "clickthrough_on_startup": True,
    },
    "activity_thresholds": {
        "thinking_timeout_seconds": 10.0,
        "intermittent_timeout_seconds": 3.0,
    },
    "poll_interval_ms": 350,
    "unread_settle_ms": 2500,
    "recent_session_days": 2,
    "default_scale": 0.75,
    "topmost": True,
    "startup_delay_seconds": 0,
    "min_play_ms": 2000,
}

_DEFAULT_COORDINATES: dict = {
    "待机.gif": {"x": 0, "y": 0},
    "得意.gif": {"x": 15, "y": 0},
    "思考.gif": {"x": 0, "y": -3},
    "拿笔待机.gif": {"x": 0, "y": -5},
    "期待.gif": {"x": 0, "y": -5},
    "连续绘制.gif": {"x": -39, "y": 4},
    "间歇绘制.gif": {"x": -45, "y": -10},
}


def _write_json_if_missing(path: Path, data: dict) -> None:
    """Write *data* to *path* if the file does not already exist."""
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # best-effort; the app can still run with embedded defaults


def init_default_files() -> None:
    """Ensure config.json and 坐标配置.json exist on disk.

    On a fresh install these files are missing.  We seed them from the
    embedded defaults so the user can edit them later.  The app itself
    only ever reads them via ``load_json`` (which falls back to the
    embedded defaults if the files are absent).
    """
    _write_json_if_missing(CONFIG_PATH, _DEFAULT_CONFIG)
    _write_json_if_missing(expand_path("assets/坐标配置.json"), _DEFAULT_COORDINATES)


def load_json(path: Path, fallback: dict | None = None) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(fallback or {})


def expand_path(value: str, base: Path = APP_DIR) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    return path if path.is_absolute() else base / path


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("remilia-codex-bridge")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    return logger


LOGGER = configure_logging()


# ── Single-instance via PID file (self-cleaning) ──────────────

_PID_PATH = APP_DIR / "remilia-bridge.pid"


def _pid_is_alive(pid: int) -> bool:
    """Return True if a process with *pid* is still running on Windows.

    Uses ``GetExitCodeProcess`` which is the canonical Windows API for
    this check — it returns ``STILL_ACTIVE`` (259) while the process
    hasn't terminated.
    """
    if sys.platform != "win32":
        return False
    import ctypes as _c
    PROCESS_QUERY_LIMITED = 0x1000
    STILL_ACTIVE = 259
    h = _c.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return False  # can't open → process is gone or access denied
    code = _c.wintypes.DWORD()
    ok = _c.windll.kernel32.GetExitCodeProcess(h, _c.byref(code))
    _c.windll.kernel32.CloseHandle(h)
    return ok and code.value == STILL_ACTIVE


def check_single_instance() -> bool:
    """Return True if this is the only instance; False if another is alive.

    Uses a PID file with exclusive-create semantics so two concurrent
    launches cannot both pass the check.  Stale files (PID no longer
    running) are automatically cleaned.
    """
    my_pid = str(os.getpid())
    try:
        # Atomic: only one process can create the file
        _PID_PATH.write_text(my_pid, encoding="utf-8")
        # Re-read to verify we still own it (handles a very tight race)
        if _PID_PATH.read_text(encoding="utf-8").strip() == my_pid:
            return True
        # Someone else grabbed it — fall through to the alive check
    except OSError:
        pass

    # File already exists — check whether that process is still alive
    try:
        old_text = _PID_PATH.read_text(encoding="utf-8").strip()
        old_pid = int(old_text)
    except (FileNotFoundError, ValueError, OSError):
        old_pid = 0

    if old_pid and old_pid != os.getpid() and _pid_is_alive(old_pid):
        return False  # another instance is really running

    # Stale PID or our own (from a previous run) — take over
    _PID_PATH.write_text(my_pid, encoding="utf-8")
    return True


def release_single_instance() -> None:
    """Remove the PID file on clean exit."""
    try:
        _PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


class CodexSessionWatcher:
    def __init__(
        self,
        sessions_dir: Path,
        recent_days: int = 2,
        discovery_interval_seconds: float = 2.0,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.recent_days = recent_days
        self.discovery_interval_seconds = discovery_interval_seconds
        self.active_turns: set[str] = set()
        self.offsets: dict[Path, int] = {}
        self.partial: dict[Path, bytes] = {}
        self.events: list[dict[str, str]] = []
        self.last_activity_time: float = 0.0
        self._last_discovery = 0.0
        self._initializing = False

    def _recent_files(self) -> list[Path]:
        if not self.sessions_dir.exists():
            return []
        cutoff = time.time() - self.recent_days * 86400
        files: list[tuple[float, Path]] = []
        try:
            candidates = self.sessions_dir.rglob("*.jsonl")
            for path in candidates:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    files.append((mtime, path))
        except OSError:
            return []
        files.sort(key=lambda item: item[0])
        return [path for _, path in files]

    def initialize(self) -> set[str]:
        self._initializing = True
        for path in self._recent_files():
            self._read_new_bytes(path, from_start=True)
        self._initializing = False
        self._last_discovery = time.monotonic()
        LOGGER.info(
            "watcher initialized: files=%d active_turns=%d",
            len(self.offsets),
            len(self.active_turns),
        )
        return set(self.active_turns)

    @staticmethod
    def _thread_id_from_path(path: Path) -> str | None:
        match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
            r"\.jsonl$",
            path.name,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    def _process_line(self, raw: bytes, source_path: Path) -> None:
        # Quick filter: only parse lines that contain relevant event types
        if (
            b'"task_started"' not in raw
            and b'"task_complete"' not in raw
            and b'"task_failed"' not in raw
            and b'"task_cancelled"' not in raw
            and b'"turn_aborted"' not in raw
            and b'"turn_cancelled"' not in raw
            and b'"assistant_message"' not in raw
            and b'"tool_use"' not in raw
            and b'"tool_result"' not in raw
        ):
            return
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if event.get("type") != "event_msg":
            return
        payload = event.get("payload") or {}
        event_type = payload.get("type")
        turn_id = payload.get("turn_id")
        if not turn_id:
            return

        # Track activity timestamp for any event that belongs to an active turn
        if event_type == "task_started" or turn_id in self.active_turns:
            self.last_activity_time = time.monotonic()

        if event_type == "task_started":
            self.active_turns.add(turn_id)
            if not self._initializing:
                self.events.append(
                    {
                        "type": event_type,
                        "turn_id": turn_id,
                        "thread_id": self._thread_id_from_path(source_path) or "",
                    }
                )
            if not self._initializing:
                LOGGER.info(
                    "task started: %s active=%d", turn_id, len(self.active_turns)
                )
        elif event_type in TERMINAL_EVENT_TYPES:
            self.active_turns.discard(turn_id)
            if not self._initializing:
                self.events.append(
                    {
                        "type": event_type,
                        "turn_id": turn_id,
                        "thread_id": self._thread_id_from_path(source_path) or "",
                    }
                )
            if not self._initializing:
                LOGGER.info(
                    "task ended: %s type=%s active=%d",
                    turn_id,
                    event_type,
                    len(self.active_turns),
                )
        # Non-lifecycle events (assistant_message, tool_use, tool_result)
        # update last_activity_time above but do NOT generate lifecycle events

    def _read_new_bytes(self, path: Path, from_start: bool = False) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        previous = 0 if from_start else self.offsets.get(path, 0)
        if size < previous:
            previous = 0
            self.partial[path] = b""
        if size == previous:
            self.offsets[path] = size
            return
        try:
            with path.open("rb") as handle:
                handle.seek(previous)
                data = handle.read()
        except OSError:
            return
        self.offsets[path] = size
        data = self.partial.get(path, b"") + data
        lines = data.split(b"\n")
        self.partial[path] = lines.pop() if lines else b""
        for line in lines:
            self._process_line(line.strip(), path)

    def poll(self) -> tuple[int, int, list[dict[str, str]], float]:
        before = len(self.active_turns)
        now = time.monotonic()
        if now - self._last_discovery >= self.discovery_interval_seconds:
            for path in self._recent_files():
                if path not in self.offsets:
                    self._read_new_bytes(path, from_start=True)
            self._last_discovery = now
        for path in tuple(self.offsets):
            self._read_new_bytes(path)
        events = self.events
        self.events = []
        return before, len(self.active_turns), events, self.last_activity_time


class CodexUnreadWatcher:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.last_mtime_ns = -1
        self.unread_thread_ids: set[str] = set()

    def poll(self) -> set[str]:
        try:
            mtime_ns = self.state_path.stat().st_mtime_ns
        except OSError:
            return set(self.unread_thread_ids)
        if mtime_ns == self.last_mtime_ns:
            return set(self.unread_thread_ids)
        self.last_mtime_ns = mtime_ns
        state = load_json(self.state_path)
        atom_state = state.get("electron-persisted-atom-state") or {}
        unread_by_host = atom_state.get("unread-thread-ids-by-host-v1") or {}
        local = unread_by_host.get("local") or []
        new_ids = {str(value) for value in local}
        if new_ids != self.unread_thread_ids:
            LOGGER.info(
                "unread watcher: %d → %d thread IDs",
                len(self.unread_thread_ids), len(new_ids),
            )
        self.unread_thread_ids = new_ids
        return set(self.unread_thread_ids)


class ClaudeSessionWatcher:
    """Monitors ``~/.claude/sessions/*.json`` for the ``status`` field.

    Claude Code writes a per-process session JSON file that includes a
    ``status`` key.  When the model is actively processing a turn,
    ``status`` is ``"busy"``; otherwise it is ``"idle"`` (or absent).

    The watcher tracks **status transitions** across polls so that a
    ``busy → idle`` change is surfaced as a ``task_complete`` event,
    which feeds into the same ``pending_reviews → review`` pipeline
    used by Codex completions.

    It also validates that the owning PID is still alive, so a stale
    session file from a crash does not keep the pet in "busy" mode.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        self._last_mtime: float = 0.0
        self._last_activity_monotonic: float = 0.0
        self._cached_busy: bool = False
        self._cached_pid: int = 0
        self._cache_mtime: float = 0.0  # wall-clock mtime used for cache-busting
        self._next_scan: float = 0.0    # monotonic time of next permitted scan
        # Track sessions that were "busy" last poll so we can detect
        # transitions:  busy → idle  =  task completed.
        self._busy_sessions: dict[int, dict] = {}  # pid → {sessionId, name, ...}
        self._completions: list[dict] = []          # pending completion events

    @property
    def is_busy(self) -> bool:
        """Return the cached busy state (updated by ``poll()``)."""
        return self._cached_busy

    def poll(self) -> tuple[bool, float, list[dict]]:
        """Return ``(is_busy, last_activity_monotonic, completions)``.

        *is_busy* is ``True`` when at least one Claude Code session has
        ``status == "busy"`` **and** its PID is still alive.

        *last_activity_monotonic* is a ``time.monotonic()`` snapshot taken
        the first time a session file's mtime changed.

        *completions* is a list of ``task_complete``-style events (each
        has ``type``, ``thread_id``, ``source``) for any session that
        transitioned from ``busy`` to ``idle`` since the last poll.
        The list is drained on each call.
        """
        if not self.sessions_dir.exists():
            self._cached_busy = False
            self._busy_sessions.clear()
            self._completions.clear()
            return False, 0.0, []

        # Throttle directory scans to 1 Hz — session files don't change
        # faster than that, and we don't need sub-second responsiveness
        # for Claude Code status changes.
        now_mono = time.monotonic()
        if now_mono < self._next_scan:
            # Return cached state; drain any pending completions
            comps = self._completions
            self._completions = []
            return self._cached_busy, self._last_activity_monotonic, comps
        self._next_scan = now_mono + 1.0

        busy = False
        latest_mtime = 0.0
        busy_pid = 0
        current_busy_pids: set[int] = set()

        try:
            for path in self.sessions_dir.glob("*.json"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > latest_mtime:
                    latest_mtime = mtime
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                pid = data.get("pid", 0)
                sid = data.get("sessionId", "")
                name = data.get("name", "")
                if data.get("status") == "busy":
                    if pid and _pid_is_alive(pid):
                        busy = True
                        busy_pid = pid
                        current_busy_pids.add(pid)
                        # Track session info so we can emit a rich
                        # completion event when it transitions to idle.
                        if pid not in self._busy_sessions:
                            self._busy_sessions[pid] = {
                                "sessionId": sid,
                                "name": name,
                            }
        except OSError:
            pass

        # ── Detect busy → idle transitions ──
        for pid, info in list(self._busy_sessions.items()):
            if pid not in current_busy_pids:
                # This session was busy last poll but is no longer busy
                # (either status changed to "idle" or the file was deleted).
                self._completions.append({
                    "type": "task_complete",
                    "thread_id": info["sessionId"],
                    "source": "claude",
                    "session_name": info.get("name", ""),
                })
                LOGGER.info(
                    "claude session: task complete pid=%d session=%s name=%s",
                    pid, info["sessionId"], info.get("name", ""),
                )
                del self._busy_sessions[pid]

        # ── When the newest mtime advances, snap a monotonic timestamp ──
        if latest_mtime > self._last_mtime:
            self._last_mtime = latest_mtime
            self._last_activity_monotonic = time.monotonic()

        # ── Keep activity timestamp fresh while Claude Code is busy ──
        # Claude Code's session JSON mtime only changes on status
        # transitions (idle→busy, busy→idle).  During a long busy
        # period the file is NOT rewritten, so the mtime-based
        # activity tracker would otherwise stall and the pet would
        # get stuck in "thinking" after thinking_timeout_seconds.
        # Bumping the timestamp on every scan while busy keeps the
        # gap small → the state machine shows "running".
        if busy:
            self._last_activity_monotonic = time.monotonic()

        # ── Invalidate cache when the on-disk state changes ──
        if latest_mtime != self._cache_mtime or busy_pid != self._cached_pid:
            self._cache_mtime = latest_mtime
            self._cached_busy = busy
            self._cached_pid = busy_pid
            if busy:
                LOGGER.info("claude session: busy pid=%d", busy_pid)

        comps = self._completions
        self._completions = []
        return self._cached_busy, self._last_activity_monotonic, comps


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
    """
    rgba = frame.convert("RGBA")
    alpha = rgba.getchannel("A")
    # 0 = transparent, 255 = opaque
    binary_mask = alpha.point(
        lambda v: 255 if v >= alpha_threshold else 0,
    )
    foreground = rgba.convert("RGB")
    background = Image.new("RGB", rgba.size, transparent_rgb)
    return Image.composite(foreground, background, binary_mask)


class RemiliaWindow:
    def __init__(self, config: dict, on_exit: Callable[[], None]) -> None:
        self.config = config
        self.on_exit = on_exit
        self.asset_dir = expand_path(config["asset_dir"])
        self.coordinates = load_json(
            expand_path(config["coordinate_file"]), fallback=_DEFAULT_COORDINATES,
        )
        default_scale = float(config.get("default_scale", 0.75))
        self.settings = {
            "x": 155,
            "y": 448,
            "scale": default_scale,
            **load_json(SETTINGS_PATH),
        }
        self.scale = max(0.4, min(2.5, float(self.settings.get("scale", 1.0))))
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

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("蕾米 AI 助手")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(config.get("topmost", True)))
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
        display_cfg = self.config.get("display", {})
        self.persistent_var = tk.BooleanVar(value=display_cfg.get("persistent", True))
        self.autohide_var = tk.BooleanVar(
            value=display_cfg.get("auto_hide_after_complete", False))
        self.scale_var = tk.DoubleVar(value=self.scale)
        self.current_status_text = "等待 AI 任务"

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
        x = int(self.settings.get("x", v_right - width - 40))
        y = int(self.settings.get("y", v_bottom - height - 80))

        # Clamp to the virtual desktop — keep at least 40 px visible so
        # the user can always grab and drag the window back.
        x = max(v_left - width + 40, min(x, v_right - 40))
        y = max(v_top - height + 40, min(y, v_bottom - 40))
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
                    alpha_threshold=96,
                )
                frames.append(ImageTk.PhotoImage(frame, master=self.root))
                delays.append(max(15, int(gif.info.get("duration", 30))))
        self.frames = frames
        self.delays = delays
        self.current_action = gif_name
        self.frame_index = 0

    def set_status_text(self, text: str) -> None:
        """Store status text (shown as first disabled item in the native menu)."""
        self.current_status_text = text

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
        self._load_action(gif_name)
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
        actions = self.config.get("actions", {})
        idle_gif = actions.get("idle", "待机.gif")
        # (gif_name, status_text, duration_ms)
        sequence: list[tuple[str, str, int]] = [
            (actions.get("startup", "期待.gif"), "演示：启动", 1800),
            (idle_gif, "演示：待机", 1500),
            (actions.get("ready", "拿笔待机.gif"), "演示：等待输入", 1500),
            (actions.get("thinking", "思考.gif"), "演示：思考中", 1800),
            (actions.get("running", "连续绘制.gif"), "演示：工作中", 2000),
            (actions.get("running_intermittent", "间歇绘制.gif"), "演示：间歇绘制", 2000),
            (actions.get("complete", "得意.gif"), "演示：任务完成", 2500),
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
        default_scale = float(self.config.get("default_scale", 0.75))
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
            dx = v_right - bw - 40
            dy = v_bottom - bh - 80
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

    def _wheel(self, event: tk.Event) -> None:
        direction = 1 if event.delta > 0 else -1
        new_scale = max(0.4, min(2.5, round(self.scale + direction * 0.1, 1)))
        self.set_scale(new_scale)

    def set_scale(self, new_scale: float, *, save: bool = True) -> None:
        new_scale = max(0.4, min(2.5, float(new_scale)))
        if new_scale == self.scale and save:
            self.scale_var.set(new_scale)
            return
        self.scale = new_scale
        self.scale_var.set(new_scale)
        self.settings["scale"] = new_scale
        self._compute_layout()
        self._apply_geometry()
        if self.current_action:
            action = self.current_action
            loops = self.loops_remaining
            self.play(action, loops=loops, status_text=self.current_status_text)
        if save:
            self._save_settings()

    def _save_settings(self) -> None:
        try:
            SETTINGS_PATH.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            LOGGER.exception("could not save settings")


class BridgeApp:
    def __init__(self, config: dict, demo: bool = False) -> None:
        self.config = config
        sessions_dir = expand_path(
            config.get(
                "codex_sessions_dir",
                r"%USERPROFILE%\.codex\sessions",
            )
        )
        self.watcher = CodexSessionWatcher(
            sessions_dir=sessions_dir,
            recent_days=int(config.get("recent_session_days", 2)),
        )
        self.unread_watcher = CodexUnreadWatcher(
            expand_path(
                config.get(
                    "codex_global_state_path",
                    r"%USERPROFILE%\.codex\.codex-global-state.json",
                )
            )
        )
        self.claude_watcher = ClaudeSessionWatcher(
            expand_path(
                config.get(
                    "claude_sessions_dir",
                    r"%USERPROFILE%\.claude\sessions",
                )
            )
        )
        self.window = RemiliaWindow(config, self.stop)
        self.running = True
        self.demo = demo
        self.pending_reviews: dict[str, dict[str, float | bool]] = {}
        self.display_mode = "hidden"
        self._started_at = 0.0  # set in start() for grace-period logic

    def start(self) -> None:
        self._started_at = time.monotonic()
        if self.demo:
            self._demo_all()
        else:
            active = self.watcher.initialize()
            if active:
                self._transition_to("thinking")
            else:
                delay_ms = max(
                    0,
                    int(self.config.get("startup_delay_seconds", 8)) * 1000,
                )
                if delay_ms > 0:
                    self.window.root.after(delay_ms, self._startup_or_idle)
                else:
                    self._transition_to("startup")
            self._schedule_poll()
        self.window.root.mainloop()

    def _startup_or_idle(self) -> None:
        """Called after startup_delay_seconds; shows startup if still idle."""
        if self.display_mode == "hidden":
            self._transition_to("startup")

    def _demo_all(self) -> None:
        """Demo mode: cycle through all 7 states in sequence."""
        display_cfg = self.config.setdefault("display", {})
        saved_persistent = display_cfg.get("persistent", True)
        display_cfg["persistent"] = True  # temporarily on for the demo

        def _seq():
            self._transition_to("startup")
            self.window.root.after(1200, lambda: self._transition_to("idle"))
            self.window.root.after(2400, lambda: self._transition_to("ready"))
            self.window.root.after(3600, lambda: self._transition_to("thinking"))
            self.window.root.after(4800, lambda: self._transition_to("running"))
            self.window.root.after(6800, lambda: self._transition_to("running_intermittent"))
            self.window.root.after(8800, lambda: self._transition_to("review"))
            self.window.root.after(10800, lambda: self._transition_to("idle"))
            self.window.root.after(12000, _cleanup_demo)

        def _cleanup_demo():
            display_cfg["persistent"] = saved_persistent
            self.stop()
        _seq()

    # ── State machine: central dispatcher ──────────────────────────

    def _transition_to(self, mode: str) -> None:
        """Central dispatcher: change display mode, logging the transition."""
        if mode == self.display_mode:
            return
        old = self.display_mode
        self.display_mode = mode
        LOGGER.info("display transition: %s → %s", old, mode)
        getattr(self, f"_go_{mode}")()

    def _go_startup(self) -> None:
        startup_gif = self.config["actions"].get("startup", "期待.gif")
        tip = "蕾米 AI 助手已启动 · 等待任务"
        self.window.play(startup_gif, loops=1, status_text=tip,
                         min_play_ms=int(self.config.get("min_play_ms", 2000)),
                         on_end=self._on_startup_done)
        self.window.set_clickthrough(
            self.config.get("display", {}).get("clickthrough_on_startup", True))


    def _on_startup_done(self) -> None:
        """Called when the startup animation finishes playing."""
        display_cfg = self.config.get("display", {})
        if display_cfg.get("persistent", True):
            self._transition_to("idle")
        else:
            self._transition_to("hidden")

    def _go_idle(self) -> None:
        idle_gif = self.config["actions"].get("idle", "待机.gif")
        tip = "等待 AI 任务"
        self.window.play(idle_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(
            self.config.get("display", {}).get("clickthrough_on_idle", False))


    def _go_thinking(self) -> None:
        thinking_gif = self.config["actions"].get("thinking", "思考.gif")
        codex_active = len(self.watcher.active_turns) > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        n = len(self.watcher.active_turns)
        tip = f"{tool} 思考中（{n} 个任务）" if n else f"{tool} 思考中"
        self.window.play(thinking_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(False)


    def _go_ready(self) -> None:
        ready_gif = self.config["actions"].get("ready", "拿笔待机.gif")
        unread_count = len(self.unread_watcher.unread_thread_ids)
        tip = f"有未读消息（{unread_count} 个会话）"
        self.window.play(ready_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(False)


    def _go_running(self) -> None:
        running_gif = self.config["actions"].get("running", "连续绘制.gif")
        codex_active = len(self.watcher.active_turns) > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        n = len(self.watcher.active_turns)
        tip = f"{tool} 工作中（{n} 个任务）" if n else f"{tool} 工作中"
        self.window.play(running_gif, loops=None, status_text=tip,
                         min_play_ms=int(self.config.get("min_play_ms", 2000)))
        self.window.set_clickthrough(False)


    def _go_running_intermittent(self) -> None:
        intermittent_gif = self.config["actions"].get("running_intermittent", "间歇绘制.gif")
        codex_active = len(self.watcher.active_turns) > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        n = len(self.watcher.active_turns)
        tip = f"{tool} 工作中（{n} 个任务）" if n else f"{tool} 工作中"
        self.window.play(intermittent_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(False)


    def _go_review(self) -> None:
        tip = f"任务完成，等待查看（{len(self.pending_reviews)}）"
        self.window.play(
            self.config["actions"].get("complete", "得意.gif"),
            loops=None, status_text=tip,
            min_play_ms=int(self.config.get("min_play_ms", 2000)),
        )
        self.window.set_clickthrough(False)


    def _go_hidden(self) -> None:
        self.window.hide()


    def _unread_is_meaningful(self, now: float) -> bool:
        """Ignore unread counts during a short grace period after startup,
        so stale entries in codex-global-state.json don't cause a false
        'ready' alert on launch."""
        grace = float(self.config.get("startup_grace_seconds", 3.0))
        return (now - self._started_at) >= grace

    def _update_reviews(self, unread: set[str]) -> None:
        now = time.monotonic()
        settle_seconds = max(
            1.8,
            float(self.config.get("unread_settle_ms", 2500)) / 1000,
        )
        completed: list[str] = []
        for thread_id, state in self.pending_reviews.items():
            if thread_id in unread:
                state["seen_unread"] = True
                continue
            if bool(state["seen_unread"]) or now - float(state["completed_at"]) >= settle_seconds:
                completed.append(thread_id)
        for thread_id in completed:
            self.pending_reviews.pop(thread_id, None)
            LOGGER.info("task result read: thread=%s", thread_id)

    def _poll(self) -> None:
        if not self.running:
            return
        try:
            before, after, events, last_activity = self.watcher.poll()
            unread = self.unread_watcher.poll()

            # Claude Code session monitoring
            claude_busy, claude_last_activity, claude_completions = \
                self.claude_watcher.poll()

            # Feed Codex terminal events into pending_reviews
            for event in events:
                if event["type"] in TERMINAL_EVENT_TYPES and event["thread_id"]:
                    self.pending_reviews[event["thread_id"]] = {
                        "completed_at": time.monotonic(),
                        "seen_unread": event["thread_id"] in unread,
                    }

            # Feed Claude Code busy→idle transitions into the same pipeline.
            # Claude Code has no "unread" concept, so completions always
            # auto-resolve after settle_seconds (default ~2.5 s).
            for comp in claude_completions:
                if comp.get("thread_id"):
                    self.pending_reviews[comp["thread_id"]] = {
                        "completed_at": time.monotonic(),
                        "seen_unread": False,
                    }

            self._update_reviews(unread)

            # ── Determine the correct display mode ──
            display_cfg = self.config.get("display", {})
            persistent = display_cfg.get("persistent", True)
            auto_hide_complete = display_cfg.get("auto_hide_after_complete", False)
            thresholds = self.config.get("activity_thresholds", {})
            think_timeout = float(thresholds.get("thinking_timeout_seconds", 10.0))
            intermittent_timeout = float(thresholds.get("intermittent_timeout_seconds", 3.0))
            now = time.monotonic()

            # Merge Codex + Claude Code into a unified active/idle signal.
            # Codex provides per-turn activity via its JSONL stream; Claude
            # Code provides a binary busy/idle via its session JSON file.
            codex_active = after > 0
            effective_active = codex_active or claude_busy
            effective_last_activity = last_activity
            if claude_last_activity > effective_last_activity:
                effective_last_activity = claude_last_activity

            # Compute gap since last activity (from either tool)
            gap = (now - effective_last_activity
                   if effective_last_activity > 0 else float("inf"))

            # Determine target mode
            if effective_active:
                if effective_last_activity == 0.0 or gap > think_timeout:
                    target = "thinking"
                elif gap > intermittent_timeout:
                    target = "running_intermittent"
                else:
                    target = "running"
            elif self.pending_reviews:
                target = "review"
            elif unread and self._unread_is_meaningful(now):
                target = "ready"
            elif persistent:
                target = "idle"
            else:
                target = "hidden"

            # Review → idle/hidden transition when reviews clear
            if self.display_mode == "review" and not self.pending_reviews:
                if auto_hide_complete and not persistent:
                    target = "hidden"
                elif unread and self._unread_is_meaningful(now):
                    target = "ready"
                else:
                    target = "idle"

            # Hidden → visible transition when something happens
            if self.display_mode == "hidden" and target != "hidden":
                self._transition_to(target)
            elif self.display_mode != target:
                self._transition_to(target)
            else:
                # Same mode — refresh status text
                self._refresh_status(target, after, unread,
                                     claude_busy=claude_busy)

        except Exception:
            LOGGER.exception("poll failed")
        self._schedule_poll()

    def _refresh_status(self, mode: str, active_count: int, unread: set[str],
                        *, claude_busy: bool = False) -> None:
        """Update status text without changing animation."""
        tool = self._tool_label(active_count > 0, claude_busy)
        if mode in ("running", "running_intermittent"):
            tip = f"{tool} 工作中（{active_count} 个任务）" if active_count else f"{tool} 工作中"
        elif mode == "thinking":
            tip = f"{tool} 思考中（{active_count} 个任务）" if active_count else f"{tool} 思考中"
        elif mode == "review":
            tip = f"任务完成，等待查看（{len(self.pending_reviews)}）"
        elif mode == "ready":
            tip = f"有未读消息（{len(unread)} 个会话）"
        elif mode == "idle":
            tip = "等待 AI 任务"
        else:
            return
        self.window.set_status_text(tip)

    @staticmethod
    def _tool_label(codex_active: bool, claude_busy: bool) -> str:
        """Return a human-readable label for which AI tool(s) are active."""
        if codex_active and claude_busy:
            return "Codex + Claude Code"
        if claude_busy:
            return "Claude Code"
        return "Codex"


    def _schedule_poll(self) -> None:
        interval = max(100, int(self.config.get("poll_interval_ms", 350)))
        self.window.root.after(interval, self._poll)

    def stop(self) -> None:
        self.running = False
        self.window.animation_token += 1
        self.window.root.destroy()


def inspect_assets(config: dict) -> dict:
    asset_dir = expand_path(config["asset_dir"])
    coordinate_file = expand_path(config["coordinate_file"])
    coordinates = load_json(coordinate_file, fallback=_DEFAULT_COORDINATES)
    report = {
        "ok": True,
        "asset_dir": str(asset_dir),
        "coordinate_file": str(coordinate_file),
        "actions": {},
        "errors": [],
    }
    for state, gif_name in config["actions"].items():
        path = asset_dir / gif_name
        try:
            with Image.open(path) as gif:
                frames = getattr(gif, "n_frames", 1)
                delays = []
                for index in range(frames):
                    gif.seek(index)
                    delays.append(int(gif.info.get("duration", 30)))
                report["actions"][state] = {
                    "file": str(path),
                    "frames": frames,
                    "size": list(gif.size),
                    "duration_ms": sum(delays),
                    "average_fps": round(1000 * frames / max(1, sum(delays)), 2),
                    "offset": coordinates.get(gif_name, {"x": 0, "y": 0}),
                }
        except OSError as error:
            report["ok"] = False
            report["errors"].append(f"{path}: {error}")
    return report


def current_status(config: dict) -> dict:
    watcher = CodexSessionWatcher(
        sessions_dir=expand_path(
            config.get("codex_sessions_dir", r"%USERPROFILE%\.codex\sessions")
        ),
        recent_days=int(config.get("recent_session_days", 2)),
    )
    active = watcher.initialize()
    return {
        "ok": True,
        "active_task_count": len(active),
        "active_turn_ids": sorted(active),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="蕾米 Codex 外置任务状态桌宠")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--demo", action="store_true")
    return parser.parse_args()


def main() -> int:
    # Catch-all exception hook to ensure NO error is silently lost
    def _log_unhandled(exc_type, exc_value, exc_tb):
        LOGGER.critical("UNHANDLED EXCEPTION", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _log_unhandled

    args = parse_args()

    # Auto-generate config files from embedded defaults on first run
    init_default_files()

    config = load_json(CONFIG_PATH, fallback=_DEFAULT_CONFIG)
    if not config:
        config = dict(_DEFAULT_CONFIG)
    if args.self_test:
        report = {
            "assets": inspect_assets(config),
            "codex": current_status(config),
        }
        report["ok"] = bool(report["assets"]["ok"] and report["codex"]["ok"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if args.status:
        print(json.dumps(current_status(config), ensure_ascii=False, indent=2))
        return 0

    # Single-instance check via PID file (stale locks auto-cleaned)
    if not check_single_instance():
        try:
            import tkinter.messagebox as _mb
            _r = tk.Tk()
            _r.withdraw()
            _mb.showinfo(
                "蕾米 Codex 助手",
                "蕾米 Codex 助手已在运行中。\n\n"
                "如果桌宠不可见，请打开任务管理器\n"
                "结束 pythonw.exe 进程后重试。",
            )
            _r.destroy()
        except Exception:
            pass
        LOGGER.info("another instance is already running -- exiting")
        return 0
    try:
        try:
            BridgeApp(config, demo=args.demo).start()
        except Exception:
            LOGGER.exception("fatal error in BridgeApp")
            return 1
    finally:
        release_single_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
