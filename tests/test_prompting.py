from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.models import DraftMember, DraftSnapshot
from lol_support_advisor.prompting import ResponseError, StaleResponseError, build_prompt, parse_response


class PromptingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.registry = ChampionRegistry(Path(self.temp_dir.name) / "champions.json")
        self.draft = DraftSnapshot(
            my_pick_order=2,
            ally_hover=[DraftMember("Jinx", "징크스", "BOTTOM", "HOVER")],
            enemy_locked=[DraftMember("Leona", "레오나", "SUPPORT", "LOCKED")],
            ally_bans=["Yuumi"],
            selected_enemy_support_id="Leona",
            selected_enemy_support_name_ko="레오나",
            selected_enemy_support_source="MANUAL_ENEMY_SUPPORT",
        )
        self.draft.refresh_snapshot_id()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _response(self, snapshot_id: str | None = None, first: str = "Janna") -> str:
        champions = [first, "Braum", "Taric"]
        payload = {
            "schema_version": 2,
            "snapshot_id": snapshot_id or self.draft.snapshot_id,
            "draft_mode": "MATCHUP",
            "recommendations": [
                {
                    "rank": index,
                    "champion_id": champion,
                    "champion_name_ko": self.registry.ko_name(champion),
                    "style": "보호형",
                    "blind_safety": "높음",
                    "reason": "이유",
                    "team_synergy": "조합",
                    "lane_plan": "운영",
                    "watch_for": "주의",
                }
                for index, champion in enumerate(champions, start=1)
            ],
        }
        return f"LOL_SUPPORT_V2\n{json.dumps(payload, ensure_ascii=False)}\nEND_LOL_SUPPORT_V2"

    def test_prompt_contains_hover_bans_and_snapshot(self) -> None:
        prompt = build_prompt(self.draft, None)
        self.assertIn('"state": "HOVER"', prompt)
        self.assertIn('"Yuumi"', prompt)
        self.assertIn(self.draft.snapshot_id, prompt)
        self.assertIn("OP.GG 캐시가 없으므로 숫자를 추측하지 말 것", prompt)

    def test_valid_response_is_parsed(self) -> None:
        result = parse_response(self._response(), self.draft, self.registry)
        self.assertEqual([item.champion_id for item in result], ["Janna", "Braum", "Taric"])

    def test_auto_support_is_marked_tentative(self) -> None:
        self.draft.selected_enemy_support_source = "AUTO_ENEMY_SUPPORT"
        prompt = build_prompt(self.draft, None)
        self.assertIn('"enemy_support_certainty": "TENTATIVE"', prompt)
        self.assertIn('"draft_mode": "MATCHUP_TENTATIVE"', prompt)

    def test_unknown_support_forces_blind_instruction(self) -> None:
        self.draft.selected_enemy_support_id = None
        self.draft.selected_enemy_support_name_ko = "모르겠음"
        self.draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
        prompt = build_prompt(self.draft, None)
        self.assertIn('"enemy_support_certainty": "UNKNOWN"', prompt)
        self.assertIn("적 서포터를 모르므로 임의 확정하지 말고", prompt)
        self.assertIn('"draft_mode": "BLIND"', prompt)

    def test_jungle_role_changes_prompt_and_lane_opponent_contract(self) -> None:
        self.draft.my_role = "JUNGLE"
        self.draft.selected_enemy_support_id = None
        self.draft.selected_enemy_support_name_ko = "모르겠음"
        self.draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
        prompt = build_prompt(self.draft, None)
        self.assertIn("정글 픽 추천 분석기", prompt)
        self.assertIn('"my_role": "JUNGLE"', prompt)
        self.assertIn('"selected_lane_opponent"', prompt)
        self.assertIn("적 정글 챔피언을 모르므로", prompt)

    def test_stale_response_is_rejected(self) -> None:
        with self.assertRaises(StaleResponseError):
            parse_response(self._response("DRAFT-OLD"), self.draft, self.registry)

    def test_unavailable_champion_is_rejected(self) -> None:
        with self.assertRaises(ResponseError):
            parse_response(self._response(first="Yuumi"), self.draft, self.registry)


if __name__ == "__main__":
    unittest.main()
