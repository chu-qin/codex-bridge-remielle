from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from remielle.app import BridgeApp
from remielle.hooks import (
    HookEventWatcher,
    emit_hook_event,
    hooks_installed,
    install_hooks,
    uninstall_hooks,
)
from remielle.watchers import CodexSessionWatcher, CodexUnreadWatcher
from remielle.window import RemielleWindow, _clamp_window_to_bounds


class TestHookQueue(unittest.TestCase):
    def test_atomic_event_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            event_dir = Path(directory)
            emit_hook_event("start", {
                "session_id": "thread-1",
                "turn_id": "turn-1",
                "hook_event_name": "UserPromptSubmit",
            }, event_dir=event_dir)
            events = HookEventWatcher(event_dir).poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["kind"], "start")
            self.assertEqual(events[0]["thread_id"], "thread-1")
            self.assertEqual(list(event_dir.glob("*.json")), [])

    def test_install_preserves_other_hooks_and_uninstall_is_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            original = {
                "hooks": {
                    "Stop": [{"hooks": [{
                        "type": "command", "command": "other-tool --done",
                    }]}],
                },
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            install_hooks(path)
            self.assertTrue(hooks_installed(path))
            uninstall_hooks(path)
            self.assertFalse(hooks_installed(path))
            document = json.loads(path.read_text(encoding="utf-8"))
            command = document["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertEqual(command, "other-tool --done")


class TestWatcherReliability(unittest.TestCase):
    def test_invalid_global_state_preserves_last_valid_unread(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps({
                "electron-persisted-atom-state": {
                    "unread-thread-ids-by-host-v1": {"local": ["thread-1"]},
                },
            }), encoding="utf-8")
            watcher = CodexUnreadWatcher(state_path, stale_hours=0)
            self.assertEqual(watcher.poll(), {"thread-1"})
            state_path.write_text("{partial", encoding="utf-8")
            self.assertEqual(watcher.poll(), {"thread-1"})

    def test_new_session_file_appears_after_short_cache_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory)
            watcher = CodexSessionWatcher(
                sessions, file_cache_ttl_seconds=0.25,
                discovery_interval_seconds=0.25,
            )
            self.assertEqual(watcher._recent_files(), [])
            path = sessions / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
            path.write_text("", encoding="utf-8")
            time.sleep(0.27)
            self.assertEqual(watcher._recent_files(), [path])

    def test_token_snapshots_are_scoped_to_the_current_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory)
            path = sessions / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
            watcher = CodexSessionWatcher(sessions)

            def feed(payload: dict) -> None:
                watcher._process_line(json.dumps({
                    "type": "event_msg", "payload": payload,
                }).encode("utf-8"), path)

            # The session counter is cumulative, so the turn begins at 100.
            feed({
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 70, "cached_input_tokens": 20,
                    "output_tokens": 30, "reasoning_output_tokens": 5,
                    "total_tokens": 100,
                }},
            })
            feed({"type": "task_started", "turn_id": "turn-1"})
            feed({
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 110, "cached_input_tokens": 35,
                    "output_tokens": 50, "reasoning_output_tokens": 12,
                    "total_tokens": 160,
                }},
            })

            usage = watcher.usage_for_turns({"turn-1"})
            self.assertEqual(usage.input_tokens, 40)
            self.assertEqual(usage.cached_input_tokens, 15)
            self.assertEqual(usage.output_tokens, 20)
            self.assertEqual(usage.reasoning_output_tokens, 7)
            self.assertEqual(usage.total_tokens, 60)

            # Repeated snapshots do not double-count, and completion keeps the
            # counters available while the result waits to be reviewed.
            feed({
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 110, "cached_input_tokens": 35,
                    "output_tokens": 50, "reasoning_output_tokens": 12,
                    "total_tokens": 160,
                }},
            })
            feed({"type": "task_complete", "turn_id": "turn-1"})
            self.assertEqual(
                watcher.usage_for_turns({"turn-1"}).total_tokens, 60
            )

    def test_inactive_sessions_are_polled_less_often(self):
        watcher = CodexSessionWatcher(
            Path("sessions"), discovery_interval_seconds=2.0
        )
        active_path = Path("active.jsonl")
        idle_path = Path("idle.jsonl")
        watcher.offsets = {active_path: 0, idle_path: 0}
        watcher.current_turn_by_path = {active_path: "turn-1"}
        watcher.active_turns = {"turn-1"}
        now = time.monotonic()
        watcher._last_discovery = now
        watcher._last_inactive_poll = now
        reads: list[Path] = []
        watcher._read_new_bytes = lambda path, from_start=False: reads.append(path)

        watcher.poll()
        self.assertEqual(reads, [active_path])

        reads.clear()
        watcher._last_inactive_poll -= 2.1
        watcher.poll()
        self.assertEqual(set(reads), {active_path, idle_path})


