from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import sys
import tempfile
import tkinter as tk

from .champions import ChampionRegistry
from .single_instance import SingleInstanceLock
from .storage import Storage
from .ui import AdvisorApp


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller one-file apps are extracted into a temporary _MEIPASS
        # directory. User settings, the Riot key, and match caches must live
        # beside the portable executable instead of disappearing on exit.
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """Return bundled read-only assets without moving user data into _MEIPASS."""
    bundled = getattr(sys, "_MEIPASS", "")
    if getattr(sys, "frozen", False) and bundled:
        return Path(str(bundled)).resolve()
    return Path(__file__).resolve().parent.parent


def configure_windows_app_identity() -> None:
    """Give source and frozen runs one stable Windows taskbar identity."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "LOL.Support.Advisor.Desktop"
        )
    except (AttributeError, OSError):
        pass


def apply_window_icon(root: tk.Tk) -> None:
    icon_path = resource_root() / "assets" / "app_icon.png"
    if not icon_path.is_file():
        return
    try:
        icon = tk.PhotoImage(file=str(icon_path))
        root.iconphoto(True, icon)
        # Tk only keeps the Tcl-side image name; retain the Python wrapper for
        # the full window lifetime so taskbar/title icons cannot disappear.
        root._advisor_app_icon = icon  # type: ignore[attr-defined]
    except (OSError, tk.TclError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoL pick and user-triggered build advisor")
    parser.add_argument("--demo", action="store_true", help="show a populated offline preview")
    args = parser.parse_args(argv)

    configure_windows_app_identity()
    instance_lock = SingleInstanceLock(
        r"Local\LOL-Pick-Advisor-Demo-Single-Instance"
        if args.demo else r"Local\LOL-Pick-Advisor-Single-Instance"
    )
    if not instance_lock.acquire():
        return 0

    try:
        data_dir = project_root() / "data"
        registry = ChampionRegistry(data_dir / "champions_ko.json")
        if args.demo:
            # Never let the public demo read the user's settings, API key,
            # Codex thread, match history, or player cache.  Only static image
            # assets are shared with the normal app so the preview still looks
            # complete.  The temporary database is removed when the window
            # closes and therefore can contain demo values only.
            with tempfile.TemporaryDirectory(
                prefix="lol-advisor-demo-", ignore_cleanup_errors=True,
            ) as demo_dir:
                storage = Storage(Path(demo_dir) / "advisor.db")
                root = tk.Tk()
                apply_window_icon(root)
                AdvisorApp(
                    root, storage, registry, demo=True, asset_dir=data_dir,
                )
                root.mainloop()
        else:
            storage = Storage(data_dir / "advisor.db")
            root = tk.Tk()
            apply_window_icon(root)
            AdvisorApp(root, storage, registry)
            root.mainloop()
    finally:
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
