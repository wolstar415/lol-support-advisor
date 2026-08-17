from __future__ import annotations

from html import unescape
from hashlib import sha1
import json
from pathlib import Path
import queue
import re
import ssl
import threading
import tkinter as tk
from typing import Callable
from urllib.request import Request, urlopen

from .champions import ChampionRegistry
from .models import ChampionBuildGuide


class ChampionIconCache:
    def __init__(self, root: tk.Misc, registry: ChampionRegistry, cache_dir: Path) -> None:
        self.root = root
        self.registry = registry
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._images: dict[tuple[str, int], tk.PhotoImage] = {}
        self._pending: set[str] = set()
        self._callbacks: dict[str, list[Callable[[], None]]] = {}
        self._prefetch_versions: set[str] = set()
        self._existing_paths: dict[str, Path] = {}
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

    def _existing_path(self, champion_id: str) -> Path | None:
        current = self._path(champion_id)
        try:
            if current.is_file() and current.stat().st_size > 0:
                self._existing_paths[champion_id] = current
                return current
        except OSError:
            pass
        remembered = self._existing_paths.get(champion_id)
        try:
            if remembered and remembered.is_file() and remembered.stat().st_size > 0:
                return remembered
        except OSError:
            self._existing_paths.pop(champion_id, None)
        try:
            candidates = [
                path for path in self.cache_dir.glob(f"*/{champion_id}.png")
                if path.is_file() and path.stat().st_size > 0
            ]
        except OSError:
            return None
        if not candidates:
            return None
        selected = max(candidates, key=lambda path: path.stat().st_mtime)
        self._existing_paths[champion_id] = selected
        return selected

    def is_cached(self, champion_id: str) -> bool:
        """Return whether a reusable local icon exists in any cached patch."""
        return self._existing_path(champion_id) is not None

    def missing_ids(self) -> list[str]:
        """List only icons that would require a download for the current patch."""
        return [
            champion_id for champion_id in sorted(self.registry.by_id)
            if not self.is_cached(champion_id)
        ]

    def get(
        self,
        champion_id: str,
        size: int,
        on_ready: Callable[[], None] | None = None,
    ) -> tk.PhotoImage | None:
        key = (champion_id, size)
        if key in self._images:
            return self._images[key]
        path = self._existing_path(champion_id)
        if path:
            try:
                original = tk.PhotoImage(file=str(path))
                divisor = max(1, round(original.width() / max(size, 1)))
                image = original.subsample(divisor, divisor) if divisor > 1 else original
                self._images[key] = image
                return image
            except tk.TclError:
                return None
        url = self.registry.icon_url(champion_id)
        if not url:
            return None
        path = self._path(champion_id)
        if on_ready:
            self._callbacks.setdefault(champion_id, []).append(on_ready)
        if champion_id in self._pending:
            return None
        self._pending.add(champion_id)

        def download() -> None:
            try:
                request = Request(url, headers={"User-Agent": "LOL-Support-Advisor/0.2"})
                with urlopen(request, timeout=12, context=ssl.create_default_context()) as response:
                    payload = response.read()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                self._existing_paths[champion_id] = path
            except OSError:
                pass
            finally:
                self._pending.discard(champion_id)
                for callback in self._callbacks.pop(champion_id, []):
                    self._ready.put(callback)

        threading.Thread(target=download, daemon=True).start()
        return None

    def prefetch_all(self, on_ready: Callable[[], None] | None = None) -> bool:
        """Download the current patch's icons sequentially without blocking Tk."""
        version = self.registry.version
        if version == "fallback" or version in self._prefetch_versions:
            if on_ready:
                self._ready.put(on_ready)
            return False
        self._prefetch_versions.add(version)
        champion_ids = sorted(self.registry.by_id)

        def download_all() -> None:
            context = ssl.create_default_context()
            for champion_id in champion_ids:
                path = self._path(champion_id)
                if self.is_cached(champion_id) or champion_id in self._pending:
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
                    self._existing_paths[champion_id] = path
                except OSError:
                    continue
                finally:
                    self._pending.discard(champion_id)
                    for callback in self._callbacks.pop(champion_id, []):
                        self._ready.put(callback)
            if on_ready:
                self._ready.put(on_ready)

        threading.Thread(target=download_all, daemon=True).start()
        return True


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
        self._item_data: dict[str, dict] = {}
        self._metadata_version = ""
        self._metadata_loading = False
        self._metadata_callbacks: list[Callable[[], None]] = []
        self._downloads: queue.Queue[
            tuple[str, Path, str, Callable[[], None] | None]
        ] = queue.Queue()
        self._ready: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        threading.Thread(target=self._download_worker, daemon=True).start()
        self.root.after(140, self._drain_ready)

    def _metadata_path(self, version: str) -> Path:
        return self.cache_dir / version / "item-ko_KR.json"

    def _apply_metadata(self, payload: dict, version: str) -> None:
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return
        self._item_data = {
            str(item_id): item for item_id, item in data.items() if isinstance(item, dict)
        }
        self._metadata_version = version
        callbacks, self._metadata_callbacks = self._metadata_callbacks, []
        for callback in callbacks:
            callback()

    def _ensure_metadata(self, on_ready: Callable[[], None] | None = None) -> None:
        version = self.registry.version
        if version == "fallback" or self._metadata_version == version:
            return
        if on_ready:
            self._metadata_callbacks.append(on_ready)
        path = self._metadata_path(version)
        if path.exists():
            try:
                self._apply_metadata(json.loads(path.read_text(encoding="utf-8")), version)
                return
            except (OSError, ValueError, TypeError):
                pass
        if self._metadata_loading:
            return
        self._metadata_loading = True

        def download() -> None:
            try:
                url = (
                    f"https://ddragon.leagueoflegends.com/cdn/{version}/"
                    "data/ko_KR/item.json"
                )
                request = Request(url, headers={"User-Agent": "LOL-Support-Advisor/0.2"})
                with urlopen(
                    request, timeout=12, context=ssl.create_default_context()
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                self._ready.put(lambda: self._apply_metadata(payload, version))
            except (OSError, ValueError, TypeError):
                pass
            finally:
                self._metadata_loading = False

        threading.Thread(target=download, daemon=True).start()

    def item_name(
        self, item_id: int | str, fallback: str = "",
        on_ready: Callable[[], None] | None = None,
    ) -> str:
        self._ensure_metadata(on_ready)
        item = self._item_data.get(str(item_id or ""))
        return str((item or {}).get("name") or fallback or f"아이템 #{item_id}")

    @staticmethod
    def _plain_description(value: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.IGNORECASE)
        text = re.sub(
            r"</(?:li|p|maintext|stats|attention|passive|active)>",
            "\n",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", " ", text)
        lines = [" ".join(line.split()) for line in unescape(text).splitlines()]
        return "\n".join(line for line in lines if line)

    def tooltip_text(self, item_id: int | str) -> str:
        normalized = str(item_id or "")
        self._ensure_metadata()
        item = self._item_data.get(normalized)
        if not item:
            return f"아이템 #{normalized}\n설명을 불러오는 중입니다."
        name = str(item.get("name") or f"아이템 #{normalized}")
        plaintext = str(item.get("plaintext") or "").strip()
        description = self._plain_description(str(item.get("description") or ""))
        gold = item.get("gold") or {}
        total_gold = int(gold.get("total") or 0) if isinstance(gold, dict) else 0
        lines = [name, f"가격 {total_gold:,}골드 · ID {normalized}"]
        if plaintext:
            lines.append(plaintext)
        if description and description.casefold() != plaintext.casefold():
            lines.append(description)
        return "\n".join(lines)

    def _path(self, item_id: str) -> Path:
        return self.cache_dir / self.registry.version / f"{item_id}.png"

    def get(
        self,
        item_id: int | str,
        size: int,
        on_ready: Callable[[], None] | None = None,
    ) -> tk.PhotoImage | None:
        self._ensure_metadata()
        normalized = str(item_id or "")
        if not normalized or normalized == "0":
            if on_ready:
                self._ready.put(on_ready)
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
            if on_ready:
                self._ready.put(on_ready)
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


class RemoteIconCache:
    """Small asynchronous cache for OP.GG rune and spell icons."""

    def __init__(self, root: tk.Misc, cache_dir: Path) -> None:
        self.root = root
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._images: dict[tuple[str, int], tk.PhotoImage] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._callbacks: dict[str, list[Callable[[], None]]] = {}
        self._ready: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._downloads: queue.Queue[tuple[str, Path, str]] = queue.Queue()
        threading.Thread(target=self._download_worker, daemon=True).start()
        self.root.after(140, self._drain_ready)

    def _path(self, key: str, url: str) -> Path:
        digest = sha1(f"{key}|{url}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.png"

    def get(
        self, key: str, url: str, size: int,
        on_ready: Callable[[], None] | None = None,
    ) -> tk.PhotoImage | None:
        if not key or not url:
            if on_ready:
                self._ready.put(on_ready)
            return None
        image_key = (f"{key}|{url}", size)
        if image_key in self._images:
            return self._images[image_key]
        path = self._path(key, url)
        if path.exists():
            try:
                original = tk.PhotoImage(file=str(path))
                divisor = max(1, round(original.width() / max(size, 1)))
                image = original.subsample(divisor, divisor) if divisor > 1 else original
                self._images[image_key] = image
                return image
            except tk.TclError:
                self._failed.add(pending_key := str(path))
                if on_ready:
                    self._ready.put(on_ready)
                return None
        pending_key = str(path)
        if on_ready:
            self._callbacks.setdefault(pending_key, []).append(on_ready)
        if pending_key in self._failed:
            for callback in self._callbacks.pop(pending_key, []):
                self._ready.put(callback)
            return None
        if pending_key in self._pending:
            return None
        self._pending.add(pending_key)

        self._downloads.put((pending_key, path, url))
        return None

    def _download_worker(self) -> None:
        context = ssl.create_default_context()
        while True:
            pending_key, path, url = self._downloads.get()
            try:
                request = Request(url, headers={"User-Agent": "LOL-Support-Advisor/0.3"})
                with urlopen(
                    request, timeout=12, context=context
                ) as response:
                    path.write_bytes(response.read())
            except OSError:
                self._failed.add(pending_key)
            finally:
                self._pending.discard(pending_key)
                for callback in self._callbacks.pop(pending_key, []):
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


class BuildAssetPreloader:
    """Sequential disk-only downloader used by the bulk build cache worker."""

    def __init__(
        self, registry: ChampionRegistry, item_dir: Path, remote_dir: Path
    ) -> None:
        self.registry = registry
        self.item_dir = item_dir
        self.remote_dir = remote_dir
        self.item_dir.mkdir(parents=True, exist_ok=True)
        self.remote_dir.mkdir(parents=True, exist_ok=True)
        self._context = ssl.create_default_context()

    def _remote_path(self, key: str, url: str) -> Path:
        digest = sha1(f"{key}|{url}".encode("utf-8")).hexdigest()
        return self.remote_dir / f"{digest}.png"

    def _item_path(self, item_id: int) -> Path:
        return self.item_dir / self.registry.version / f"{item_id}.png"

    def _download(self, url: str, path: Path) -> str:
        if not url:
            return "failed"
        if path.exists() and path.stat().st_size > 0:
            return "cached"
        temporary = path.with_suffix(".part")
        try:
            request = Request(url, headers={"User-Agent": "LOL-Support-Advisor/0.3"})
            with urlopen(request, timeout=12, context=self._context) as response:
                payload = response.read()
            if not payload:
                return "failed"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(payload)
            temporary.replace(path)
            return "downloaded"
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return "failed"

    def cache_guide(self, guide: ChampionBuildGuide) -> dict[str, int]:
        totals = {"downloaded": 0, "cached": 0, "failed": 0}
        seen: set[tuple[str, int, str]] = set()
        for rune_build in guide.rune_builds:
            for perk in rune_build.perks:
                key = ("rune", perk.asset_id, perk.icon_url)
                if key in seen:
                    continue
                seen.add(key)
                status = self._download(
                    perk.icon_url,
                    self._remote_path(f"rune:{perk.asset_id}", perk.icon_url),
                )
                totals[status] += 1
        for spell in guide.summoner_spells:
            key = ("spell", spell.asset_id, spell.icon_url)
            if key in seen:
                continue
            seen.add(key)
            status = self._download(
                spell.icon_url,
                self._remote_path(f"spell:{spell.asset_id}", spell.icon_url),
            )
            totals[status] += 1
        for group in guide.item_groups:
            for item in group.items:
                key = ("item", item.asset_id, "")
                if key in seen:
                    continue
                seen.add(key)
                item_url = item.icon_url or (
                    f"https://ddragon.leagueoflegends.com/cdn/{self.registry.version}/"
                    f"img/item/{item.asset_id}.png"
                )
                status = self._download(item_url, self._item_path(item.asset_id))
                totals[status] += 1
        return totals
