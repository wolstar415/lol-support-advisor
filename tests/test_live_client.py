from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.live_client import LiveClient


class FakeLiveClient(LiveClient):
    def get(self, path: str):
        if path.endswith("playerlist"):
            return [
                {
                    "championName": "Kai'Sa", "riotId": "Me#KR1",
                    "riotIdGameName": "Me", "riotIdTagLine": "KR1",
                    "team": "ORDER", "position": "BOTTOM", "level": 8,
                },
                {
                    "championName": "Leona", "riotId": "Enemy#KR2",
                    "riotIdGameName": "Enemy", "riotIdTagLine": "KR2",
                    "team": "CHAOS", "position": "UTILITY", "level": 7,
                },
            ]
        if path.endswith("activeplayer"):
            return {"riotId": "Me#KR1"}
        return {"gameMode": "CLASSIC", "gameTime": 321.5}


class HiddenLiveClient(LiveClient):
    def get(self, path: str):
        if path.endswith("playerlist"):
            return [
                {
                    "championName": "Ornn", "team": "ORDER",
                    "position": "TOP", "level": 2,
                },
                {
                    "championName": "Leona", "team": "CHAOS",
                    "position": "UTILITY", "level": 2,
                },
            ]
        if path.endswith("activeplayer"):
            return {"championName": "Ornn"}
        return {"gameMode": "CLASSIC", "gameTime": 1.0}


class LiveClientTests(unittest.TestCase):
    def test_playerlist_is_split_around_active_players_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            snapshot = FakeLiveClient(registry).snapshot()
            self.assertEqual(snapshot.active_team, "ORDER")
            self.assertEqual(snapshot.allies[0].riot_id, "Me#KR1")
            self.assertEqual(snapshot.enemies[0].champion_id, "Leona")
            self.assertEqual(snapshot.allies[0].champion_id, "Kaisa")
            self.assertAlmostEqual(snapshot.game_time, 321.5)

    def test_hidden_players_receive_unique_non_queryable_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            client = HiddenLiveClient(registry)
            snapshot = client.snapshot()
            self.assertEqual(snapshot.active_team, "ORDER")
            self.assertTrue(snapshot.players[0].is_active_player)
            self.assertEqual(snapshot.players[0].riot_tag_line, "")
            self.assertTrue(snapshot.players[0].riot_game_name.startswith("비공개 ORDER"))
            self.assertTrue(snapshot.players[1].riot_game_name.startswith("비공개 CHAOS"))
            self.assertNotEqual(snapshot.players[0].riot_id, snapshot.players[1].riot_id)

    def test_identity_snapshot_reads_only_the_small_roster_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            paths: list[str] = []
            client = HiddenLiveClient(registry)
            original = client.get
            client.get = lambda path: (paths.append(path), original(path))[1]  # type: ignore[method-assign]
            snapshot = client.identity_snapshot()
            self.assertEqual(len(snapshot.players), 2)
            self.assertEqual(paths, ["/liveclientdata/playerlist"])


if __name__ == "__main__":
    unittest.main()
