from __future__ import annotations

import argparse
import json
import os
import sys
import tkinter as tk

sys.dont_write_bytecode = True

# Ensure the package root is on sys.path
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from remielle.config import (
    APP_DIR, CONFIG_PATH, DATA_DIR, LOGGER,
    init_default_files, load_config,
    check_single_instance, release_single_instance,
)


def _enable_windows_dpi_awareness() -> None:
    """Prevent Windows from bitmap-scaling Tk text on high-DPI displays."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="蕾米 AI 助手")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args()


def main() -> int:
    _enable_windows_dpi_awareness()
    args = parse_args()

    # Auto-generate config files from embedded defaults on first run
    init_default_files()

    config = load_config(CONFIG_PATH)
    try:
        from remielle.app import BridgeApp, inspect_assets, current_status
    except Exception as error:
        LOGGER.exception("could not import application dependencies")
        try:
            import tkinter.messagebox as _mb
            _r = tk.Tk()
            _r.withdraw()
            _mb.showerror(
                "蕾米 AI 助手无法启动",
                "Python 运行环境缺少依赖或版本不兼容。\n\n"
                f"{type(error).__name__}: {error}\n\n"
                "请安装 Python 3.12+ 和 Pillow：\n"
                "python -m pip install Pillow",
            )
            _r.destroy()
        except Exception:
            pass
        return 1
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

    # Developer demo is short-lived and may run beside the tray instance.
    owns_instance = False
    if not args.demo:
        owns_instance = check_single_instance()
    # Single-instance check via PID file (stale locks auto-cleaned)
    if not args.demo and not owns_instance:
        try:
            import tkinter.messagebox as _mb
            _r = tk.Tk()
            _r.withdraw()
            _mb.showinfo(
                "蕾米 AI 助手",
                "另一个实例已在运行中。\n\n"
                "如果确定没有其他实例，请删除文件：\n"
                f"{DATA_DIR / 'remielle-bridge.pid'}",
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
        if owns_instance:
            release_single_instance()

    return 0


if __name__ == "__main__":
    sys.exit(main())
