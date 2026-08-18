from __future__ import annotations

import unittest

from lol_support_advisor.history import (
    MatchLpChange, analyze_history, attach_match_lp_changes,
)


def history_match(
    match_id: str, champion: str, won: bool, creation: int, kills: int = 2,
    position: str = "UTILITY",
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
                    "teamPosition": position, "win": won, "kills": kills,
                    "deaths": 2, "assists": 10, "visionScore": 40,
                    "totalMinionsKilled": 20, "neutralMinionsKilled": 0,
                    "totalDamageDealtToChampions": 8000, "totalDamageTaken": 5000,
                    "goldEarned": 9000, "item0": 1001, "item1": 2003,
                    "summoner1Id": 4, "summoner2Id": 14,
                    "riotIdGameName": "Me", "riotIdTagline": "KR1",
                    "perks": {"styles": [
                        {"description": "primary", "style": 8200,
                         "selections": [{"perk": 8214}]},
                        {"description": "subStyle", "style": 8300,
                         "selections": [{"perk": 8345}]},
                    ]},
                },
                {
                    "puuid": "ally", "teamId": 100, "championName": "Jinx",
                    "kills": 8, "win": won,
                    "riotIdGameName": "Ally", "riotIdTagline": "KR2",
                },
                {
                    "puuid": "enemy", "teamId": 200, "championName": "Leona",
                    "kills": 3, "win": not won,
                    "riotIdGameName": "Enemy", "riotIdTagline": "KR3",
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
        self.assertEqual(overview.entries[0].summoner_spell_ids, (4, 14))
        self.assertEqual(overview.entries[0].primary_rune_id, 8214)
        self.assertEqual(overview.entries[0].secondary_rune_style_id, 8300)
        self.assertEqual(overview.entries[0].ally_players[0][1], "Me#KR1")
        self.assertEqual(overview.entries[0].cs_per_minute, 1.0)
        self.assertEqual(overview.champions[0].champion_id, "Janna")
        self.assertEqual(overview.champions[0].games, 2)

    def test_empty_history(self) -> None:
        overview = analyze_history([], "mine")
        self.assertEqual(overview.games, 0)
        self.assertIsNone(overview.win_rate)
        self.assertEqual(overview.current_streak, 0)

    def test_champion_performance_is_split_by_position(self) -> None:
        overview = analyze_history([
            history_match("KR_SUP_2", "Briar", True, 4000, position="UTILITY"),
            history_match("KR_SUP_1", "Briar", False, 3000, position="UTILITY"),
            history_match("KR_JGL_2", "Briar", True, 2000, position="JUNGLE"),
            history_match("KR_JGL_1", "Briar", True, 1000, position="JUNGLE"),
        ], "mine")
        briar = {
            stat.position: stat for stat in overview.champions
            if stat.champion_id == "Briar"
        }
        self.assertEqual(set(briar), {"SUPPORT", "JUNGLE"})
        self.assertEqual((briar["SUPPORT"].games, briar["SUPPORT"].wins), (2, 1))
        self.assertEqual((briar["JUNGLE"].games, briar["JUNGLE"].wins), (2, 2))

    def test_recent_twenty_summary_has_top_three_champions(self) -> None:
        matches = [
            history_match(
                f"KR_{index}",
                "Janna" if index < 9 else "Braum" if index < 16 else "Lulu",
                index % 2 == 0,
                30_000 - index,
            )
            for index in range(25)
        ]
        overview = analyze_history(matches, "mine")

        self.assertEqual(overview.recent_20_games, 20)
        self.assertEqual(
            [(stat.champion_id, stat.games) for stat in overview.recent_20_champions],
            [("Janna", 9), ("Braum", 7), ("Lulu", 4)],
        )
        self.assertIsNone(overview.recent_20_lp_sum)
        self.assertEqual(overview.recent_20_lp_known_games, 0)

    def test_lp_attachment_keeps_unknown_games_out_of_recent_sum(self) -> None:
        overview = analyze_history([
            history_match("KR_3", "Janna", True, 3000),
            history_match("KR_2", "Janna", False, 2000),
            history_match("KR_1", "Braum", True, 1000),
        ], "mine")

        def change(
            match_id: str, delta: int | None, confidence: str,
        ) -> MatchLpChange:
            return MatchLpChange(
                match_id=match_id, puuid="mine", before_snapshot_id=1,
                after_snapshot_id=2, before_tier="GOLD",
                before_division="I", before_lp=40, after_tier="GOLD",
                after_division="I", after_lp=40 + (delta or 0),
                lp_delta=delta, confidence=confidence,
                resolved_at="2026-08-18T12:00:00",
            )

        attach_match_lp_changes(overview, {
            "KR_3": change("KR_3", 23, "EXACT"),
            "KR_2": change("KR_2", -19, "EXACT"),
            "KR_1": change("KR_1", None, "TRANSITION"),
        })

        self.assertEqual(overview.recent_20_lp_sum, 4)
        self.assertEqual(overview.recent_20_lp_known_games, 2)
        self.assertEqual(overview.recent_20_lp_inferred_games, 0)
        self.assertIsNone(overview.entries[2].lp_delta)
        self.assertEqual(overview.entries[2].lp_confidence, "TRANSITION")


if __name__ == "__main__":
    unittest.main()
