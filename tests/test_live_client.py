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


if __name__ == "__main__":
    unittest.main()
