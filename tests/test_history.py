from __future__ import annotations

import unittest

from lol_support_advisor.history import analyze_history


def history_match(
    match_id: str, champion: str, won: bool, creation: int, kills: int = 2,
) -> dict:
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameCreation": creation,
            "gameDuration": 1200,
            "queueId": 420,
            "participants": [
                {
                    "puuid": "mine", "teamId": 100, "championName": champion,
                    "teamPosition": "UTILITY", "win": won, "kills": kills,
                    "deaths": 2, "assists": 10, "visionScore": 40,
                    "totalMinionsKilled": 20, "neutralMinionsKilled": 0,
                    "totalDamageDealtToChampions": 8000, "totalDamageTaken": 5000,
                    "goldEarned": 9000, "item0": 1001, "item1": 2003,
                },
                {
                    "puuid": "ally", "teamId": 100, "championName": "Jinx",
                    "kills": 8, "win": won,
                },
                {
                    "puuid": "enemy", "teamId": 200, "championName": "Leona",
                    "kills": 3, "win": not won,
                },
            ],
        },
    }


class HistoryAnalysisTests(unittest.TestCase):
    def test_history_summary_and_entries(self) -> None:
        overview = analyze_history([
            history_match("KR_3", "Janna", True, 3000),
            history_match("KR_2", "Janna", True, 2000),
            history_match("KR_1", "Braum", False, 1000),
        ], "mine")
        self.assertEqual((overview.games, overview.wins), (3, 2))
        self.assertEqual(overview.current_streak, 2)
        self.assertAlmostEqual(overview.win_rate or 0, 66.666, places=2)
        self.assertEqual(overview.entries[0].match_id, "KR_3")
        self.assertEqual(overview.entries[0].items, (1001, 2003))
        self.assertEqual(overview.entries[0].cs_per_minute, 1.0)
        self.assertEqual(overview.champions[0].champion_id, "Janna")
        self.assertEqual(overview.champions[0].games, 2)

    def test_empty_history(self) -> None:
        overview = analyze_history([], "mine")
        self.assertEqual(overview.games, 0)
        self.assertIsNone(overview.win_rate)
        self.assertEqual(overview.current_streak, 0)


if __name__ == "__main__":
    unittest.main()
