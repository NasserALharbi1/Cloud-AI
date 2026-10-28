"""Entry point for Cloud — Terminal-based AI Agent for Windows (TUI and optional --gui mode)."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path


def _find_icon_path(filename: str = "cloud.ico") -> Path | None:
    """Locate cloud.ico or cloud.png in PyInstaller _MEIPASS, exe folder, or project root."""
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(getattr(sys, "_MEIPASS")) / filename)
    candidates.append(Path(sys.executable).resolve().parent / filename)
    candidates.append(Path(__file__).resolve().parent / filename)
    for p in candidates:
        if p.is_file():
            return p
    return None


def _configure_windows_utf8_console() -> None:
    """Force UTF-8 codepage (65001), UTF-8 stdio streams, and custom console icon on Windows."""
    if os.name != "nt":
        return
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleTitleW("Cloud")

        # Apply cloud.ico to the Windows Console window titlebar & taskbar (64-bit safe)
        ico_path = _find_icon_path("cloud.ico")
        kernel32.GetConsoleWindow.restype = ctypes.wintypes.HWND
        hwnd = kernel32.GetConsoleWindow()
        if hwnd and ico_path:
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x00000010
            WM_SETICON = 0x0080
            user32.LoadImageW.argtypes = [
                ctypes.wintypes.HINSTANCE,
                ctypes.wintypes.LPCWSTR,
                ctypes.wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.wintypes.UINT,
            ]
            user32.LoadImageW.restype = ctypes.wintypes.HANDLE
            user32.SendMessageW.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.wintypes.UINT,
                ctypes.wintypes.WPARAM,
                ctypes.wintypes.LPARAM,
            ]
            h_icon = user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if h_icon:
                user32.SendMessageW(hwnd, WM_SETICON, 0, h_icon)
                user32.SendMessageW(hwnd, WM_SETICON, 1, h_icon)
    except Exception:
        pass

    for stream_name in ("stdout", "stderr", "__stdout__", "__stderr__", "stdin", "__stdin__"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_windows_utf8_console()

from cloud_agent import __version__  # noqa: E402
from cloud_agent.tui.app import CloudApp  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cloud",
        description="Cloud — Autonomous Terminal AI Agent for Windows",
    )
    parser.add_argument(
        "--cwd",
        type=str,
        default=None,
        help="Initial working directory (defaults to current directory)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch native Windows GUI mode (PySide6) with native Arabic RTL rendering",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Cloud AI Agent v{__version__}",
    )
    args = parser.parse_args()

    start_dir = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    if args.gui:
        from cloud_agent.tui.gui_app import run_gui_app
        return run_gui_app(initial_cwd=start_dir)

    app = CloudApp(initial_cwd=start_dir)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
