from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from PIL import Image

from .config import (APP_DIR, CONFIG_PATH, _DEFAULT_COORDINATES,
                     TERMINAL_EVENT_TYPES, LOGGER,
                     expand_path, load_json)
from .watchers import CodexSessionWatcher, CodexUnreadWatcher, ClaudeSessionWatcher
from .window import RemielleWindow

class BridgeApp:
    def __init__(self, config: dict, demo: bool = False) -> None:
        self.config = config
        sessions_dir = expand_path(config["codex_sessions_dir"])
        self.watcher = CodexSessionWatcher(
            sessions_dir=sessions_dir,
            recent_days=int(config["recent_session_days"]),
            discovery_interval_seconds=float(config["discovery_interval_seconds"]),
        )
        self.unread_watcher = CodexUnreadWatcher(
            expand_path(config["codex_global_state_path"]),
            sessions_dir=sessions_dir,
        )
        self.claude_watcher = ClaudeSessionWatcher(
            expand_path(config["claude_sessions_dir"]),
            scan_interval_seconds=float(config["claude_scan_interval_seconds"]),
        )
        self.window = RemielleWindow(config, self.stop)
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
                    int(self.config["startup_delay_seconds"]) * 1000,
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
        display_cfg = self.config["display"]
        saved_persistent = display_cfg["persistent"]
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

    _MODE_LABELS: dict[str, str] = {
        "hidden": "已隐藏",
        "startup": "启动中",
        "idle": "待机中",
        "thinking": "思考中",
        "running": "工作中",
        "running_intermittent": "间歇工作中",
        "review": "任务完成待查看",
        "ready": "有未读消息",
    }

    def _transition_to(self, mode: str) -> None:
        """Central dispatcher: change display mode, logging the transition."""
        if mode == self.display_mode:
            return
        old = self.display_mode
        self.display_mode = mode
        LOGGER.info("display transition: %s → %s", old, mode)
        # Update tray tooltip — only called on actual transitions, not
        # every poll cycle, so NIM_MODIFY is cheap.
        tray = getattr(self.window, "_tray", None)
        if tray:
            label = self._MODE_LABELS.get(mode, mode)
            tray.set_tip(f"蕾米 · {label}")
        getattr(self, f"_go_{mode}")()

    def _go_startup(self) -> None:
        startup_gif = self.config["actions"]["startup"]
        tip = "蕾米 AI 助手已启动 · 等待任务"
        self.window.play(startup_gif, loops=1, status_text=tip,
                         min_play_ms=int(self.config["min_play_ms"]),
                         on_end=self._on_startup_done)
        self.window.set_clickthrough(
            self.config["display"]["clickthrough_on_startup"])


    def _on_startup_done(self) -> None:
        """Called when the startup animation finishes playing."""
        display_cfg = self.config["display"]
        if display_cfg["persistent"]:
            self._transition_to("idle")
        else:
            self._transition_to("hidden")

    def _go_idle(self) -> None:
        idle_gif = self.config["actions"]["idle"]
        tip = "等待 AI 任务"
        self.window.play(idle_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(
            self.config["display"]["clickthrough_on_idle"])


    def _go_thinking(self) -> None:
        thinking_gif = self.config["actions"]["thinking"]
        codex_active = len(self.watcher.active_turns) > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        n = len(self.watcher.active_turns)
        tip = f"{tool} 思考中（{n} 个任务）" if n else f"{tool} 思考中"
        self.window.play(thinking_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(False)


    def _go_ready(self) -> None:
        ready_gif = self.config["actions"]["ready"]
        unread_count = len(self.unread_watcher.unread_thread_ids)
        tip = f"有未读消息（{unread_count} 个会话）"
        self.window.play(ready_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(False)


    def _go_running(self) -> None:
        running_gif = self.config["actions"]["running"]
        codex_active = len(self.watcher.active_turns) > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        n = len(self.watcher.active_turns)
        tip = f"{tool} 工作中（{n} 个任务）" if n else f"{tool} 工作中"
        self.window.play(running_gif, loops=None, status_text=tip,
                         min_play_ms=int(self.config["min_play_ms"]))
        self.window.set_clickthrough(False)


    def _go_running_intermittent(self) -> None:
        intermittent_gif = self.config["actions"]["running_intermittent"]
        codex_active = len(self.watcher.active_turns) > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        n = len(self.watcher.active_turns)
        tip = f"{tool} 工作中（{n} 个任务）" if n else f"{tool} 工作中"
        self.window.play(intermittent_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(False)


    def _go_review(self) -> None:
        tip = f"任务完成，等待查看（{len(self.pending_reviews)}）"
        self.window.play(
            self.config["actions"]["complete"],
            loops=None, status_text=tip,
            min_play_ms=int(self.config["min_play_ms"]),
        )
        self.window.set_clickthrough(False)


    def _go_hidden(self) -> None:
        self.window.hide()


    def _unread_is_meaningful(self, now: float) -> bool:
        """Ignore unread counts during a short grace period after startup,
        so stale entries in codex-global-state.json don't cause a false
        'ready' alert on launch."""
        grace = float(self.config["startup_grace_seconds"])
        return (now - self._started_at) >= grace

    def _update_reviews(self, unread: set[str]) -> None:
        now = time.monotonic()
        settle_seconds = max(
            1.8,
            float(self.config["unread_settle_ms"]) / 1000,
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

    @staticmethod
    def _determine_target(
        *,
        codex_active: bool,
        claude_busy: bool,
        codex_last_activity: float,
        now: float,
        has_pending_reviews: bool,
        has_unread: bool,
        persistent: bool,
        auto_hide_complete: bool,
        think_timeout: float,
        intermittent_timeout: float,
        current_mode: str,
    ) -> str:
        """Pure function: compute the next display mode from watcher signals.

        DESIGN RULES (regression-tested):
        ─────────────────────────────────
        1. Codex provides high-frequency activity signals (JSONL lines every
           ~100 ms).  Gap-based degradation (running → intermittent →
           thinking) is meaningful because the gap reflects real output gaps.

        2. Claude Code provides low-frequency activity signals (session JSON
           file writes every 5-30 s).  When Claude is the **only** active
           tool, gap-based degradation is meaningless — we cannot distinguish
           "streaming tokens" from "reasoning" between file writes.  In that
           case we treat the activity timer as continuously fresh.

        3. When BOTH tools are active, Codex's high-frequency signal drives
           the degradation timeline.  Claude's binary busy/idle only
           contributes to the ``effective_active`` flag.

        4. If a third tool watcher is added, YOU MUST DECLARE its signal
           frequency here and handle it in the ``# <-- SIGNAL FREQUENCY``
           block below.  Do NOT silently merge a low-frequency watcher into
           the gap-based path.
        """
        # ══ Activity merge — each watcher declares its signal frequency ══
        effective_active = codex_active or claude_busy

        if codex_active:
            # High-frequency → gap-based degradation applies
            effective_last_activity = codex_last_activity
        elif claude_busy:
            # Low-frequency → treat as continuously fresh
            # <-- SIGNAL FREQUENCY: new watchers go here
            effective_last_activity = now
        else:
            effective_last_activity = 0.0

        gap = (now - effective_last_activity
               if effective_last_activity > 0 else 0.0)

        # ══ Priority-ordered target selection ══
        if effective_active:
            if gap > think_timeout:
                target = "thinking"
            elif gap > intermittent_timeout:
                target = "running_intermittent"
            else:
                target = "running"
        elif has_pending_reviews:
            target = "review"
        elif has_unread:
            target = "ready"
        elif persistent:
            target = "idle"
        else:
            target = "hidden"

        # ══ Mode-specific exit transitions ══
        if current_mode == "review" and not has_pending_reviews:
            if auto_hide_complete and not persistent:
                target = "hidden"
            elif has_unread:
                target = "ready"
            else:
                target = "idle"

        return target

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
            for comp in claude_completions:
                if comp.get("thread_id"):
                    self.pending_reviews[comp["thread_id"]] = {
                        "completed_at": time.monotonic(),
                        "seen_unread": False,
                    }

            self._update_reviews(unread)

            now = time.monotonic()
            target = self._determine_target(
                codex_active=after > 0,
                claude_busy=claude_busy,
                codex_last_activity=last_activity,
                now=now,
                has_pending_reviews=bool(self.pending_reviews),
                has_unread=bool(unread) and self._unread_is_meaningful(now),
                persistent=self.config["display"]["persistent"],
                auto_hide_complete=self.config["display"]["auto_hide_after_complete"],
                think_timeout=float(self.config["activity_thresholds"]["thinking_timeout_seconds"]),
                intermittent_timeout=float(self.config["activity_thresholds"]["intermittent_timeout_seconds"]),
                current_mode=self.display_mode,
            )

            # Hidden → visible transition when something happens
            if self.display_mode == "hidden" and target != "hidden":
                self._transition_to(target)
            elif self.display_mode != target:
                self._transition_to(target)
            else:
                # Same mode — refresh status text
                self._refresh_status(target, after, unread,
                                     claude_busy=claude_busy)

            # Push a snapshot of bridge state to the window so the
            # right-click menu always shows current info.
            self.window.set_status_info({
                "mode": self._MODE_LABELS.get(self.display_mode, self.display_mode),
                "active": str(after) if after else "",
                "unread": str(len(unread)) if unread else "",
                "claude": "忙碌" if claude_busy else "空闲",
                "reviews": str(len(self.pending_reviews)) if self.pending_reviews else "",
            })

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
        interval = max(100, int(self.config["poll_interval_ms"]))
        self.window.root.after(interval, self._poll)

    def stop(self) -> None:
        """Unified exit — safe to call multiple times.

        All exit paths (tray menu, window menu, future hotkeys) funnel
        through here so cleanup is guaranteed exactly once.
        """
        if not self.running:
            return
        self.running = False
        self.window.animation_token += 1

        tray = getattr(self.window, "_tray", None)
        if tray is not None:
            tray.destroy()
            self.window._tray = None

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
        sessions_dir=expand_path(config["codex_sessions_dir"]),
        recent_days=int(config["recent_session_days"]),
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

    config = load_config(CONFIG_PATH)
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
