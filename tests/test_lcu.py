from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.lcu import parse_lcu_session


class LcuParsingTests(unittest.TestCase):
    def test_locked_hover_bans_and_pick_order_are_separated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            session = {
                "localPlayerCellId": 1,
                "myTeam": [
                    {"cellId": 0, "assignedPosition": "jungle"},
                    {"cellId": 1, "assignedPosition": "utility"},
                    {"cellId": 2, "assignedPosition": "bottom"},
                ],
                "theirTeam": [
                    {"cellId": 5, "assignedPosition": "utility", "championId": 89},
                ],
                "actions": [
                    [{"type": "pick", "actorCellId": 0, "championId": 64, "completed": True}],
                    [
                        {"type": "pick", "actorCellId": 1, "championId": 40,
                         "completed": False, "isInProgress": True},
                        {"type": "pick", "actorCellId": 2, "championId": 222,
                         "completed": False, "isInProgress": False},
                    ],
                ],
                "bans": {"myTeamBans": [350], "theirTeamBans": [412]},
            }
            draft = parse_lcu_session(session, registry)
            self.assertEqual(draft.my_role, "SUPPORT")
            self.assertEqual(draft.my_pick_order, 2)
            self.assertEqual(draft.my_status, "SELECTING")
            self.assertEqual(draft.ally_locked[0].champion_id, "LeeSin")
            self.assertEqual(draft.my_hover.champion_id, "Janna")
            self.assertEqual(draft.ally_hover[0].champion_id, "Jinx")
            self.assertEqual(draft.enemy_locked[0].champion_id, "Leona")
            self.assertEqual(draft.ally_bans, ["Yuumi"])
            self.assertEqual(draft.enemy_bans, ["Thresh"])

    def test_local_assigned_position_drives_recommendation_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            draft = parse_lcu_session({
                "localPlayerCellId": 3,
                "myTeam": [{"cellId": 3, "assignedPosition": "jungle"}],
                "theirTeam": [
                    {"cellId": 8, "assignedPosition": "jungle", "championId": 120}
                ],
                "actions": [],
                "bans": {},
            }, registry)
            self.assertEqual(draft.my_role, "JUNGLE")
            self.assertEqual(draft.enemy_locked[0].role, "JUNGLE")


if __name__ == "__main__":
    unittest.main()
