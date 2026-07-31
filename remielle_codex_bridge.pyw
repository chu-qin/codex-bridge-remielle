from __future__ import annotations

import argparse
import json
import os
import sys
import tkinter as tk

# Ensure the package root is on sys.path
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from remielle.config import (
    APP_DIR, CONFIG_PATH, LOGGER,
    init_default_files, load_config,
    check_single_instance, release_single_instance,
)
from remielle.app import BridgeApp, inspect_assets, current_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="蕾米 AI 助手")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args()


def main() -> int:
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
                "蕾米 AI 助手",
                "另一个实例已在运行中。\n\n"
                "如果确定没有其他实例，请删除文件：\n"
                f"{APP_DIR / 'remielle-bridge.pid'}",
            )
            _r.destroy()
        except Exception:
            pass
        return 1

    # Keep the PID file alive until exit
    try:
        def _log_unhandled(exc_type, exc_value, exc_tb):
            LOGGER.critical(
                "UNHANDLED EXCEPTION",
                exc_info=(exc_type, exc_value, exc_tb),
            )
        sys.excepthook = _log_unhandled

        app = BridgeApp(config, demo=args.demo)
        app.start()
    finally:
        release_single_instance()

    return 0


if __name__ == "__main__":
    sys.exit(main())
