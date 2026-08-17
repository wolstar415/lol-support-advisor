from __future__ import annotations

from pathlib import Path
import queue
import ssl
import threading
import tkinter as tk
from typing import Callable
from urllib.request import Request, urlopen

from .champions import ChampionRegistry


class ChampionIconCache:
    def __init__(self, root: tk.Misc, registry: ChampionRegistry, cache_dir: Path) -> None:
        self.root = root
        self.registry = registry
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._images: dict[tuple[str, int], tk.PhotoImage] = {}
        self._pending: set[str] = set()
        self._prefetch_versions: set[str] = set()
        self._ready: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.root.after(120, self._drain_ready)

    def _drain_ready(self) -> None:
        try:
            while True:
                callback = self._ready.get_nowait()
                callback()
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._drain_ready)
        except tk.TclError:
            return

    def _path(self, champion_id: str) -> Path:
        return self.cache_dir / self.registry.version / f"{champion_id}.png"

    def get(
        self,
        champion_id: str,
        size: int,
        on_ready: Callable[[], None] | None = None,
    ) -> tk.PhotoImage | None:
        key = (champion_id, size)
        if key in self._images:
            return self._images[key]
        path = self._path(champion_id)
        if path.exists():
            try:
                original = tk.PhotoImage(file=str(path))
                divisor = max(1, round(original.width() / max(size, 1)))
                image = original.subsample(divisor, divisor) if divisor > 1 else original
                self._images[key] = image
                return image
            except tk.TclError:
                return None
        url = self.registry.icon_url(champion_id)
        if not url or champion_id in self._pending:
            return None
        self._pending.add(champion_id)

        def download() -> None:
            try:
                request = Request(url, headers={"User-Agent": "LOL-Support-Advisor/0.2"})
                with urlopen(request, timeout=12, context=ssl.create_default_context()) as response:
                    payload = response.read()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            except OSError:
                pass
            finally:
                self._pending.discard(champion_id)
                if on_ready:
                    self._ready.put(on_ready)

        threading.Thread(target=download, daemon=True).start()
        return None

    def prefetch_all(self, on_ready: Callable[[], None] | None = None) -> None:
        """Download the current patch's icons sequentially without blocking Tk."""
        version = self.registry.version
        if version == "fallback" or version in self._prefetch_versions:
            return
        self._prefetch_versions.add(version)
        champion_ids = sorted(self.registry.by_id)

        def download_all() -> None:
            context = ssl.create_default_context()
            for champion_id in champion_ids:
                path = self._path(champion_id)
                if path.exists() or champion_id in self._pending:
                    continue
                url = self.registry.icon_url(champion_id)
                if not url:
                    continue
                self._pending.add(champion_id)
                try:
                    request = Request(url, headers={"User-Agent": "LOL-Support-Advisor/0.2"})
                    with urlopen(
                        request, timeout=12, context=context
                    ) as response:
                        payload = response.read()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                except OSError:
                    continue
                finally:
                    self._pending.discard(champion_id)
            if on_ready:
                self._ready.put(on_ready)

        threading.Thread(target=download_all, daemon=True).start()


class ItemIconCache:
    """Lazy, single-worker Data Dragon item icon cache for match history views."""

    def __init__(
        self, root: tk.Misc, registry: ChampionRegistry, cache_dir: Path
    ) -> None:
        self.root = root
        self.registry = registry
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._images: dict[tuple[str, int], tk.PhotoImage] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._callbacks: dict[str, list[Callable[[], None]]] = {}
        self._downloads: queue.Queue[
            tuple[str, Path, str, Callable[[], None] | None]
        ] = queue.Queue()
        self._ready: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        threading.Thread(target=self._download_worker, daemon=True).start()
        self.root.after(140, self._drain_ready)

    def _path(self, item_id: str) -> Path:
        return self.cache_dir / self.registry.version / f"{item_id}.png"

    def get(
        self,
        item_id: int | str,
        size: int,
        on_ready: Callable[[], None] | None = None,
    ) -> tk.PhotoImage | None:
        normalized = str(item_id or "")
        if not normalized or normalized == "0":
            return None
        key = (normalized, size)
        if key in self._images:
            return self._images[key]
        path = self._path(normalized)
        if path.exists():
            try:
                original = tk.PhotoImage(file=str(path))
                divisor = max(1, round(original.width() / max(size, 1)))
                image = original.subsample(divisor, divisor) if divisor > 1 else original
                self._images[key] = image
                return image
            except tk.TclError:
                self._failed.add(normalized)
                return None
        if normalized in self._pending:
            if on_ready:
                self._callbacks.setdefault(normalized, []).append(on_ready)
            return None
        if self.registry.version == "fallback" or normalized in self._failed:
            return None
        self._pending.add(normalized)
        if on_ready:
            self._callbacks.setdefault(normalized, []).append(on_ready)
        url = (
            f"https://ddragon.leagueoflegends.com/cdn/{self.registry.version}/"
            f"img/item/{normalized}.png"
        )
        self._downloads.put((normalized, path, url, None))
        return None

    def _download_worker(self) -> None:
        context = ssl.create_default_context()
        while True:
            item_id, path, url, on_ready = self._downloads.get()
            try:
                request = Request(url, headers={"User-Agent": "LOL-Support-Advisor/0.2"})
                with urlopen(request, timeout=12, context=context) as response:
                    payload = response.read()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            except OSError:
                self._failed.add(item_id)
            finally:
                self._pending.discard(item_id)
                if on_ready:
                    self._ready.put(on_ready)
                for callback in self._callbacks.pop(item_id, []):
                    self._ready.put(callback)
                self._downloads.task_done()

    def _drain_ready(self) -> None:
        try:
            while True:
                self._ready.get_nowait()()
        except queue.Empty:
            pass
        try:
            self.root.after(140, self._drain_ready)
        except tk.TclError:
            return
