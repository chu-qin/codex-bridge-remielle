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
from .watchers import (CodexSessionWatcher, CodexUnreadWatcher,
                       ClaudeSessionWatcher, TokenUsage)
from .hooks import HookEventWatcher, hooks_installed
from .win32_helpers import _is_codex_foreground
from .window import RemielleWindow

class BridgeApp:
    def __init__(self, config: dict, demo: bool = False) -> None:
        self.config = config
        sessions_dir = expand_path(config["codex_sessions_dir"])
        self.watcher = CodexSessionWatcher(
            sessions_dir=sessions_dir,
            recent_days=int(config["recent_session_days"]),
            discovery_interval_seconds=float(config["discovery_interval_seconds"]),
            file_cache_ttl_seconds=float(config["session_file_cache_ttl_seconds"]),
        )
        self.unread_watcher = CodexUnreadWatcher(
            expand_path(config["codex_global_state_path"]),
            sessions_dir=sessions_dir,
            stale_hours=float(config["unread_stale_hours"]),
        )
        self.claude_watcher = ClaudeSessionWatcher(
            expand_path(config["claude_sessions_dir"]),
            scan_interval_seconds=float(config["claude_scan_interval_seconds"]),
            projects_dir=expand_path(config["claude_projects_dir"]),
            recent_days=int(config["recent_session_days"]),
            token_scan_interval_seconds=float(
                config["claude_token_scan_interval_seconds"]
            ),
        )
        self.hook_watcher = HookEventWatcher(
            max_age_hours=float(config["hook_queue_max_age_hours"]),
        )
        self.hook_active_turns: set[str] = set()
        self.hook_finished_turns: set[str] = set()
        self.window = RemielleWindow(
            config,
            self.stop,
            on_acknowledge=self.acknowledge_reviews,
        )
        self.running = True
        self.demo = demo
        self.pending_reviews: dict[str, dict[str, float | bool | str]] = {}
        self.display_mode = "hidden"
        self._started_at = 0.0  # set in start() for grace-period logic
        self._codex_was_foreground = False
        self._hooks_enabled_cache = hooks_installed()
        self._hooks_status_checked_at = time.monotonic()

    def start(self) -> None:
        self._started_at = time.monotonic()
        if self.demo:
            self._demo_all()
            self.window.root.after(
                250,
                lambda: self.window.show_menu_at(
                    self.window.root.winfo_screenwidth() - 24, 80,
                    source="tray",
                ),
            )
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

        def _demo_step(mode: str, tokens: int = 0) -> None:
            self._transition_to(mode)
            indicator_status = {
                "running": "工作中",
                "running_intermittent": "间歇工作中",
                "thinking": "思考中",
                "review": "完成",
            }.get(mode, self._MODE_LABELS.get(mode, mode))
            self.window.set_status_info({
                "mode": self._MODE_LABELS.get(mode, mode),
                "indicator_status": indicator_status,
                "active": "1" if mode in {
                    "thinking", "running", "running_intermittent"
                } else "",
                "unread": "",
                "claude": "空闲",
                "reviews": "1" if mode == "review" else "",
                "hooks": "演示",
                "token_total": str(tokens),
                "token_input": str(round(tokens * 0.72)),
                "token_cached": str(round(tokens * 0.18)),
                "token_output": str(round(tokens * 0.28)),
            })

        def _seq():
            _demo_step("startup")
            self.window.root.after(1500, lambda: _demo_step("idle"))
            self.window.root.after(3000, lambda: _demo_step("ready", 320))
            self.window.root.after(5000, lambda: _demo_step("thinking", 860))
            self.window.root.after(8000, lambda: _demo_step("running", 1840))
            self.window.root.after(
                16000, lambda: _demo_step("running_intermittent", 4720)
            )
            self.window.root.after(24000, lambda: _demo_step("review", 6380))
            self.window.root.after(30000, lambda: _demo_step("idle"))
            self.window.root.after(32000, _cleanup_demo)

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
        n = self._codex_active_count()
        codex_active = n > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
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
        n = self._codex_active_count()
        codex_active = n > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        tip = f"{tool} 工作中（{n} 个任务）" if n else f"{tool} 工作中"
        self.window.play(running_gif, loops=None, status_text=tip,
                         min_play_ms=int(self.config["min_play_ms"]))
        self.window.set_clickthrough(False)


    def _go_running_intermittent(self) -> None:
        intermittent_gif = self.config["actions"]["running_intermittent"]
        n = self._codex_active_count()
        codex_active = n > 0
        tool = self._tool_label(codex_active, self.claude_watcher.is_busy)
        tip = f"{tool} 工作中（{n} 个任务）" if n else f"{tool} 工作中"
        self.window.play(intermittent_gif, loops=None, status_text=tip)
        self.window.set_clickthrough(False)


    def _go_review(self) -> None:
        outcomes = {str(item.get("outcome") or "complete")
                    for item in self.pending_reviews.values()}
        if "failed" in outcomes:
            action = self.config["actions"]["failed"]
            tip = f"任务执行失败，等待确认（{len(self.pending_reviews)}）"
        elif outcomes == {"cancelled"}:
            action = self.config["actions"]["cancelled"]
            tip = f"任务已取消，等待确认（{len(self.pending_reviews)}）"
        else:
            action = self.config["actions"]["complete"]
            tip = f"任务完成，等待查看（{len(self.pending_reviews)}）"
        self.window.play(
            action,
            loops=None, status_text=tip,
            min_play_ms=int(self.config["min_play_ms"]),
        )
        self.window.set_clickthrough(False)


    def _go_hidden(self) -> None:
        self.window.hide()

    def _codex_active_count(self) -> int:
        fallback = self.watcher.active_turns - self.hook_finished_turns
        return len(fallback | self.hook_active_turns)


    def _unread_is_meaningful(self, now: float) -> bool:
        """Ignore unread counts during a short grace period after startup,
        so stale entries in codex-global-state.json don't cause a false
        'ready' alert on launch."""
        grace = float(self.config["startup_grace_seconds"])
        return (now - self._started_at) >= grace

    def _update_reviews(
        self,
        unread: set[str],
        *,
        codex_foreground: bool = False,
        now: float | None = None,
    ) -> None:
        """Clear results after a read transition or a guarded focus fallback.

        The unread-ID transition remains the precise, preferred signal.  Some
        Codex Desktop builds retain stale unread IDs or never mark the task
        that was already open when it completed.  For those cases we accept a
        stable foreground transition only when exactly one Codex result is
        pending.  The extra Win32 query is performed only in review mode.
        """
        now = time.monotonic() if now is None else now
        settle_seconds = max(
            0.0,
            float(getattr(self, "config", {}).get("unread_settle_ms", 2500))
            / 1000.0,
        )
        focus_delay = max(
            0.0,
            float(
                getattr(self, "config", {}).get(
                    "review_focus_ack_delay_ms", 900
                )
            ) / 1000.0,
        )
        focus_entered = (
            codex_foreground
            and not bool(getattr(self, "_codex_was_foreground", False))
        )
        self._codex_was_foreground = codex_foreground
        allow_focus_fallback = (
            len(self.pending_reviews) == 1
            and str(next(iter(self.pending_reviews.values())).get("source") or "codex")
            == "codex"
        )
        completed: list[tuple[str, str]] = []
        for thread_id, state in self.pending_reviews.items():
            completed_at = float(state.get("completed_at") or now)
            elapsed = max(0.0, now - completed_at)
            is_unread = thread_id in unread
            if is_unread:
                state["seen_unread"] = True

            # Do not treat the completion poll itself as a later focus event.
            # A genuine focus transition must occur after the result exists.
            if (
                allow_focus_fallback
                and focus_entered
                and elapsed >= 0.1
            ):
                state["codex_focus_at"] = now

            if not is_unread and bool(state.get("seen_unread")):
                completed.append((thread_id, "unread-to-read"))
                continue

            # If the task was already visible when it completed, Codex may
            # never create a blue dot.  Only accept that inference when the ID
            # is absent and the short unread-settle window has passed.
            if (
                not is_unread
                and not bool(state.get("seen_unread"))
                and bool(state.get("codex_foreground_at_completion"))
                and elapsed >= settle_seconds
            ):
                completed.append((thread_id, "already-visible"))
                continue

            # Stale unread-ID fallback: entering Codex after completion is
            # treated as a view only for one pending Codex result and only
            # after focus remains stable for a short delay.
            focus_at = float(state.get("codex_focus_at") or 0.0)
            if (
                allow_focus_fallback
                and codex_foreground
                and focus_at > completed_at
                and elapsed >= settle_seconds
                and now - focus_at >= focus_delay
            ):
                completed.append((thread_id, "codex-focus"))

        for thread_id, reason in completed:
            self.pending_reviews.pop(thread_id, None)
            LOGGER.info(
                "task result read: thread=%s reason=%s", thread_id, reason
            )

    @staticmethod
    def _outcome_for_event(event_type: str) -> str:
        if event_type == "task_complete":
            return "complete"
        if event_type == "task_failed":
            return "failed"
        return "cancelled"

    def _record_review(self, *, thread_id: str, turn_id: str = "",
                       outcome: str = "complete", unread: set[str],
                       codex_foreground: bool = False,
                       source: str = "codex") -> None:
        if not thread_id:
            return
        current = self.pending_reviews.get(thread_id)
        if current and str(current.get("turn_id") or "") == turn_id:
            if outcome != "complete":
                current["outcome"] = outcome
            current["seen_unread"] = bool(current.get("seen_unread")) or thread_id in unread
            current["codex_foreground_at_completion"] = (
                bool(current.get("codex_foreground_at_completion"))
                or codex_foreground
            )
            return
        self.pending_reviews[thread_id] = {
            "completed_at": time.monotonic(),
            "seen_unread": thread_id in unread,
            "outcome": outcome,
            "turn_id": turn_id,
            "source": source,
            "codex_foreground_at_completion": codex_foreground,
            "codex_focus_at": 0.0,
        }

    def _hooks_enabled(self, now: float) -> bool:
        """Return Hook status with a short cache instead of reading JSON 2×/s."""
        interval = max(
            1.0, float(self.config.get("hook_status_cache_seconds", 10.0))
        )
        if now - self._hooks_status_checked_at >= interval:
            self._hooks_enabled_cache = hooks_installed()
            self._hooks_status_checked_at = now
        return self._hooks_enabled_cache

    def acknowledge_reviews(self) -> None:
        """Explicit fallback when the Codex read-state is unavailable."""
        if not self.pending_reviews:
            return
        count = len(self.pending_reviews)
        self.pending_reviews.clear()
        LOGGER.warning("task results acknowledged manually: count=%d", count)
        if self.display_mode == "review":
            display = self.config["display"]
            self._transition_to(
                "idle" if display["persistent"] and not display["auto_hide_after_complete"]
                else "hidden"
            )

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
            if auto_hide_complete or not persistent:
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
            hook_events = self.hook_watcher.poll()

            # Claude Code session monitoring
            claude_busy, claude_last_activity, claude_completions = \
                self.claude_watcher.poll()

            # Claude Code token usage: live delta while busy, frozen delta
            # while a Claude result awaits review.  Claude reviews carry no
            # Codex ``turn_id``, so ``review_turns`` never includes them —
            # this explicit source check covers the frozen delta instead.
            claude_review_pending = any(
                str(item.get("source") or "") == "claude"
                for item in self.pending_reviews.values()
            )
            if claude_busy:
                claude_usage = self.claude_watcher.active_usage
            elif claude_review_pending:
                claude_usage = self.claude_watcher.review_usage
            else:
                claude_usage = TokenUsage()

            has_codex_completion = any(
                event.get("kind") == "complete" for event in hook_events
            ) or any(
                event.get("type") in TERMINAL_EVENT_TYPES for event in events
            )
            needs_codex_focus = bool(self.pending_reviews) or has_codex_completion
            codex_foreground = (
                _is_codex_foreground() if needs_codex_focus else False
            )

            # Official Codex lifecycle hooks are the primary signal.  JSONL
            # remains active as a fallback for clients where hooks are absent.
            for hook_event in hook_events:
                turn_id = str(hook_event.get("turn_id") or "")
                thread_id = str(hook_event.get("thread_id") or "")
                if hook_event.get("kind") == "start" and turn_id:
                    self.hook_finished_turns.discard(turn_id)
                    self.hook_active_turns.add(turn_id)
                elif hook_event.get("kind") == "complete":
                    self.hook_active_turns.discard(turn_id)
                    if turn_id:
                        self.hook_finished_turns.add(turn_id)
                    self._record_review(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        outcome="complete",
                        unread=unread,
                        codex_foreground=codex_foreground,
                    )

            # Feed Codex terminal events into pending_reviews
            for event in events:
                if event["type"] in TERMINAL_EVENT_TYPES and event["thread_id"]:
                    turn_id = event.get("turn_id", "")
                    self.hook_active_turns.discard(turn_id)
                    self.hook_finished_turns.discard(turn_id)
                    self._record_review(
                        thread_id=event["thread_id"],
                        turn_id=turn_id,
                        outcome=self._outcome_for_event(event["type"]),
                        unread=unread,
                        codex_foreground=codex_foreground,
                    )

            # Feed Claude Code busy→idle transitions into the same pipeline.
            for comp in claude_completions:
                if comp.get("thread_id"):
                    self._record_review(
                        thread_id=comp["thread_id"],
                        outcome="complete",
                        unread=unread,
                        source="claude",
                    )

            now = time.monotonic()
            self._update_reviews(
                unread,
                codex_foreground=codex_foreground,
                now=now,
            )

            fallback_active = self.watcher.active_turns - self.hook_finished_turns
            effective_turns = fallback_active | self.hook_active_turns
            effective_active_count = len(effective_turns)
            target = self._determine_target(
                codex_active=effective_active_count > 0,
                claude_busy=claude_busy,
                codex_last_activity=last_activity,
                now=now,
                has_pending_reviews=bool(self.pending_reviews),
                has_unread=(
                    bool(unread)
                    and bool(self.config["display"]["show_unrelated_unread"])
                    and self._unread_is_meaningful(now)
                ),
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
                self._refresh_status(target, effective_active_count, unread,
                                     claude_busy=claude_busy)

            review_turns = {
                str(item.get("turn_id") or "")
                for item in self.pending_reviews.values()
                if item.get("turn_id")
            }
            token_usage = (
                self.watcher.usage_for_turns(effective_turns | review_turns)
                + claude_usage
            )
            indicator_status = self._MODE_LABELS.get(
                self.display_mode, self.display_mode
            )
            if self.display_mode == "review":
                outcomes = {
                    str(item.get("outcome") or "complete")
                    for item in self.pending_reviews.values()
                }
                if "failed" in outcomes:
                    indicator_status = "失败"
                elif outcomes == {"cancelled"}:
                    indicator_status = "取消"
                else:
                    indicator_status = "完成"

            # Push a snapshot of bridge state to the window so the
            # right-click menu always shows current info.
            self.window.set_status_info({
                "mode": self._MODE_LABELS.get(self.display_mode, self.display_mode),
                "indicator_status": indicator_status,
                "active": str(effective_active_count) if effective_active_count else "",
                "unread": str(len(unread)) if unread else "",
                "claude": "忙碌" if claude_busy else "空闲",
                "reviews": str(len(self.pending_reviews)) if self.pending_reviews else "",
                "hooks": "已启用" if self._hooks_enabled(now) else "兼容模式",
                "token_total": str(token_usage.total_tokens),
                "token_input": str(token_usage.input_tokens),
                "token_cached": str(token_usage.cached_input_tokens),
                "token_output": str(token_usage.output_tokens),
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
            outcomes = {str(item.get("outcome") or "complete")
                        for item in self.pending_reviews.values()}
            if "failed" in outcomes:
                tip = f"任务执行失败，等待确认（{len(self.pending_reviews)}）"
            elif outcomes == {"cancelled"}:
                tip = f"任务已取消，等待确认（{len(self.pending_reviews)}）"
            else:
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
        file_cache_ttl_seconds=float(config["session_file_cache_ttl_seconds"]),
    )
    active = watcher.initialize()
    return {
        "ok": True,
        "active_task_count": len(active),
        "active_turn_ids": sorted(active),
        "hooks_installed": hooks_installed(),
    }
