from __future__ import annotations

from pathlib import Path
import queue
import threading
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.icons import (
    BuildAssetPreloader,
    ChampionIconCache,
    ItemIconCache,
    RemoteIconCache,
)
from lol_support_advisor.models import BuildAsset


class _FakeRoot:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object]] = []

    def after(self, _delay: int, _callback: object) -> str:
        self.scheduled.append((_delay, _callback))
        return "after-id"


class _ImmediateThread:
    def __init__(self, *, target: object, daemon: bool) -> None:
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        self.target()


class _PausedThread:
    targets: list[object] = []

    def __init__(self, *, target: object, daemon: bool) -> None:
        self.target = target
        self.daemon = daemon
        self.__class__.targets.append(target)

    def start(self) -> None:
        return


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class ChampionIconCacheTests(unittest.TestCase):
    def test_indexes_existing_patch_icons_once_without_get_time_glob(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_icon = root / "icons" / "15.24" / "Malphite.png"
            old_icon.parent.mkdir(parents=True)
            old_icon.write_bytes(b"cached-icon")
            registry = ChampionRegistry(root / "champions.json")
            registry.version = "16.16"
            cache = ChampionIconCache(_FakeRoot(), registry, root / "icons")

            with patch.object(Path, "glob", side_effect=AssertionError("rescanned")):
                self.assertTrue(cache.is_cached("Malphite"))
                self.assertEqual(cache._existing_path("Malphite"), old_icon)

    def test_pending_champion_deduplicates_equivalent_lambda_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            registry.version = "16.16"
            cache = ChampionIconCache(
                _FakeRoot(), registry, Path(temp_dir) / "icons",
            )
            delivered: list[str] = []

            def ready(card_key: str):
                return lambda key=card_key: delivered.append(key)

            _PausedThread.targets = []
            with patch("lol_support_advisor.icons.threading.Thread", _PausedThread):
                cache.get("Malphite", 48, ready("A:0"))
                cache.get("Malphite", 48, ready("A:0"))
                cache.get("Malphite", 48, ready("A:1"))

            self.assertEqual(len(_PausedThread.targets), 1)
            self.assertEqual(len(cache._callbacks["Malphite"]), 2)

    def test_failed_download_does_not_trigger_rebuild_or_immediate_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            registry.version = "16.16"
            cache = ChampionIconCache(
                _FakeRoot(), registry, Path(temp_dir) / "icons",
            )
            callbacks: list[str] = []
            with patch(
                "lol_support_advisor.icons.threading.Thread", _ImmediateThread,
            ), patch(
                "lol_support_advisor.icons.urlopen",
                side_effect=OSError("offline"),
            ) as fetch:
                self.assertIsNone(
                    cache.get("Malphite", 48, lambda: callbacks.append("ready"))
                )
                self.assertIsNone(
                    cache.get("Malphite", 48, lambda: callbacks.append("retry"))
                )

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(callbacks, [])

    def test_successful_download_is_atomically_published_before_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            registry.version = "16.16"
            cache = ChampionIconCache(
                _FakeRoot(), registry, Path(temp_dir) / "icons",
            )
            callback = lambda: None

            with patch(
                "lol_support_advisor.icons.threading.Thread", _ImmediateThread,
            ), patch(
                "lol_support_advisor.icons.urlopen",
                return_value=_FakeResponse(b"complete-image"),
            ):
                self.assertIsNone(cache.get("Malphite", 48, callback))

            path = cache._path("Malphite")
            self.assertEqual(path.read_bytes(), b"complete-image")
            self.assertFalse(path.with_suffix(".part").exists())
            self.assertIs(cache._ready.get_nowait(), callback)

    def test_callback_bookkeeping_is_safe_under_concurrent_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            cache = ChampionIconCache(
                _FakeRoot(), registry, Path(temp_dir) / "icons",
            )
            callbacks = [lambda value=index: value for index in range(64)]
            start = threading.Barrier(len(callbacks))

            def remember(callback: object) -> None:
                start.wait()
                cache._remember_callback("Malphite", callback)

            workers = [
                threading.Thread(target=remember, args=(callback,))
                for callback in callbacks
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual(len(cache._take_callbacks("Malphite")), 64)
            self.assertNotIn("Malphite", cache._callback_tokens)


class FailedIconCallbackTests(unittest.TestCase):
    def test_item_failure_is_sticky_and_never_queues_ready_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            registry.version = "16.16"
            cache = ItemIconCache(
                _FakeRoot(), registry, Path(temp_dir) / "items",
            )
            callbacks: list[str] = []
            with patch.object(cache, "_ensure_metadata"), patch(
                "lol_support_advisor.icons.urlopen",
                side_effect=OSError("offline"),
            ) as fetch:
                cache.get("3070", 24, lambda: callbacks.append("first"))
                cache._downloads.join()
                cache.get("3070", 24, lambda: callbacks.append("retry"))

            self.assertEqual(fetch.call_count, 1)
            self.assertIn("3070", cache._failed)
            with self.assertRaises(queue.Empty):
                cache._ready.get_nowait()
            self.assertEqual(callbacks, [])


class LocalizedItemMetadataTests(unittest.TestCase):
    def test_english_item_name_and_tooltip_use_cached_en_us_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            registry.version = "16.16"
            cache = ItemIconCache(
                _FakeRoot(), registry, Path(temp_dir) / "items",
            )
            cache._apply_localized_metadata({
                "data": {
                    "3070": {
                        "name": "Tear of the Goddess",
                        "plaintext": "Increases maximum mana",
                        "description": "<mainText>Gain <attention>Mana</attention></mainText>",
                        "gold": {"total": 400},
                    },
                },
            }, "16.16", "en_US")

            self.assertEqual(
                cache.localized_item_name(3070, "en", "여신의 눈물"),
                "Tear of the Goddess",
            )
            tooltip = cache.localized_tooltip_text(3070, "en")
            self.assertIn("Tear of the Goddess", tooltip)
            self.assertIn("Cost 400 gold", tooltip)
            self.assertNotIn("여신", tooltip)

    def test_remote_failure_and_invalid_input_do_not_queue_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = RemoteIconCache(_FakeRoot(), Path(temp_dir) / "remote")
            callbacks: list[str] = []
            url = "https://assets.example/rune.png"
            with patch(
                "lol_support_advisor.icons.urlopen",
                side_effect=OSError("offline"),
            ) as fetch:
                cache.get("rune:1", url, 24, lambda: callbacks.append("first"))
                cache._downloads.join()
                cache.get("rune:1", url, 24, lambda: callbacks.append("retry"))
                cache.get("", url, 24, lambda: callbacks.append("invalid"))

            self.assertEqual(fetch.call_count, 1)
            with self.assertRaises(queue.Empty):
                cache._ready.get_nowait()
            self.assertEqual(callbacks, [])


class ReadyQueueDrainTests(unittest.TestCase):
    def test_every_icon_cache_limits_a_ready_burst_and_reschedules_quickly(self) -> None:
        for cache_type, idle_delay in (
            (ChampionIconCache, 120),
            (ItemIconCache, 140),
            (RemoteIconCache, 140),
        ):
            with self.subTest(cache=cache_type.__name__):
                root = _FakeRoot()
                cache = cache_type.__new__(cache_type)
                cache.root = root
                cache._ready = queue.SimpleQueue()
                delivered: list[int] = []
                for index in range(20):
                    cache._ready.put(lambda value=index: delivered.append(value))

                cache._drain_ready()

                self.assertEqual(delivered, list(range(16)))
                self.assertEqual(root.scheduled[-1][0], 1)
                root.scheduled[-1][1]()
                self.assertEqual(delivered, list(range(20)))
                self.assertEqual(root.scheduled[-1][0], idle_delay)


class BuildAssetPreloaderTests(unittest.TestCase):
    def _preloader(self, temp_dir: str) -> BuildAssetPreloader:
        registry = ChampionRegistry(Path(temp_dir) / "champions.json")
        registry.version = "16.16"
        return BuildAssetPreloader(
            registry,
            Path(temp_dir) / "items",
            Path(temp_dir) / "remote",
        )

    def test_caches_unique_spells_from_every_recommended_combination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preloader = self._preloader(temp_dir)
            guide = SimpleNamespace(
                rune_builds=[],
                summoner_spell_builds=[
                    SimpleNamespace(spells=[
                        BuildAsset(4, "점멸", "https://assets/flash-a.png"),
                        BuildAsset(14, "점화", "https://assets/ignite.png"),
                    ]),
                    SimpleNamespace(spells=[
                        BuildAsset(3, "탈진", "https://assets/exhaust.png"),
                        BuildAsset(4, "점멸", "https://assets/flash-b.png"),
                    ]),
                ],
                # A new guide uses the combinations as the source of truth.
                summoner_spells=[
                    BuildAsset(7, "회복", "https://assets/heal.png"),
                ],
                item_groups=[],
            )

            with patch.object(
                preloader, "_download", return_value="cached",
            ) as download:
                totals = preloader.cache_guide(guide)

            self.assertEqual(totals, {"downloaded": 0, "cached": 3, "failed": 0})
            self.assertEqual(download.call_count, 3)
            downloaded_urls = {call.args[0] for call in download.call_args_list}
            self.assertEqual(downloaded_urls, {
                "https://assets/flash-a.png",
                "https://assets/ignite.png",
                "https://assets/exhaust.png",
            })

    def test_legacy_flat_spell_pair_remains_a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preloader = self._preloader(temp_dir)
            guide = SimpleNamespace(
                rune_builds=[],
                summoner_spells=[
                    BuildAsset(4, "점멸", "https://assets/flash.png"),
                    BuildAsset(14, "점화", "https://assets/ignite.png"),
                ],
                item_groups=[],
            )

            with patch.object(
                preloader, "_download", return_value="cached",
            ) as download:
                totals = preloader.cache_guide(guide)

            self.assertEqual(totals["cached"], 2)
            self.assertEqual(download.call_count, 2)


if __name__ == "__main__":
    unittest.main()
