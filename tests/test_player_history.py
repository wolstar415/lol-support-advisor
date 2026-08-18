from __future__ import annotations

import unittest

from lol_support_advisor.models import OpggMcpRecentMatch
from lol_support_advisor.player_history import (
    OtherPlayerHistoryPager,
    completed_solo_matches,
    merge_solo_match_pages,
    next_opgg_match_limit,
    normalize_riot_id,
    split_riot_id,
)


def match(
    match_id: str,
    created_at: str,
    *,
    game_type: str = "SOLORANKED",
    result: str = "WIN",
) -> OpggMcpRecentMatch:
    return OpggMcpRecentMatch(
        match_id=match_id,
        created_at=created_at,
        game_type=game_type,
        champion_key=89,
        champion_name="Leona",
        position="SUPPORT",
        result=result,
    )


class PlayerHistoryHelpersTest(unittest.TestCase):
    def test_riot_id_normalization_and_split(self) -> None:
        self.assertEqual(normalize_riot_id("  피카피카츄  # KR1 "), "피카피카츄#kr1")
        self.assertEqual(split_riot_id(" 피카피카츄 # KR1 "), ("피카피카츄", "KR1"))
        self.assertEqual(normalize_riot_id("태그 없음"), "")
        self.assertIsNone(split_riot_id("태그 없음"))

    def test_completed_solo_matches_filters_sorts_and_deduplicates(self) -> None:
        rows = [
            match("2", "2026-08-18T02:00:00"),
            match("1", "2026-08-18T01:00:00"),
            match("3", "2026-08-18T03:00:00", game_type="ARAM"),
            match("4", "2026-08-18T04:00:00", result="REMAKE"),
            match("2", "2026-08-18T02:00:00"),
        ]
        self.assertEqual(
            [item.match_id for item in completed_solo_matches(rows)],
            ["2", "1"],
        )

    def test_merge_and_cumulative_opgg_limits_are_ten_then_twenty(self) -> None:
        first = [match(str(index), f"2026-08-18T00:{index:02d}:00") for index in range(10)]
        second = [match(str(index), f"2026-08-18T00:{index:02d}:00") for index in range(5, 20)]
        merged = merge_solo_match_pages(first, second)
        self.assertEqual(len(merged), 20)
        self.assertEqual(next_opgg_match_limit(0), 10)
        self.assertEqual(next_opgg_match_limit(9), 10)
        self.assertEqual(next_opgg_match_limit(10), 20)
        self.assertIsNone(next_opgg_match_limit(20))

    def test_other_player_pager_requests_exact_ten_and_deduplicates(self) -> None:
        pager = OtherPlayerHistoryPager()
        self.assertEqual(pager.next_request(), (0, 10))
        self.assertEqual(
            pager.accept_page([f"KR_{index}" for index in range(10)], True),
            [f"KR_{index}" for index in range(10)],
        )
        self.assertEqual(pager.next_request(), (10, 10))
        self.assertEqual(
            pager.accept_page(["KR_9", *[f"KR_{index}" for index in range(10, 19)]], True),
            [f"KR_{index}" for index in range(10, 19)],
        )
        self.assertEqual(pager.next_request(), (20, 10))
        pager.accept_page(["KR_20", "KR_21"], False)
        self.assertIsNone(pager.next_request())


if __name__ == "__main__":
    unittest.main()
