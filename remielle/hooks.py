from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import uuid

from .config import APP_DIR, HOOK_EVENT_DIR, LOGGER


_HOOK_SCRIPT = APP_DIR / "remielle_hook.py"
_HOOK_MARKER = "REMIELLE_CODEX_BRIDGE_EVENT"


def _python_console_executable() -> Path:
    """Return a Python executable suitable for a Codex command hook."""
    current = Path(sys.executable)
    if current.name.lower() == "pythonw.exe":
        console = current.with_name("python.exe")
        if console.exists():
            return console
    return current


def emit_hook_event(kind: str, payload: dict, event_dir: Path = HOOK_EVENT_DIR) -> Path:
    """Atomically enqueue one small lifecycle event from a Codex hook."""
    event_dir.mkdir(parents=True, exist_ok=True)
    now_ns = time.time_ns()
    event = {
        "kind": kind,
        "thread_id": str(payload.get("session_id") or ""),
        "turn_id": str(payload.get("turn_id") or ""),
        "hook_event_name": str(payload.get("hook_event_name") or ""),
        "cwd": str(payload.get("cwd") or ""),
        "created_at": time.time(),
        "source": "codex-hook",
    }
    stem = f"{now_ns}-{os.getpid()}-{uuid.uuid4().hex}"
    temporary = event_dir / f".{stem}.tmp"
    target = event_dir / f"{stem}.json"
    temporary.write_text(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


class HookEventWatcher:
    """Consumes atomic event files written by ``remielle_hook.py``."""

    def __init__(self, event_dir: Path = HOOK_EVENT_DIR,
                 max_age_hours: float = 24.0) -> None:
        self.event_dir = event_dir
        self.max_age_hours = max_age_hours

    def poll(self) -> list[dict]:
        try:
            self.event_dir.mkdir(parents=True, exist_ok=True)
            paths = sorted(self.event_dir.glob("*.json"))
        except OSError:
            return []

        events: list[dict] = []
        cutoff = time.time() - self.max_age_hours * 3600
        for path in paths:
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if float(event.get("created_at") or 0) < cutoff:
                continue
            if event.get("kind") in {"start", "complete"}:
                events.append(event)
        return events


def _hooks_path() -> Path:
    codex_home = os.getenv("CODEX_HOME")
    return Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"


def user_hooks_path() -> Path:
    return _hooks_path() / "hooks.json"


def _hook_command(kind: str) -> str:
    if getattr(sys, "frozen", False):
        helper = APP_DIR / "RemielleHook.exe"
        return f'"{helper}" {kind} {_HOOK_MARKER}'
    python = _python_console_executable()
    return f'"{python}" "{_HOOK_SCRIPT}" {kind} {_HOOK_MARKER}'


def _is_ours(group: dict) -> bool:
    for handler in group.get("hooks") or []:
        command = str(handler.get("commandWindows") or handler.get("command") or "")
        if _HOOK_MARKER in command:
            return True
    return False


def hooks_installed(path: Path | None = None) -> bool:
    path = path or user_hooks_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = document.get("hooks") or {}
    return any(_is_ours(group) for event in ("UserPromptSubmit", "Stop")
               for group in hooks.get(event, []))


def install_hooks(path: Path | None = None) -> Path:
    """Merge bridge hooks into the user hook file without replacing others."""
    path = path or user_hooks_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict):
            document = {}
    except FileNotFoundError:
        document = {}
    except json.JSONDecodeError as error:
        raise ValueError(f"现有 hooks.json 不是有效 JSON：{error}") from error

    hooks = document.setdefault("hooks", {})
    for event_name, kind, message in (
        ("UserPromptSubmit", "start", "蕾米开始记录任务"),
        ("Stop", "complete", "蕾米记录任务完成"),
    ):
        groups = hooks.setdefault(event_name, [])
        groups[:] = [group for group in groups if not _is_ours(group)]
        command = _hook_command(kind)
        groups.append({
            "hooks": [{
                "type": "command",
                "command": command,
                "commandWindows": command,
                "timeout": 3,
                "statusMessage": message,
            }],
        })

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.remielle.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    LOGGER.warning("Codex hooks installed: %s", path)
    return path


def uninstall_hooks(path: Path | None = None) -> Path:
    """Remove only hook groups owned by this bridge."""
    path = path or user_hooks_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return path
    except json.JSONDecodeError as error:
        raise ValueError(f"现有 hooks.json 不是有效 JSON：{error}") from error

    hooks = document.get("hooks") or {}
    for event_name in ("UserPromptSubmit", "Stop"):
        groups = hooks.get(event_name)
        if isinstance(groups, list):
            hooks[event_name] = [group for group in groups if not _is_ours(group)]
            if not hooks[event_name]:
                hooks.pop(event_name, None)

    temporary = path.with_suffix(".json.remielle.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    LOGGER.warning("Codex hooks removed: %s", path)
    return path
