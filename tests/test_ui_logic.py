from __future__ import annotations

import unittest
from types import SimpleNamespace

from lol_support_advisor.ui import (
    AdvisorApp, candidate_score, support_archetype, team_objective_counts,
)
from lol_support_advisor.icons import ItemIconCache
from lol_support_advisor.models import OpggCounter, PersonalStat, PlayerProfileStat


class DuoEvidenceTests(unittest.TestCase):
    def test_mousewheel_routes_to_match_detail_canvas(self) -> None:
        calls: list[tuple[int, str]] = []

        class Canvas:
            @staticmethod
            def winfo_exists() -> bool:
                return True

            @staticmethod
            def yview_scroll(amount: int, unit: str) -> None:
                calls.append((amount, unit))

        top = SimpleNamespace(_advisor_scroll_canvas=Canvas())
        widget = SimpleNamespace(winfo_toplevel=lambda: top)
        AdvisorApp._on_mousewheel(
            SimpleNamespace(), SimpleNamespace(widget=widget, delta=-120, num=0)
        )
        self.assertEqual(calls, [(3, "units")])

    def test_match_detail_objectives_include_grubs_and_herald(self) -> None:
        counts = team_objective_counts({"objectives": {
            "horde": {"kills": 5}, "riftHerald": {"kills": 1},
            "dragon": {"kills": 2}, "baron": {"kills": 1},
            "tower": {"kills": 7},
        }})
        self.assertEqual(counts["void_grubs"], 5)
        self.assertEqual(counts["rift_heralds"], 1)

    def test_item_description_html_is_readable(self) -> None:
        text = ItemIconCache._plain_description(
            "<mainText><stats>체력 +400</stats><br><passive>회복 효과</passive></mainText>"
        )
        self.assertIn("체력 +400", text)
        self.assertIn("회복 효과", text)
        self.assertNotIn("<", text)
    def test_duo_evidence_levels(self) -> None:
        self.assertEqual(
            AdvisorApp._classify_duo_evidence([(0, 0), (1, 1)])[0], "매우 유력"
        )
        self.assertEqual(AdvisorApp._classify_duo_evidence([(0, 0)])[0], "유력")
        self.assertEqual(
            AdvisorApp._classify_duo_evidence([(4, 8), (5, 9)])[0], "유력"
        )
        self.assertEqual(
            AdvisorApp._classify_duo_evidence([(2, 8), (9, 20)])[0], "가능"
        )
        self.assertIsNone(AdvisorApp._classify_duo_evidence([(3, 7)]))

    def test_previous_game_kda(self) -> None:
        profile = PlayerProfileStat(
            last_game_champion_id="Nami",
            last_game_kills=2,
            last_game_deaths=4,
            last_game_assists=18,
        )
        self.assertEqual(profile.last_game_kda, 5.0)

    def test_support_archetype_filters(self) -> None:
        self.assertEqual(support_archetype("Janna"), "UTILITY")
        self.assertEqual(support_archetype("Leona"), "ENGAGE")
        self.assertEqual(support_archetype("Xerath"), "POKE")
        self.assertEqual(support_archetype("Garen"), "OTHER")

    def test_candidate_score_uses_local_evidence_and_confidence(self) -> None:
        counter = OpggCounter("Janna", "잔나", 53.0, 6000)
        base_score, base_confidence = candidate_score(counter)
        personal = PersonalStat(
            games=20, wins=14, losses=6, win_rate=70.0,
            matchup_games=10, matchup_wins=7, matchup_losses=3, matchup_win_rate=70.0,
        )
        combined_score, combined_confidence = candidate_score(counter, personal)
        self.assertGreater(combined_score, base_score)
        self.assertEqual(base_confidence, "낮음")
        self.assertEqual(combined_confidence, "높음")


if __name__ == "__main__":
    unittest.main()
