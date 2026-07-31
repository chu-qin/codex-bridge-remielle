"""Unit tests for the state machine pure function ``_determine_target``.

These tests encode the DESIGN RULES that must not regress.
Run with:  python -m unittest remielle.tests.test_state_machine -v
"""

from __future__ import annotations

import unittest

from remielle.app import BridgeApp

_dt = BridgeApp._determine_target

# ── Shared defaults to keep test cases concise ──

_DEFAULTS = {
    "codex_active": False,
    "claude_busy": False,
    "codex_last_activity": 0.0,
    "now": 1000.0,
    "has_pending_reviews": False,
    "has_unread": False,
    "persistent": True,
    "auto_hide_complete": False,
    "think_timeout": 10.0,
    "intermittent_timeout": 3.0,
    "current_mode": "idle",
}


def _call(**overrides):
    kw = {**_DEFAULTS, **overrides}
    return _dt(**kw)


class TestCodexHighFrequency(unittest.TestCase):
    """Codex JSONL provides ~100 ms signals → gap-based degradation works."""

    def test_running_fresh_activity(self):
        self.assertEqual(_call(codex_active=True, codex_last_activity=999.5), "running")

    def test_intermittent(self):
        self.assertEqual(_call(codex_active=True, codex_last_activity=995.0),
                         "running_intermittent")

    def test_thinking(self):
        self.assertEqual(_call(codex_active=True, codex_last_activity=985.0), "thinking")

    def test_gap_at_intermittent_boundary(self):
        """gap == 3.0 → running (boundary is >, not >=)."""
        self.assertEqual(_call(codex_active=True, codex_last_activity=997.0), "running")

    def test_gap_at_thinking_boundary(self):
        """gap == 10.0 → intermittent (think_timeout boundary is >, not >=)."""
        self.assertEqual(_call(codex_active=True, codex_last_activity=990.0),
                         "running_intermittent")

    def test_gap_just_past_thinking_boundary(self):
        """gap == 10.001 → thinking."""
        self.assertEqual(_call(codex_active=True, codex_last_activity=989.999), "thinking")


class TestClaudeLowFrequency(unittest.TestCase):
    """Claude-only: session JSON writes every 5-30 s → always 'running'."""

    def test_claude_only_is_running(self):
        self.assertEqual(_call(claude_busy=True, codex_last_activity=0.0), "running")

    def test_claude_only_running_at_any_time(self):
        self.assertEqual(_call(claude_busy=True, now=5000.0, codex_last_activity=0.0),
                         "running")

    def test_claude_only_with_codex_inactive(self):
        self.assertEqual(_call(codex_active=False, claude_busy=True,
                              codex_last_activity=0.0), "running")


class TestBothActive(unittest.TestCase):
    """Both tools active → Codex high-frequency signal drives degradation."""

    def test_both_active_codex_fresh(self):
        self.assertEqual(_call(codex_active=True, claude_busy=True,
                              codex_last_activity=999.5), "running")

    def test_both_active_codex_stale(self):
        self.assertEqual(_call(codex_active=True, claude_busy=True,
                              codex_last_activity=985.0), "thinking")


class TestRegressionGuards(unittest.TestCase):
    """Tests that prevent the exact regressions we've already fixed twice."""

    def test_claude_only_never_thinking(self):
        """Claude-only busy MUST NOT degrade to thinking — low-frequency
        signal can't support gap-based degradation."""
        result = _call(codex_active=False, claude_busy=True,
                      codex_last_activity=0.0, now=1000.0)
        self.assertNotEqual(result, "thinking",
            "REGRESSION: Claude-only degraded to thinking! See DESIGN RULE #2.")
        self.assertEqual(result, "running")

    def test_idle_when_no_activity(self):
        """No tool active → idle (not some phantom active state)."""
        self.assertEqual(_call(codex_active=False, claude_busy=False,
                              codex_last_activity=0.0), "idle")


class TestLowerPriorityStates(unittest.TestCase):
    """review, ready, idle, hidden — tested when no tool is active."""

    def test_pending_reviews(self):
        self.assertEqual(_call(has_pending_reviews=True), "review")

    def test_unread(self):
        self.assertEqual(_call(has_unread=True), "ready")

    def test_persistent_idle(self):
        self.assertEqual(_call(), "idle")

    def test_not_persistent_hidden(self):
        self.assertEqual(_call(persistent=False), "hidden")

    def test_review_cleared_auto_hide(self):
        self.assertEqual(_call(current_mode="review", has_pending_reviews=False,
                              auto_hide_complete=True, persistent=False), "hidden")

    def test_review_cleared_persistent(self):
        self.assertEqual(_call(current_mode="review", has_pending_reviews=False,
                              persistent=True), "idle")

    def test_review_cleared_has_unread(self):
        self.assertEqual(_call(current_mode="review", has_pending_reviews=False,
                              has_unread=True), "ready")


class TestPriorityOrder(unittest.TestCase):
    """Active tasks always win over reviews, unread, idle."""

    def test_active_wins_over_review(self):
        self.assertEqual(_call(codex_active=True, codex_last_activity=999.5,
                              has_pending_reviews=True), "running")

    def test_active_wins_over_unread(self):
        self.assertEqual(_call(codex_active=True, codex_last_activity=999.5,
                              has_unread=True), "running")


class TestEdgeCases(unittest.TestCase):
    def test_review_with_pending_stays(self):
        self.assertEqual(_call(current_mode="review", has_pending_reviews=True), "review")

    def test_codex_first_poll_zero_activity(self):
        """When Codex is active but last_activity=0.0 (first poll before
        any line is read), gap=0.0 → running.  This avoids the old bug
        where effective_last_activity==0.0 incorrectly forced 'thinking'."""
        result = _call(codex_active=True, codex_last_activity=0.0, now=1000.0)
        self.assertEqual(result, "running")


if __name__ == "__main__":
    unittest.main()
