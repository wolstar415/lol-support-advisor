from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.lcu import (
    LcuActionError, LcuClient, champ_select_time_left_ms,
    champion_action_in_progress, deferred_ban_due,
    find_local_champion_action, parse_lcu_session,
)


class FakeLcuClient(LcuClient):
    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__()
        self.responses = responses
        self.writes: list[tuple[str, str, object]] = []

    def get(self, path: str) -> object:
        return self.responses[path]

    def patch(self, path: str, payload: object) -> object:
        self.writes.append(("PATCH", path, payload))
        return None

    def request(self, method: str, path: str, payload: object = None) -> object:
        if method.upper() == "POST":
            self.writes.append(("POST", path, payload))
            return None
        return super().request(method, path, payload)


class LcuParsingTests(unittest.TestCase):
    def test_deferred_ban_uses_riot_timer_then_local_fallback(self) -> None:
        session = {
            "timer": {
                "adjustedTimeLeftInPhase": 5200,
                "totalTimeInPhase": 30000,
            }
        }
        self.assertEqual(champ_select_time_left_ms(session), 5200)
        self.assertFalse(deferred_ban_due(session, 5000, 10.0, 9.0))
        session["timer"]["adjustedTimeLeftInPhase"] = 4900
        self.assertTrue(deferred_ban_due(session, 5000, 10.0, 1.0))
        session["timer"]["adjustedTimeLeftInPhase"] = 9000
        self.assertTrue(deferred_ban_due(session, 5000, 10.0, 10.0))

        no_timer: dict = {}
        self.assertIsNone(champ_select_time_left_ms(no_timer))
        self.assertFalse(deferred_ban_due(no_timer, 5000, 12.0, 11.9))
        self.assertTrue(deferred_ban_due(no_timer, 5000, 12.0, 12.0))

    def test_infinite_champ_select_timer_uses_fallback(self) -> None:
        session = {
            "timer": {
                "adjustedTimeLeftInPhase": 1000,
                "isInfinite": True,
            }
        }
        self.assertIsNone(champ_select_time_left_ms(session))
        self.assertFalse(deferred_ban_due(session, 5000, 20.0, 19.0))

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
            self.assertEqual(draft.ally_locked[0].pick_turn, 1)
            self.assertEqual(draft.my_hover.champion_id, "Janna")
            self.assertEqual(draft.my_hover.pick_turn, 2)
            self.assertEqual(draft.ally_hover[0].champion_id, "Jinx")
            self.assertEqual(draft.enemy_locked[0].champion_id, "Leona")
            self.assertEqual(draft.ally_bans, ["Yuumi"])
            self.assertEqual(draft.enemy_bans, ["Thresh"])
            self.assertEqual(draft.ally_ban_actions[0].state, "LOCKED")
            self.assertEqual(draft.enemy_ban_actions[0].order, 1)

    def test_actions_expose_fixed_slots_and_in_progress_bans_before_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            my_team = [
                {"cellId": index, "assignedPosition": position}
                for index, position in enumerate(
                    ["top", "jungle", "middle", "bottom", "utility"]
                )
            ]
            their_team = [
                {"cellId": index + 5, "assignedPosition": position}
                for index, position in enumerate(
                    ["top", "jungle", "middle", "bottom", "utility"]
                )
            ]
            def pick(actor: int, champion_id: int = 0) -> dict:
                return {
                    "type": "pick", "actorCellId": actor,
                    "championId": champion_id, "completed": bool(champion_id),
                }

            # Standard draft sequence: one pick, two picks, two picks, two picks,
            # two picks, one pick. Players in the same group share the same turn.
            pick_actions = [
                [pick(0)],
                [pick(5), pick(6)],
                [pick(1, 64), pick(2)],
                [pick(7), pick(8)],
                [pick(3), pick(4)],
                [pick(9, 89)],
            ]
            session = {
                "localPlayerCellId": 4,
                "myTeam": my_team,
                "theirTeam": their_team,
                "actions": [
                    [{
                        "type": "ban", "actorCellId": 0, "championId": 350,
                        "completed": True, "isInProgress": False,
                    }],
                    [{
                        "type": "ban", "actorCellId": 5, "championId": 412,
                        "completed": False, "isInProgress": True,
                    }],
                    *pick_actions,
                ],
                "bans": {"myTeamBans": [], "theirTeamBans": []},
            }
            draft = parse_lcu_session(session, registry)
            self.assertEqual(len(draft.ally_team_order), 5)
            self.assertEqual(len(draft.enemy_team_order), 5)
            self.assertEqual(
                [member.pick_order for member in draft.ally_team_order],
                [1, 2, 3, 4, 5],
            )
            self.assertEqual(
                [member.pick_turn for member in draft.ally_team_order],
                [1, 3, 3, 5, 5],
            )
            self.assertEqual(
                [member.pick_turn for member in draft.enemy_team_order],
                [2, 2, 4, 4, 6],
            )
            self.assertEqual(draft.ally_bans, ["Yuumi"])
            self.assertEqual(draft.enemy_bans, [])
            self.assertEqual(draft.ally_ban_actions[0].state, "LOCKED")
            self.assertEqual(draft.enemy_ban_actions[0].state, "HOVER")
            self.assertEqual(draft.enemy_ban_actions[0].champion_id, "Thresh")

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


