from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import time

APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
CONFIG_PATH = APP_DIR / "config.json"
_LOCAL_APPDATA = Path(os.getenv("LOCALAPPDATA", str(APP_DIR)))
DATA_DIR = _LOCAL_APPDATA / "RemielleCodexBridge"
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = APP_DIR
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "remielle-bridge.log"
HOOK_EVENT_DIR = DATA_DIR / "hook-events"
LEGACY_SETTINGS_PATH = APP_DIR / "settings.json"
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
    "claude_projects_dir": "%USERPROFILE%/.claude/projects",
    "claude_token_scan_interval_seconds": 3.0,
    "actions": {
        "startup": "待机.gif",
        "idle": "待机.gif",
        "thinking": "思考.gif",
        "ready": "拿笔待机.gif",
        "running": "连续绘制.gif",
        "running_intermittent": "间歇绘制.gif",
        "complete": "得意.gif",
        "failed": "思考.gif",
        "cancelled": "拿笔待机.gif",
        "drag": "期待.gif",
    },
    "display": {
        "persistent": False,
        "auto_hide_after_complete": True,
        "show_unrelated_unread": False,
        "clickthrough_on_idle": False,
        "clickthrough_on_startup": True,
    },
    "indicator": {
        "enabled": True,
        "position": "right",
        "orientation": "auto",
    },
    "activity_thresholds": {
        "thinking_timeout_seconds": 10.0,
        "intermittent_timeout_seconds": 3.0,
    },
    "rendering": {
        "alpha_threshold": 96,
        "scale_min": 0.2,
        "scale_max": 3.0,
        "scale_step": 0.05,
        "visibility_margin_px": 40,
        "default_position_offset_x": 40,
        "default_position_offset_y": 80,
    },
    "poll_interval_ms": 500,
    "unread_settle_ms": 2500,
    "review_focus_ack_delay_ms": 900,
    "hook_status_cache_seconds": 10.0,
    "unread_stale_hours": 0,
    "recent_session_days": 2,
    "default_scale": 0.75,
    "topmost": True,
    "startup_delay_seconds": 0,
    "min_play_ms": 2000,
    "startup_grace_seconds": 3.0,
    "claude_scan_interval_seconds": 1.0,
    "discovery_interval_seconds": 2.0,
    "session_file_cache_ttl_seconds": 1.0,
    "frame_cache_max": 2,
    "hook_queue_max_age_hours": 24,
    "ui": {
        "menu_style": "panel",
        "accent": "#a84664",
        "accent_soft": "#f7e6eb",
        "background": "#fffaf8",
        "surface": "#f8f0f2",
        "surface_hover": "#f1dfe4",
        "border": "#eadde1",
        "indicator_glass": "#d8c7cd",
        "indicator_border": "#b89fa9",
        "indicator_text": "#30262a",
        "indicator_muted": "#66535b",
        "indicator_alpha": 0.72,
        "text": "#3c2b32",
        "muted": "#8d7780",
        "danger": "#b85c6d",
    },
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
    if not SETTINGS_PATH.exists() and LEGACY_SETTINGS_PATH.exists():
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(LEGACY_SETTINGS_PATH, SETTINGS_PATH)
        except OSError:
            pass


def load_json(path: Path, fallback: dict | None = None) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(fallback or {})


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*; nested dicts are merged,
    not replaced.  *base* is mutated and returned."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path) -> dict:
    """Load config JSON, filling missing keys from ``_DEFAULT_CONFIG``.

    The on-disk file is never rewritten — the merge only exists in memory,
    so user config stays lean while the app always sees a complete config.
    """
    from copy import deepcopy
    merged = deepcopy(_DEFAULT_CONFIG)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        user = {}
    _deep_merge(merged, user)
    return merged


def expand_path(value: str, base: Path = APP_DIR) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    return path if path.is_absolute() else base / path


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("remielle-codex-bridge")
    logger.setLevel(logging.WARNING)
    if not logger.handlers:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                LOG_PATH,
                maxBytes=300_000,
                backupCount=1,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
    return logger


LOGGER = configure_logging()


# ── Single-instance via PID file (self-cleaning) ──────────────

_PID_PATH = DATA_DIR / "remielle-bridge.pid"
_INSTANCE_MUTEX = None


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
    global _INSTANCE_MUTEX
    my_pid = str(os.getpid())
    if sys.platform == "win32":
        import ctypes as _c
        kernel32 = _c.windll.kernel32
        kernel32.CreateMutexW.restype = _c.wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, "Local\\RemielleCodexBridge")
        if handle:
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            _INSTANCE_MUTEX = handle
            try:
                _PID_PATH.write_text(my_pid, encoding="utf-8")
            except OSError:
                pass
            return True

    # Portable fallback for non-Windows development/test environments.
    try:
        fd = os.open(_PID_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(my_pid)
        return True
    except FileExistsError:
        pass
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
    global _INSTANCE_MUTEX
    if _INSTANCE_MUTEX and sys.platform == "win32":
        try:
            import ctypes as _c
            _c.windll.kernel32.ReleaseMutex(_INSTANCE_MUTEX)
            _c.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
        except Exception:
            pass
        _INSTANCE_MUTEX = None
    try:
        _PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass
