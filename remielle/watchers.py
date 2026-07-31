from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .config import expand_path, load_json, LOGGER, TERMINAL_EVENT_TYPES, _pid_is_alive

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
        # Cache for _recent_files() — full rglob + stat of the sessions
        # tree is expensive; cache results for 30 s between refreshes.
        self._file_cache: list[Path] | None = None
        self._file_cache_time: float = 0.0
        self._file_cache_ttl: float = 30.0

    def _recent_files(self) -> list[Path]:
        """Return ``.jsonl`` files modified within ``recent_days``.

        Results are cached for ``_file_cache_ttl`` seconds to avoid
        repeated full-directory scans when the poll fires at 350 ms.
        """
        now = time.time()
        if (self._file_cache is not None
                and now - self._file_cache_time < self._file_cache_ttl):
            return self._file_cache
        if not self.sessions_dir.exists():
            self._file_cache = []
            self._file_cache_time = now
            return []
        cutoff = now - self.recent_days * 86400
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
            self._file_cache = []
            self._file_cache_time = now
            return []
        files.sort(key=lambda item: item[0])
        self._file_cache = [path for _, path in files]
        self._file_cache_time = now
        return self._file_cache

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
    def __init__(self, state_path: Path,
                 sessions_dir: Path | None = None,
                 stale_hours: float = 4.0) -> None:
        self.state_path = state_path
        self.sessions_dir = sessions_dir
        self.stale_hours = stale_hours
        self.last_mtime_ns = -1
        self.unread_thread_ids: set[str] = set()

    def _recent_thread_ids(self) -> set[str]:
        """Return thread UUIDs whose newest session file was modified
        within ``stale_hours``.

        Codex's ``unread-thread-ids-by-host-v1`` can accumulate stale
        entries that are never cleared even after messages are read.
        Cross-referencing with recent on-disk activity prevents the pet
        from getting stuck in "ready" mode indefinitely.
        """
        if self.sessions_dir is None or not self.sessions_dir.exists():
            return set()
        cutoff = time.time() - self.stale_hours * 3600
        # Per-thread newest mtime:  thread_id → max mtime
        thread_mtimes: dict[str, float] = {}
        try:
            for path in self.sessions_dir.rglob("*.jsonl"):
                tid = CodexSessionWatcher._thread_id_from_path(path)
                if not tid:
                    continue
                try:
                    mt = path.stat().st_mtime
                except OSError:
                    continue
                if mt > thread_mtimes.get(tid, 0):
                    thread_mtimes[tid] = mt
        except OSError:
            return set()
        return {tid for tid, mt in thread_mtimes.items() if mt >= cutoff}

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

        # Cross-reference with on-disk session file recency.
        # A thread that Codex still lists as "unread" but whose newest
        # session file hasn't been touched in ``stale_hours`` is stale —
        # the user has already seen those messages.
        #
        # Always intersect — even when *every* thread is stale (recent
        # is empty), the intersection correctly yields zero unread IDs.
        recent = self._recent_thread_ids()
        validated = new_ids & recent
        dropped = new_ids - recent
        if dropped:
            LOGGER.info(
                "unread watcher: %d raw → %d validated "
                "(dropped %d threads inactive >%.0fh)",
                len(new_ids), len(validated), len(dropped),
                self.stale_hours,
            )
        new_ids = validated

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

    def __init__(self, sessions_dir: Path,
                 scan_interval_seconds: float = 1.0) -> None:
        self.sessions_dir = sessions_dir
        self._scan_interval = scan_interval_seconds
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
        # Per-PID ``updatedAt`` cache for granular activity detection.
        # When ``updatedAt`` advances while status is "busy", the model
        # is actively producing output (tokens, tool calls, etc.).
        self._updated_ats: dict[int, int] = {}
        # Cache parsed session JSON by (path → (mtime_ns, data)) so we
        # don't re-read+parse files whose on-disk content hasn't changed.
        self._session_cache: dict[Path, tuple[int, dict]] = {}

    def _read_session_cached(self, path: Path) -> dict | None:
        """Return parsed JSON for *path*, or ``None`` on error.

        Caches by ``st_mtime_ns`` so a file that hasn't been rewritten
        since the last scan is returned from memory without touching the
        filesystem parser.
        """
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            self._session_cache.pop(path, None)
            return None
        cached = self._session_cache.get(path)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        self._session_cache[path] = (mtime_ns, data)
        return data

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
        self._next_scan = now_mono + self._scan_interval

        busy = False
        latest_mtime = 0.0
        latest_busy_mtime = 0.0
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
                data = self._read_session_cached(path)
                if data is None:
                    continue
                pid = data.get("pid", 0)
                sid = data.get("sessionId", "")
                name = data.get("name", "")
                updated_at = data.get("updatedAt", 0)
                if data.get("status") == "busy":
                    if pid and _pid_is_alive(pid):
                        # ── Busy-staleness guard ──
                        # ``updatedAt`` is a JS epoch (milliseconds since
                        # 1970).  If it hasn't changed in a long time while
                        # the status is still "busy", the session is stale —
                        # the owning process may be hung or the session file
                        # wasn't cleaned up properly.
                        now_ms = int(time.time() * 1000)
                        stale_cutoff_ms = now_ms - 1_800_000  # 30 min
                        if updated_at > 0 and updated_at < stale_cutoff_ms:
                            LOGGER.info(
                                "claude session: ignoring stale busy pid=%d "
                                "(updatedAt age=%d min)",
                                pid,
                                (now_ms - updated_at) // 60_000,
                            )
                            continue  # treat as not-busy
                        busy = True
                        busy_pid = pid
                        current_busy_pids.add(pid)
                        if mtime > latest_busy_mtime:
                            latest_busy_mtime = mtime
                        # Track session info so we can emit a rich
                        # completion event when it transitions to idle.
                        if pid not in self._busy_sessions:
                            self._busy_sessions[pid] = {
                                "sessionId": sid,
                                "name": name,
                            }
                        # ── Granular activity via ``updatedAt`` ──
                        # Claude Code bumps this field every time it
                        # writes to the session file (token streaming,
                        # tool calls, etc.).  We use it as a high-res
                        # proxy for "the model is producing output."
                        prev_ua = self._updated_ats.get(pid, 0)
                        if updated_at > prev_ua:
                            self._last_activity_monotonic = time.monotonic()
                        self._updated_ats[pid] = updated_at
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
                self._updated_ats.pop(pid, None)  # clean up stale entry

        # ── Activity tracking ──
        # Only busy-session file changes count as activity.  Idle session
        # files (or files from stale sessions) should NOT refresh the
        # activity timer — otherwise a lingering idle session that writes
        # frequently keeps the pet in "running" mode forever.
        if latest_busy_mtime > self._last_mtime:
            self._last_mtime = latest_busy_mtime
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