class LcuActionTests(unittest.TestCase):
    @staticmethod
    def session(
        *, local_in_progress: bool = True, local_type: str = "pick",
        local_champion: int = 0, local_completed: bool = False,
    ) -> dict:
        return {
            "localPlayerCellId": 3,
            "myTeam": [{"cellId": 3, "assignedPosition": "utility"}],
            "theirTeam": [{"cellId": 8, "assignedPosition": "utility"}],
            "actions": [[
                {
                    "id": 501, "type": local_type, "actorCellId": 2,
                    "championId": 89, "completed": False, "isInProgress": True,
                },
                {
                    "id": 777, "type": local_type, "actorCellId": 3,
                    "championId": local_champion, "completed": local_completed,
                    "isInProgress": local_in_progress,
                },
            ]],
            "bans": {"myTeamBans": [], "theirTeamBans": []},
        }

    def client(self, session: dict, **responses: object) -> FakeLcuClient:
        defaults: dict[str, object] = {
            "/lol-gameflow/v1/gameflow-phase": "ChampSelect",
            "/lol-champ-select/v1/session": session,
            "/lol-champions/v1/owned-champions-minimal": [
                {"id": 40, "ownership": {"owned": True}},
                {"id": 99, "ownership": {"owned": True}},
            ],
            "/lol-champ-select/v1/pickable-champion-ids": [40, 99],
            "/lol-champ-select/v1/bannable-champion-ids": [40, 99],
        }
        defaults.update(responses)
        return FakeLcuClient(defaults)

    def test_hover_uses_only_local_in_progress_action_and_does_not_complete(self) -> None:
        session = self.session()
        client = self.client(session)
        result = client.perform_champion_action(40, "hover")
        self.assertEqual(result.action_id, 777)
        self.assertEqual(client.writes, [(
            "PATCH", "/lol-champ-select/v1/session/actions/777",
            {"championId": 40, "completed": False},
        )])

    def test_pick_is_blocked_before_patch_when_champion_is_not_owned(self) -> None:
        client = self.client(
            self.session(),
            **{"/lol-champions/v1/owned-champions-minimal": []},
        )
        with self.assertRaisesRegex(LcuActionError, "보유하지 않은"):
            client.perform_champion_action(40, "pick")
        self.assertEqual(client.writes, [])

    def test_pick_is_blocked_before_patch_when_it_is_not_my_turn(self) -> None:
        session = self.session(local_in_progress=False)
        client = self.client(session)
        with self.assertRaisesRegex(LcuActionError, "아직 내 픽 차례"):
            client.perform_champion_action(40, "pick")
        self.assertEqual(client.writes, [])

    def test_banned_champion_is_blocked_before_patch(self) -> None:
        session = self.session()
        session["bans"]["theirTeamBans"] = [40]
        client = self.client(session)
        with self.assertRaisesRegex(LcuActionError, "이미 밴된"):
            client.perform_champion_action(40, "pick")
        self.assertEqual(client.writes, [])

    def test_ban_completes_only_the_local_ban_action(self) -> None:
        session = self.session(local_type="ban")
        client = self.client(session)
        result = client.perform_champion_action(99, "ban")
        self.assertTrue(result.completed)
        self.assertEqual(client.writes[0], (
            "PATCH", "/lol-champ-select/v1/session/actions/777",
            {"championId": 99, "completed": True},
        ))

    def test_local_turn_helpers_never_fall_back_to_a_teammate(self) -> None:
        session = self.session(local_in_progress=False)
        self.assertFalse(champion_action_in_progress(session, "pick"))
        with self.assertRaises(LcuActionError):
            find_local_champion_action(session, "pick", require_in_progress=True)

    def test_ready_check_posts_only_while_response_is_pending(self) -> None:
        path = "/lol-matchmaking/v1/ready-check"
        pending = FakeLcuClient({
            path: {"state": "InProgress", "playerResponse": "None"}
        })
        self.assertTrue(pending.accept_ready_check_if_pending())
        self.assertEqual(pending.writes, [(
            "POST", "/lol-matchmaking/v1/ready-check/accept", None,
        )])
        accepted = FakeLcuClient({
            path: {"state": "InProgress", "playerResponse": "Accepted"}
        })
        self.assertFalse(accepted.accept_ready_check_if_pending())
        self.assertEqual(accepted.writes, [])


if __name__ == "__main__":
    unittest.main()
