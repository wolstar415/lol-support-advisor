from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.opgg import OpggClient


class OpggParsingTests(unittest.TestCase):
    def test_rendered_counter_table_tokens_are_inverted_for_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            client = OpggClient(registry)
            tokens = [
                "Leona", "Win rate", "51.77", "%", "Pick rate", "8.07", "%",
                "Search a champion", "Win rate", "Games",
                "Janna", "46.69", "%", "1,480",
                "Braum", "48.92", "%", "1,799",
                "Nautilus", "54.07", "%", "3,477",
            ]
            entries = {item.champion_id: item for item in client._table_entries(tokens, "Leona")}
            self.assertAlmostEqual(entries["Janna"].versus_win_rate, 53.31)
            self.assertEqual(entries["Janna"].games, 1480)
            self.assertAlmostEqual(entries["Nautilus"].versus_win_rate, 45.93)


if __name__ == "__main__":
    unittest.main()
