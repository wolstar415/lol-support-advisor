from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.storage import Storage


def match_payload(
    match_id: str, my_win: bool, my_champion: str, enemy_support: str,
    game_creation: int = 1000,
) -> dict:
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameCreation": game_creation,
            "queueId": 420,
            "participants": [
                {
                    "puuid": "mine", "teamId": 100, "teamPosition": "UTILITY",
                    "championName": my_champion, "win": my_win, "kills": 2,
                    "deaths": 2, "assists": 10, "visionScore": 50,
                    "riotIdGameName": "Me", "riotIdTagline": "KR1",
                },
                {
                    "puuid": "enemy", "teamId": 200, "teamPosition": "UTILITY",
                    "championName": enemy_support, "win": not my_win,
                    "riotIdGameName": "Enemy", "riotIdTagline": "KR2",
                },
                {
                    "puuid": "ally", "teamId": 100, "teamPosition": "BOTTOM",
                    "championName": "Jinx", "win": my_win,
                    "riotIdGameName": "Ally", "riotIdTagline": "KR3",
                },
            ],
        },
    }


class StorageTests(unittest.TestCase):
    def test_cooldown_and_personal_matchup_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            now = datetime(2026, 8, 17, 12, 0, 0)
            storage.mark_opgg_success(now)
            remaining = storage.opgg_cooldown_remaining(now + timedelta(minutes=20))
            self.assertEqual(int(remaining.total_seconds()), 40 * 60)
            storage.mark_riot_sync(now)
            riot_remaining = storage.riot_sync_cooldown_remaining(now + timedelta(minutes=4))
            self.assertEqual(int(riot_remaining.total_seconds()), 6 * 60)
            storage.save_matches([
                match_payload("KR_1", True, "Janna", "Leona"),
                match_payload("KR_2", False, "Janna", "Leona"),
                match_payload("KR_3", True, "Braum", "Leona"),
            ])
            stat = storage.personal_stat("mine", "Janna", "Leona")
            self.assertEqual((stat.games, stat.wins, stat.losses), (2, 1, 1))
            self.assertEqual((stat.matchup_games, stat.matchup_wins), (2, 1))
            self.assertAlmostEqual(stat.kda or 0, 6.0)
            batch = storage.personal_stats("mine", ["Janna", "Braum"], "Leona")
            self.assertEqual((batch["Janna"].games, batch["Braum"].games), (2, 1))
            self.assertEqual(storage.relationship_record("mine", "ally"), (3, 2, 0, 0))
            self.assertEqual(storage.relationship_record("mine", "enemy"), (0, 0, 3, 2))
            self.assertEqual(storage.player_champion_record("enemy", "Leona"), (3, 1))
            self.assertEqual(storage.find_puuid_by_riot_id("Enemy#KR2"), "enemy")
            self.assertEqual(storage.pair_same_team_games("mine", "ally"), 3)
            self.assertIn(("Enemy", "KR2"), storage.recent_riot_ids("mine"))
            self.assertEqual(storage.count_player_matches("mine"), 3)
            self.assertEqual(
                {match["metadata"]["matchId"] for match in storage.player_matches("mine")},
                {"KR_1", "KR_2", "KR_3"},
            )

    def test_relationship_summary_includes_recency_and_last_meeting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            storage.save_matches([
                match_payload("KR_OLD", False, "Braum", "Leona", 1000),
                match_payload("KR_MID", True, "Janna", "Leona", 2000),
                match_payload("KR_NEW", True, "Nami", "Leona", 3000),
            ])
            ally = storage.relationship_summary("mine", "ally")
            self.assertEqual((ally["together_games"], ally["together_wins"]), (3, 2))
            self.assertEqual(ally["last_met_game_number"], 1)
            self.assertTrue(ally["last_met_same_team"])
            self.assertTrue(ally["last_met_my_win"])
            self.assertEqual(ally["last_met_my_champion_id"], "Nami")
            self.assertEqual(ally["last_met_other_champion_id"], "Jinx")
            self.assertEqual(ally["recent_10_together_games"], 3)
            latest = storage.latest_player_match("mine")
            self.assertIsNotNone(latest)
            self.assertEqual(latest["champion_id"], "Nami")
            self.assertEqual((latest["kills"], latest["deaths"], latest["assists"]), (2, 2, 10))
            self.assertTrue(latest["won"])

    def test_development_key_refresh_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            saved_at = datetime(2026, 8, 17, 12, 0, 0)
            storage.set_riot_api_key("local-development-key", saved_at)
            remaining = storage.riot_api_key_refresh_remaining(
                saved_at + timedelta(hours=23)
            )
            self.assertEqual(int(remaining.total_seconds()), 60 * 60)
            self.assertFalse(storage.riot_api_key_needs_refresh(saved_at + timedelta(hours=23)))
            self.assertTrue(storage.riot_api_key_needs_refresh(saved_at + timedelta(hours=24)))
            storage.mark_riot_api_key_invalid()
            self.assertEqual(storage.riot_api_key_refresh_remaining(saved_at), timedelta(0))


if __name__ == "__main__":
    unittest.main()
