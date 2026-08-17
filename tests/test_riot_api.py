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

    def test_key_validation_requires_account_puuid(self) -> None:
        client = RiotApiClient("test-key")
        with patch.object(client, "resolve_account", return_value={"puuid": "mine"}):
            self.assertEqual(client.validate_key_for_account("Me", "KR1"), "mine")
        with patch.object(client, "resolve_account", return_value={}):
            with self.assertRaises(RiotApiError):
                client.validate_key_for_account("Me", "KR1")


if __name__ == "__main__":
    unittest.main()
