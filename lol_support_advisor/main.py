from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import tkinter as tk

from .champions import ChampionRegistry
from .single_instance import SingleInstanceLock
from .storage import Storage
from .ui import AdvisorApp


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoL pick and user-triggered build advisor")
    parser.add_argument("--demo", action="store_true", help="show a populated offline preview")
    args = parser.parse_args(argv)

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
                AdvisorApp(
                    root, storage, registry, demo=True, asset_dir=data_dir,
                )
                root.mainloop()
        else:
            storage = Storage(data_dir / "advisor.db")
            root = tk.Tk()
            AdvisorApp(root, storage, registry)
            root.mainloop()
    finally:
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
