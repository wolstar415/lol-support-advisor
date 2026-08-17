from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.models import (
    DraftMember, DraftSnapshot, OpggCounter, OpggSnapshot,
    OpggSynergySnapshot, OpggSynergyStat, PersonalStat,
)
from lol_support_advisor.prompting import (
    ResponseError, StaleResponseError, build_memory_prompt, build_prompt, parse_response,
)


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

    @staticmethod
    def _query_payload(prompt: str) -> dict:
        body = prompt.split("LOL_PICK_QUERY_V4\n", 1)[1].split(
            "\nEND_LOL_PICK_QUERY_V4", 1
        )[0]
        return json.loads(body)

    def test_prompt_contains_hover_bans_and_snapshot(self) -> None:
        prompt = build_prompt(self.draft, None)
        payload = self._query_payload(prompt)
        self.assertIn(["Jinx", "BOTTOM", "HOVER", None, None], payload["ally"])
        self.assertEqual(payload["bans"]["ally"], ["Yuumi"])
        self.assertEqual(payload["snapshot_id"], self.draft.snapshot_id)
        self.assertIn("숫자를 추측하지 말 것", payload["opgg"]["notice"])
        self.assertLess(len(prompt), 2500)
        self.assertNotIn('"recommendations"', prompt)

    def test_memory_prompt_contains_rules_and_response_contract(self) -> None:
        prompt = build_memory_prompt()
        self.assertIn("LOL_PICK_MEMORY_V4", prompt)
        self.assertIn("LOCKED는 확정", prompt)
        self.assertIn("아군 원딜 궁합", prompt)
        self.assertIn("LOL_SUPPORT_V2", prompt)
        self.assertIn('"recommendations"', prompt)

    def test_prompt_includes_position_meta_separately_from_matchup(self) -> None:
        meta = OpggSnapshot(
            enemy_support_id=None, enemy_support_name_ko=None,
            counters=[
                OpggCounter(
                    "Thresh", "쓰레쉬", 52.18, 0, overall_win_rate=52.18,
                    position_rank=1, pick_rate=13.82, ban_rate=9.3,
                )
            ],
        )
        prompt = build_prompt(self.draft, None, meta)
        payload = self._query_payload(prompt)
        self.assertEqual(
            payload["opgg"]["position_meta"][0],
            [1, "Thresh", 52.18, 13.82, 9.3, "O"],
        )

    def test_prompt_respects_configured_meta_display_count(self) -> None:
        meta = OpggSnapshot(
            enemy_support_id=None, enemy_support_name_ko=None,
            counters=[
                OpggCounter(
                    f"Champion{index}", f"챔피언{index}", 50.0, 1000,
                    overall_win_rate=50.0 + index / 10,
                    position_rank=index,
                )
                for index in range(1, 8)
            ],
        )

        payload = self._query_payload(
            build_prompt(self.draft, None, meta, meta_limit=5)
        )

        self.assertEqual(len(payload["opgg"]["position_meta"]), 5)
        self.assertEqual(payload["opgg"]["position_meta"][-1][1], "Champion5")

    def test_support_prompt_includes_opgg_and_local_adc_synergy_separately(self) -> None:
        synergy = OpggSynergySnapshot(
            ally_champion_key=222, ally_champion_id="Jinx",
            ally_champion_name_ko="징크스", fetched_at="2026-08-18T04:00:00",
            synergies=[OpggSynergyStat(
                champion_key=412, champion_id="Thresh",
                champion_name_ko="쓰레쉬", games=3817, wins=2122,
                win_rate=56.0, synergy_rank=1, synergy_tier=1,
            )], status="OK",
        )
        local = {
            "Thresh": PersonalStat(
                ally_adc_games=5, ally_adc_wins=4, ally_adc_losses=1,
                ally_adc_win_rate=80.0,
            )
        }
        payload = self._query_payload(
            build_prompt(self.draft, None, None, synergy, local)
        )
        combo = payload["ally_adc_synergy"]
        self.assertEqual(combo["ally_adc"], "Jinx")
        self.assertEqual(combo["ally_adc_state"], "HOVER")
        self.assertEqual(combo["candidates"][0], ["Thresh", 56.0, 3817, 1, 1])
        self.assertEqual(combo["my_local_combos"][0], ["Thresh", 80.0, 5])
        self.assertIn("team_engage_and_peel", payload["decision_focus"])

    def test_valid_response_is_parsed(self) -> None:
        result = parse_response(self._response(), self.draft, self.registry)
        self.assertEqual([item.champion_id for item in result], ["Janna", "Braum", "Taric"])

    def test_auto_support_is_marked_tentative(self) -> None:
        self.draft.selected_enemy_support_source = "AUTO_ENEMY_SUPPORT"
        prompt = build_prompt(self.draft, None)
        payload = self._query_payload(prompt)
        self.assertEqual(payload["enemy_support_certainty"], "TENTATIVE")
        self.assertEqual(payload["draft_mode"], "MATCHUP_TENTATIVE")

    def test_unknown_support_forces_blind_instruction(self) -> None:
        self.draft.selected_enemy_support_id = None
        self.draft.selected_enemy_support_name_ko = "모르겠음"
        self.draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
        prompt = build_prompt(self.draft, None)
        payload = self._query_payload(prompt)
        self.assertEqual(payload["enemy_support_certainty"], "UNKNOWN")
        self.assertIn("적 서포터를 모르므로 임의 확정하지 말고", payload["opponent"]["instruction"])
        self.assertEqual(payload["draft_mode"], "BLIND")

    def test_jungle_role_changes_prompt_and_lane_opponent_contract(self) -> None:
        self.draft.my_role = "JUNGLE"
        self.draft.selected_enemy_support_id = None
        self.draft.selected_enemy_support_name_ko = "모르겠음"
        self.draft.selected_enemy_support_source = "MANUAL_UNKNOWN"
        prompt = build_prompt(self.draft, None)
        payload = self._query_payload(prompt)
        self.assertEqual(payload["role"], "JUNGLE")
        self.assertEqual(payload["selected_lane_opponent"]["position"], "JUNGLE")
        self.assertIn("적 정글 챔피언을 모르므로", payload["opponent"]["instruction"])

    def test_stale_response_is_rejected(self) -> None:
        with self.assertRaises(StaleResponseError):
            parse_response(self._response("DRAFT-OLD"), self.draft, self.registry)

    def test_unavailable_champion_is_rejected(self) -> None:
        with self.assertRaises(ResponseError):
            parse_response(self._response(first="Yuumi"), self.draft, self.registry)


if __name__ == "__main__":
    unittest.main()
