from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tkinter as tk

from .champions import ChampionRegistry
from .storage import Storage
from .ui import AdvisorApp


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only LoL support pick advisor")
    parser.add_argument("--demo", action="store_true", help="show a populated offline preview")
    args = parser.parse_args(argv)

    data_dir = project_root() / "data"
    storage = Storage(data_dir / "advisor.db")
    registry = ChampionRegistry(data_dir / "champions_ko.json")
    root = tk.Tk()
    AdvisorApp(root, storage, registry, demo=args.demo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
