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

    def test_non_support_position_accepts_position_page_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            client = OpggClient(registry)
            tokens = [
                "Nidalee", "Win rate", "50.0", "%", "Win rate", "Games",
                "LeeSin", "47.5", "%", "2,000",
                "Hecarim", "51.0", "%", "1,500",
            ]
            entries = {
                item.champion_id: item
                for item in client._table_entries(tokens, "Nidalee", "JUNGLE")
            }
            self.assertIn("LeeSin", entries)
            self.assertIn("Hecarim", entries)

    def test_position_ranking_keeps_opgg_order_pick_and_ban_rates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            client = OpggClient(registry)
            client._fetch = lambda _url: """
                <div>Ranking Table</div><div>Rank</div><div>Champion</div>
                <div>Tier</div><div>Role</div><div>Win rate</div>
                <div>Pick rate</div><div>Ban rate</div>
                <div>1</div><div>Thresh</div><div>52.18</div><div>%</div>
                <div>13.82</div><div>%</div><div>9.30</div><div>%</div>
                <div>2</div><div>Leona</div><div>51.79</div><div>%</div>
                <div>8.07</div><div>%</div><div>7.51</div><div>%</div>
                <div>3</div><div>10</div><div>Lux</div><div>50.50</div><div>%</div>
                <div>6.20</div><div>%</div><div>4.10</div><div>%</div>
                <div>Patch 16.16</div>
            """
            snapshot = client.refresh_overall("SUPPORT")
            self.assertEqual(
                [entry.champion_id for entry in snapshot.counters],
                ["Thresh", "Leona", "Lux"],
            )
            self.assertEqual(snapshot.counters[0].position_rank, 1)
            self.assertEqual(snapshot.counters[2].position_rank, 3)
            self.assertAlmostEqual(snapshot.counters[0].pick_rate or 0, 13.82)
            self.assertAlmostEqual(snapshot.counters[0].ban_rate or 0, 9.30)
            self.assertEqual(snapshot.patch, "16.16")
            patch, champion_ids = client.refresh_position_champions("SUPPORT")
            self.assertEqual(patch, "16.16")
            self.assertEqual(champion_ids, ["Thresh", "Leona", "Lux"])

    def test_build_parser_extracts_runes_spells_skills_and_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            client = OpggClient(registry)
            rune_ids = "8465,8463,8473,8242,8345,8347,5005,5001,5001"
            html = rf'''
                <script>\"importClientData\":{{\"type\":\"CHAMPION_DETAIL_BUILD\",\"championKey\":\"thresh\",\"primaryStyleId\":8400,\"subStyleId\":8300,\"selectedPerkIds\":[{rune_ids}]}}</script>
                <table><caption>SummonerSpells Table</caption><tr><td>
                <img alt="Flash" src="https://x/lol/16.16.1/spell/SummonerFlash.png" />
                <img alt="Ignite" src="https://x/lol/16.16.1/spell/SummonerDot.png" />
                </td></tr></table>
                <table><caption>SkillOrder Table</caption><tr><td>
                <span>Q</span><span>E</span><span>W</span><span>Q</span>
                <span>E</span><span>W</span><span>Q</span><span>Q</span>
                <span>R</span><span>Q</span><span>E</span><span>Q</span>
                <span>E</span><span>R</span><span>E</span><span>E</span>
                <span>W</span><span>W</span>
                </td></tr></table>
                <table><caption>Items Table</caption><tr><td>Starter items
                <img alt="Health Potion" src="https://x/lol/16.16.1/item/2003.png" />
                </td></tr></table>
            '''
            client._fetch = lambda _url: html
            guide = client.refresh_build("Thresh", "SUPPORT")
            self.assertEqual(len(guide.rune_builds[0].perks), 9)
            self.assertEqual(
                [spell.asset_id for spell in guide.summoner_spells], [4, 14]
            )
            self.assertEqual(guide.skill_priority, ["Q", "E", "W"])
            self.assertEqual(
                [item.asset_id for item in guide.item_groups[0].items], [3865, 2003]
            )


if __name__ == "__main__":
    unittest.main()