class TestReviewLifecycle(unittest.TestCase):
    @staticmethod
    def _app_with_review(*, completed_at: float, seen_unread: bool = False,
                         foreground_at_completion: bool = False) -> BridgeApp:
        app = object.__new__(BridgeApp)
        app.config = {
            "unread_settle_ms": 2500,
            "review_focus_ack_delay_ms": 900,
        }
        app._codex_was_foreground = False
        app.pending_reviews = {
            "thread-1": {
                "completed_at": completed_at,
                "seen_unread": seen_unread,
                "outcome": "complete",
                "turn_id": "turn-1",
                "source": "codex",
                "codex_foreground_at_completion": foreground_at_completion,
                "codex_focus_at": 0.0,
            },
        }
        return app

    def test_unseen_review_never_expires_by_timeout(self):
        app = self._app_with_review(completed_at=time.monotonic() - 3600)
        app._update_reviews(set())
        self.assertIn("thread-1", app.pending_reviews)
        app.pending_reviews["thread-1"]["seen_unread"] = True
        app._update_reviews(set())
        self.assertNotIn("thread-1", app.pending_reviews)

    def test_unread_to_read_transition_clears_immediately(self):
        app = self._app_with_review(completed_at=100.0)
        app._update_reviews({"thread-1"}, now=101.0)
        self.assertIn("thread-1", app.pending_reviews)
        app._update_reviews(set(), now=101.5)
        self.assertNotIn("thread-1", app.pending_reviews)

    def test_already_visible_result_clears_after_settle_window(self):
        app = self._app_with_review(
            completed_at=100.0,
            foreground_at_completion=True,
        )
        app._update_reviews(set(), codex_foreground=True, now=102.0)
        self.assertIn("thread-1", app.pending_reviews)
        app._update_reviews(set(), codex_foreground=True, now=102.6)
        self.assertNotIn("thread-1", app.pending_reviews)

    def test_codex_focus_clears_one_stale_unread_result(self):
        app = self._app_with_review(
            completed_at=100.0,
            seen_unread=True,
        )
        app._update_reviews({"thread-1"}, codex_foreground=False, now=101.0)
        app._update_reviews({"thread-1"}, codex_foreground=True, now=103.0)
        self.assertIn("thread-1", app.pending_reviews)
        app._update_reviews({"thread-1"}, codex_foreground=True, now=104.0)
        self.assertNotIn("thread-1", app.pending_reviews)

    def test_codex_focus_does_not_guess_between_multiple_results(self):
        app = self._app_with_review(
            completed_at=100.0,
            seen_unread=True,
        )
        app.pending_reviews["thread-2"] = {
            **app.pending_reviews["thread-1"],
            "turn_id": "turn-2",
        }
        app._update_reviews(
            {"thread-1", "thread-2"}, codex_foreground=True, now=103.0
        )
        app._update_reviews(
            {"thread-1", "thread-2"}, codex_foreground=True, now=105.0
        )
        self.assertEqual(set(app.pending_reviews), {"thread-1", "thread-2"})

    def test_terminal_outcomes_are_distinct(self):
        self.assertEqual(BridgeApp._outcome_for_event("task_complete"), "complete")
        self.assertEqual(BridgeApp._outcome_for_event("task_failed"), "failed")
        self.assertEqual(BridgeApp._outcome_for_event("task_cancelled"), "cancelled")

    def test_hook_status_uses_short_cache(self):
        app = object.__new__(BridgeApp)
        app.config = {"hook_status_cache_seconds": 10.0}
        app._hooks_enabled_cache = True
        app._hooks_status_checked_at = 100.0
        with patch("remielle.app.hooks_installed", return_value=False) as probe:
            self.assertTrue(app._hooks_enabled(105.0))
            probe.assert_not_called()
            self.assertFalse(app._hooks_enabled(111.0))
            probe.assert_called_once_with()


