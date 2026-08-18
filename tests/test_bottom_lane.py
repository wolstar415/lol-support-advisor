from __future__ import annotations

import unittest

from lol_support_advisor.bottom_lane import analyze_bottom_lane


class BottomLaneAnalysisTests(unittest.TestCase):
    def test_level_two_engage_pair_is_not_claimed_as_level_one_win(self) -> None:
        result = analyze_bottom_lane(
            "Samira", "Leona", "Vayne", "Sona",
        )

        self.assertEqual(result.level_results[0], "EVEN")
        self.assertEqual(result.level_results[1:3], ("WIN", "WIN"))
        self.assertEqual(result.style, "AGGRESSIVE")
        self.assertEqual(result.confidence, "MEDIUM")

    def test_scaling_lane_is_told_to_play_safe_into_early_pressure(self) -> None:
        result = analyze_bottom_lane(
            "Vayne", "Yuumi", "Draven", "Nautilus",
        )

        self.assertEqual(result.level_results[:3], ("LOSE", "LOSE", "LOSE"))
        self.assertEqual(result.style, "SAFE")

    def test_external_laning_rate_only_breaks_a_close_result(self) -> None:
        result = analyze_bottom_lane(
            "Ashe", "Rakan", "Kaisa", "Camille",
            ally_laning_win_rates=(54.0, 53.0),
        )

        self.assertEqual(result.level_results[:3], ("EVEN", "EVEN", "EVEN"))
        self.assertEqual(result.style, "AGGRESSIVE")
        self.assertEqual(result.confidence, "HIGH")

    def test_unknown_off_role_pick_lowers_confidence(self) -> None:
        result = analyze_bottom_lane(
            "Ashe", "UnknownSupport", "Kaisa", "Camille",
        )

        self.assertEqual(result.known_champions, 3)
        self.assertEqual(result.confidence, "LOW")

    def test_plan_explains_when_early_pressure_ends(self) -> None:
        result = analyze_bottom_lane(
            "Draven", "Pantheon", "MissFortune", "Fiddlesticks",
        )

        self.assertEqual(result.level_results[:3], ("WIN", "WIN", "WIN"))
        self.assertEqual(result.level_results[3], "LOSE")
        self.assertEqual(result.timing, "EARLY_THEN_SAFE")


if __name__ == "__main__":
    unittest.main()
