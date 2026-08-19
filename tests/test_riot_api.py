from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lol_support_advisor.riot_api import RiotApiClient, RiotApiError
from lol_support_advisor.storage import Storage


class PagingClient(RiotApiClient):
    def __init__(self) -> None:
        super().__init__("test-key")
        self.starts: list[int] = []

    def _get(self, url: str) -> list[str]:
        query = parse_qs(urlparse(url).query)
        start = int(query["start"][0])
        count = int(query["count"][0])
        self.starts.append(start)
        available = 50 if start == 200 else count
        return [f"KR_{index}" for index in range(start, start + available)]


class RiotApiTests(unittest.TestCase):
    def test_match_ids_pages_one_thousand_in_hundreds(self) -> None:
        client = PagingClient()
        ids = client.match_ids("puuid", count=1000)
        self.assertEqual(len(ids), 250)
        self.assertEqual(client.starts, [0, 100, 200])

    def test_ranked_entries_use_current_puuid_endpoint(self) -> None:
        client = RiotApiClient("test-key")
        with patch.object(client, "_get", return_value=[]) as get:
            self.assertEqual(client.league_entries_by_puuid("a/b", platform="kr"), [])
        self.assertIn("/entries/by-puuid/a%2Fb", get.call_args.args[0])

    def test_account_identity_can_be_resolved_from_game_session_puuid(self) -> None:
        client = RiotApiClient("test-key")
        with patch.object(
            client, "_get", return_value={"gameName": "Player", "tagLine": "KR1"}
        ) as get:
            account = client.resolve_account_by_puuid("a/b")
        self.assertEqual(account["gameName"], "Player")
        self.assertIn("/accounts/by-puuid/a%2Fb", get.call_args.args[0])

    def test_completed_full_history_uses_one_page_for_incremental_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            storage.set_setting("riot_full_history_puuid", "mine")
            client = RiotApiClient("test-key")
            with (
                patch.object(client, "resolve_account", return_value={"puuid": "mine"}),
                patch.object(client, "league_entries_by_puuid", return_value=[]),
                patch.object(client, "match_ids", return_value=[]) as match_ids,
            ):
                client.sync(storage, "Me", "KR1", count=1000)
            match_ids.assert_called_once_with("mine", count=100)

    def test_sync_records_newest_match_marker_after_id_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            client = RiotApiClient("test-key")
            payload = {
                "metadata": {"matchId": "KR_NEW", "participants": ["mine"]},
                "info": {
                    "queueId": 420, "gameCreation": 10,
                    "participants": [{"puuid": "mine"}],
                },
            }
            with (
                patch.object(client, "resolve_account", return_value={"puuid": "mine"}),
                patch.object(client, "league_entries_by_puuid", return_value=[]),
                patch.object(client, "match_ids", return_value=["KR_NEW"]),
                patch.object(client, "match", return_value=payload),
            ):
                client.sync(storage, "Me", "KR1", count=1000)

            self.assertEqual(storage.get_setting("riot_latest_match_id"), "KR_NEW")
            self.assertIsNotNone(storage.load_match("KR_NEW"))

    def test_player_match_page_caps_ids_and_details_at_ten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            client = RiotApiClient("test-key")
            ids = [f"KR_{index}" for index in range(20, 30)]

            def match_payload(match_id: str) -> dict[str, object]:
                return {
                    "metadata": {"matchId": match_id},
                    "info": {
                        "queueId": 420,
                        "gameCreation": 1,
                        "participants": [],
                    },
                }

            with (
                patch.object(
                    client,
                    "resolve_account",
                    return_value={
                        "puuid": "other-puuid",
                        "gameName": "Canonical",
                        "tagLine": "KR1",
                    },
                ),
                patch.object(client, "match_id_page", return_value=ids) as id_page,
                patch.object(
                    client, "match", side_effect=match_payload
                ) as match_detail,
            ):
                result = client.sync_player_match_page(
                    storage, "Other", "kr1", start=20, count=999
                )

            self.assertEqual(result, ("other-puuid", ids, 10, True))
            id_page.assert_called_once_with("other-puuid", start=20, count=10)
            self.assertEqual(match_detail.call_count, 10)
            self.assertEqual(
                storage.find_puuid_by_riot_id("Canonical#KR1"), "other-puuid"
            )
            manifest = storage.load_player_match_page("Canonical#KR1", 20)
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest[0], "other-puuid")
            self.assertEqual(manifest[1], ids)

    def test_player_match_page_reuses_cached_details_and_reports_last_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            storage.save_matches(
                [{
                    "metadata": {"matchId": "KR_cached"},
                    "info": {
                        "queueId": 420,
                        "gameCreation": 1,
                        "participants": [],
                    },
                }]
            )
            client = RiotApiClient("test-key")
            ids = ["KR_cached", "KR_new"]
            new_match = {
                "metadata": {"matchId": "KR_new"},
                "info": {
                    "queueId": 420,
                    "gameCreation": 2,
                    "participants": [],
                },
            }
            with (
                patch.object(
                    client, "resolve_account", return_value={"puuid": "other"}
                ),
                patch.object(client, "match_id_page", return_value=ids),
                patch.object(client, "match", return_value=new_match) as detail,
            ):
                result = client.sync_player_match_page(
                    storage, "Other", "KR1", start=-5, count=10
                )

            self.assertEqual(result, ("other", ids, 1, False))
            detail.assert_called_once_with("KR_new")
            self.assertIsNotNone(storage.load_match("KR_cached"))
            self.assertIsNotNone(storage.load_match("KR_new"))

    def test_player_match_page_uses_cached_puuid_when_riot_id_lookup_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            client = RiotApiClient("test-key")
            with (
                patch.object(client, "resolve_account") as resolve,
                patch.object(client, "match_id_page", return_value=[]) as id_page,
            ):
                result = client.sync_player_match_page(
                    storage, "Temporary Name", "KR1", known_puuid="known-puuid",
                )

            resolve.assert_not_called()
            id_page.assert_called_once_with("known-puuid", start=0, count=10)
            self.assertEqual(result, ("known-puuid", [], 0, False))

    def test_player_page_refetches_detail_deleted_during_retention_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            cached = {
                "metadata": {"matchId": "KR_race"},
                "info": {"queueId": 420, "gameCreation": 1, "participants": []},
            }
            storage.save_matches([cached])
            original_touch = storage.touch_match_cache

            def delete_before_touch(match_ids: list[str]) -> int:
                with storage._connect() as connection:
                    connection.execute(
                        "DELETE FROM matches WHERE match_id = ?", ("KR_race",)
                    )
                return original_touch(match_ids)

            client = RiotApiClient("test-key")
            with (
                patch.object(
                    client, "resolve_account", return_value={"puuid": "other"},
                ),
                patch.object(client, "match_id_page", return_value=["KR_race"]),
                patch.object(client, "match", return_value=cached) as detail,
                patch.object(
                    storage, "touch_match_cache", side_effect=delete_before_touch,
                ),
            ):
                result = client.sync_player_match_page(
                    storage, "Other", "KR1", start=0, count=10,
                )

            self.assertEqual(result, ("other", ["KR_race"], 1, False))
            detail.assert_called_once_with("KR_race")
            self.assertIsNotNone(storage.load_match("KR_race"))

    def test_match_id_page_makes_one_request_with_safe_bounds(self) -> None:
        client = RiotApiClient("test-key")
        with patch.object(
            client, "_get", return_value=[f"KR_{index}" for index in range(15)]
        ) as get:
            result = client.match_id_page("a/b", start=-3, count=1000)

        self.assertEqual(len(result), 10)
        get.assert_called_once()
        url = get.call_args.args[0]
        self.assertIn("/by-puuid/a%2Fb/ids?", url)
        self.assertEqual(parse_qs(urlparse(url).query)["queue"], ["420"])
        self.assertEqual(parse_qs(urlparse(url).query)["start"], ["0"])
        self.assertEqual(parse_qs(urlparse(url).query)["count"], ["10"])

    def test_key_validation_requires_account_puuid(self) -> None:
        client = RiotApiClient("test-key")
        with patch.object(client, "resolve_account", return_value={"puuid": "mine"}):
            self.assertEqual(client.validate_key_for_account("Me", "KR1"), "mine")
        with patch.object(client, "resolve_account", return_value={}):
            with self.assertRaises(RiotApiError):
                client.validate_key_for_account("Me", "KR1")


if __name__ == "__main__":
    unittest.main()
