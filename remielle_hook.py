"""Tiny command-hook entry point for Remielle Codex Bridge.

Codex sends one official lifecycle JSON object on stdin.  This helper writes
only stable IDs to the bridge queue and never parses the transcript file.
"""

from __future__ import annotations

import json
import sys

sys.dont_write_bytecode = True

from remielle.hooks import emit_hook_event


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    if kind not in {"start", "complete"}:
        return 2
    try:
        payload = json.load(sys.stdin)
        emit_hook_event(kind, payload)
    except Exception:
        # Hooks must never interfere with the Codex turn.
        if kind == "complete":
            print('{"continue":true}')
        return 0
    if kind == "complete":
        # Stop hooks expect JSON on stdout when they exit successfully.
        print('{"continue":true,"suppressOutput":true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