class TestIndicatorLayout(unittest.TestCase):
    def test_position_selects_orientation_automatically(self):
        window = object.__new__(RemielleWindow)
        expected = {
            "top": "horizontal",
            "bottom": "horizontal",
            "left": "vertical",
            "right": "vertical",
        }
        for position, orientation in expected.items():
            window.settings = {"indicator_position": position}
            self.assertEqual(window.indicator_position, position)
            self.assertEqual(window.indicator_orientation, orientation)

    def test_legacy_side_position_migrates_to_right(self):
        window = object.__new__(RemielleWindow)
        window.settings = {"indicator_position": "side"}
        self.assertEqual(window.indicator_position, "right")
        self.assertEqual(window.indicator_orientation, "vertical")

    def test_unchanged_status_snapshot_skips_redraw(self):
        window = object.__new__(RemielleWindow)
        window._status_info = {"mode": "工作中", "token_total": "120"}
        window._panel = None
        window._apply_geometry = lambda: self.fail("unexpected geometry update")
        window._draw_indicator = lambda: self.fail("unexpected redraw")
        window.set_status_info({"mode": "工作中", "token_total": "120"})


class TestScaleAndMonitorGeometry(unittest.TestCase):
    def test_fitting_window_is_fully_clamped_to_monitor(self):
        bounds = (1920, -200, 3840, 1000)
        self.assertEqual(
            _clamp_window_to_bounds(
                5000, -900, 300, 400, bounds, margin=40
            ),
            (3540, -200),
        )

    def test_oversized_window_keeps_a_visible_handle(self):
        self.assertEqual(
            _clamp_window_to_bounds(
                5000, 5000, 2200, 1400, (0, 0, 1920, 1080), margin=40
            ),
            (1880, 1040),
        )

    def test_wheel_uses_five_percent_deferred_step(self):
        window = object.__new__(RemielleWindow)
        window.scale = 0.75
        window._scale_min = 0.2
        window._scale_max = 3.0
        window.config = {"rendering": {"scale_step": 0.05}}
        calls: list[tuple[float, dict]] = []
        window.set_scale = lambda value, **kwargs: calls.append((value, kwargs))
        event = type("WheelEvent", (), {"delta": 120})()
        window._wheel(event)
        self.assertEqual(calls, [(0.8, {"defer_render": True})])

    def test_reset_uses_current_monitor_work_area(self):
        class Root:
            @staticmethod
            def winfo_x(): return 2300

            @staticmethod
            def winfo_y(): return 200

            @staticmethod
            def winfo_width(): return 300

            @staticmethod
            def winfo_height(): return 280

        window = object.__new__(RemielleWindow)
        window.root = Root()
        window.config = {"default_scale": 0.75}
        window.base_width = 320
        window.base_height = 300
        window._default_offset_x = 40
        window._default_offset_y = 80
        window.settings = {}
        scales: list[tuple[float, bool]] = []
        window.set_scale = lambda value, save=True: scales.append((value, save))
        window._save_settings = lambda: None
        with patch(
            "remielle.window._get_monitor_work_area_for_rect",
            return_value=(1920, 0, 3840, 1040),
        ):
            window.reset_geometry()
        self.assertEqual(window.settings["x"], 3560)
        self.assertEqual(window.settings["y"], 735)
        self.assertEqual(scales, [(0.75, False)])


if __name__ == "__main__":
    unittest.main()
