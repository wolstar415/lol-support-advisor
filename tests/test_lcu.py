from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from lol_support_advisor.champions import ChampionRegistry
from lol_support_advisor.lcu import (
    LcuActionError, LcuActionStateChanged, LcuClient, LcuUnavailable,
    champ_select_time_left_ms, champ_select_timer_phase,
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
        if method.upper() in {"POST", "DELETE"}:
            self.writes.append((method.upper(), path, payload))
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
        self.assertFalse(deferred_ban_due(session, 5000, 10.0, 10.0))

        no_timer: dict = {}
        self.assertIsNone(champ_select_time_left_ms(no_timer))
        self.assertFalse(deferred_ban_due(no_timer, 5000, 12.0, 11.9))
        self.assertTrue(deferred_ban_due(no_timer, 5000, 12.0, 12.0))

    def test_champ_select_timer_phase_is_normalized(self) -> None:
        self.assertEqual(
            champ_select_timer_phase({"timer": {"phase": " ban_pick "}}),
            "BAN_PICK",
        )
        self.assertEqual(champ_select_timer_phase({"timer": {}}), "")
        self.assertEqual(champ_select_timer_phase({}), "")

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

    def test_accepted_pick_order_swap_moves_local_player_from_fifth_to_third(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")

            def pick(actor: int) -> dict:
                return {
                    "type": "pick", "actorCellId": actor, "championId": 0,
                    "completed": False, "isInProgress": False,
                }

            session = {
                "localPlayerCellId": 4,
                "myTeam": [
                    {"cellId": 0, "assignedPosition": "top"},
                    {"cellId": 1, "assignedPosition": "jungle"},
                    {"cellId": 2, "assignedPosition": "middle"},
                    {"cellId": 3, "assignedPosition": "bottom"},
                    {"cellId": 4, "assignedPosition": "utility"},
                ],
                "theirTeam": [
                    {"cellId": 5 + index, "assignedPosition": position}
                    for index, position in enumerate(
                        ("top", "jungle", "middle", "bottom", "utility")
                    )
                ],
                # Pick slots remain fixed when Riot accepts the exchange.
                "actions": [
                    [pick(0)], [pick(5), pick(6)], [pick(1), pick(2)],
                    [pick(7), pick(8)], [pick(3), pick(4)], [pick(9)],
                ],
                "pickOrderSwaps": [{"id": 9, "cellId": 2, "state": "SENT"}],
                "bans": {},
            }
            before = parse_lcu_session(session, registry)
            self.assertEqual(before.my_pick_order, 5)
            self.assertEqual(before.my_role, "SUPPORT")
            self.assertEqual(before.pick_order_swap_state, "SENT")
            self.assertEqual(before.pick_order_swap_target_cell_id, 2)

            accepted = deepcopy(session)
            accepted["localPlayerCellId"] = 2
            accepted["myTeam"][2]["assignedPosition"] = "utility"
            accepted["myTeam"][4]["assignedPosition"] = "middle"
            accepted["pickOrderSwaps"] = [
                {"id": 20 + cell_id, "cellId": cell_id, "state": "AVAILABLE"}
                for cell_id in (0, 1, 3, 4)
            ]
            after = parse_lcu_session(accepted, registry)

            self.assertEqual(after.local_player_cell_id, 2)
            self.assertEqual(after.my_pick_order, 3)
            self.assertEqual(after.my_role, "SUPPORT")
            self.assertEqual(after.pick_order_swap_state, "")
            self.assertNotEqual(before.snapshot_id, after.snapshot_id)

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

    def test_planning_champion_pick_intent_is_local_hover(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ChampionRegistry(Path(temp_dir) / "champions.json")
            draft = parse_lcu_session({
                "localPlayerCellId": 4,
                "myTeam": [{
                    "cellId": 4,
                    "assignedPosition": "utility",
                    "championId": 0,
                    "championPickIntent": 54,
                }],
                "theirTeam": [],
                "actions": [],
                "bans": {},
                "timer": {"phase": "PLANNING"},
            }, registry)

            self.assertIsNotNone(draft.my_hover)
            self.assertEqual(draft.my_hover.champion_id, "Malphite")
            self.assertEqual(draft.my_hover.state, "HOVER")
            self.assertEqual(draft.my_status, "SELECTING")


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
            "timer": {
                "phase": "BAN_PICK",
                "adjustedTimeLeftInPhase": 10000,
            },
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

    def test_hover_can_select_on_the_local_future_action_before_my_turn(self) -> None:
        session = self.session(local_in_progress=False)
        client = self.client(
            session,
            **{"/lol-champ-select/v1/pickable-champion-ids": []},
        )

        result = client.perform_champion_action(40, "hover")

        self.assertFalse(result.completed)
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

    def test_auto_ban_can_stage_without_bannable_endpoint_gate(self) -> None:
        session = self.session(local_type="ban")
        client = self.client(
            session,
            **{"/lol-champ-select/v1/bannable-champion-ids": []},
        )

        result = client.perform_champion_action(
            99,
            "ban_hover",
            expected_action_id=777,
            expected_current_champion_ids={0, 99},
            verify_bannable=False,
        )

        self.assertFalse(result.completed)
        self.assertEqual(client.writes, [(
            "PATCH", "/lol-champ-select/v1/session/actions/777",
            {"championId": 99, "completed": False},
        )])

    def test_auto_ban_can_commit_without_bannable_endpoint_gate(self) -> None:
        session = self.session(local_type="ban", local_champion=99)
        client = self.client(
            session,
            **{"/lol-champ-select/v1/bannable-champion-ids": []},
        )

        result = client.perform_champion_action(
            99,
            "ban",
            expected_action_id=777,
            expected_current_champion_ids={0, 99},
            verify_bannable=False,
        )

        self.assertTrue(result.completed)
        self.assertEqual(client.writes[-1], (
            "PATCH", "/lol-champ-select/v1/session/actions/777",
            {"championId": 99, "completed": True},
        ))

    def test_auto_ban_never_overwrites_a_manual_ban_choice(self) -> None:
        session = self.session(local_type="ban", local_champion=40)
        client = self.client(session)

        with self.assertRaisesRegex(LcuActionStateChanged, "사용자가 다른"):
            client.perform_champion_action(
                99,
                "ban",
                expected_action_id=777,
                expected_current_champion_ids={0, 99},
                verify_bannable=False,
            )

        self.assertEqual(client.writes, [])

    def test_ban_is_blocked_during_planning_even_if_action_is_in_progress(self) -> None:
        session = self.session(local_type="ban")
        session["timer"]["phase"] = "PLANNING"
        session["timer"]["adjustedTimeLeftInPhase"] = 7500
        client = self.client(session)

        with self.assertRaisesRegex(LcuActionStateChanged, "실제 밴 단계"):
            client.perform_champion_action(99, "ban")

        self.assertEqual(client.writes, [])

    def test_scheduled_ban_is_blocked_if_local_action_id_changed(self) -> None:
        session = self.session(local_type="ban")
        client = self.client(session)

        with self.assertRaisesRegex(LcuActionStateChanged, "작업이 변경"):
            client.perform_champion_action(
                99, "ban", expected_action_id=123,
            )

        self.assertEqual(client.writes, [])

    def test_scheduled_ban_rechecks_cancellation_before_patch(self) -> None:
        client = self.client(self.session(local_type="ban"))
        checks = 0

        def still_current() -> bool:
            nonlocal checks
            checks += 1
            # The monitor is current while it waits for the write lock, then
            # is cancelled while the read-only LCU preflight is in flight.
            return checks == 1

        with self.assertRaisesRegex(LcuActionStateChanged, "취소"):
            client.perform_champion_action(
                99,
                "ban",
                expected_action_id=777,
                pre_commit_check=still_current,
            )

        self.assertEqual(checks, 2)
        self.assertEqual(client.writes, [])

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

    def test_ready_check_rechecks_toggle_before_accepting(self) -> None:
        path = "/lol-matchmaking/v1/ready-check"
        client = FakeLcuClient({
            path: {"state": "InProgress", "playerResponse": "None"}
        })
        with self.assertRaises(LcuActionStateChanged):
            client.accept_ready_check_if_pending(
                pre_commit_check=lambda: False,
            )
        self.assertEqual(client.writes, [])

    def test_stop_matchmaking_uses_lobby_delete_endpoint(self) -> None:
        client = FakeLcuClient({})
        client.stop_matchmaking_search()
        self.assertEqual(client.writes, [(
            "DELETE", "/lol-lobby/v2/lobby/matchmaking/search", None,
        )])


class LcuCredentialCacheTests(unittest.TestCase):
    def test_malformed_lockfile_falls_back_without_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lockfile = Path(temp_dir) / "lockfile"
            lockfile.write_text(
                "LeagueClientUx:123:not-a-port:token:https",
                encoding="utf-8",
            )
            client = LcuClient(str(lockfile))
            with (
                patch(
                    "lol_support_advisor.lcu.Path.read_text",
                    side_effect=[
                        "LeagueClientUx:123:not-a-port:token:https",
                        *[OSError("missing") for _index in range(15)],
                    ],
                ),
                patch.object(
                    client, "_credentials_from_process",
                    return_value=(3210, "process-token"),
                ) as process_lookup,
            ):
                self.assertEqual(
                    client._credentials(), (3210, "process-token")
                )
            process_lookup.assert_called_once_with()

    def test_failed_discovery_has_short_negative_cache(self) -> None:
        client = LcuClient()
        with (
            patch.object(
                client, "_credentials_from_lockfile", return_value=None,
            ) as lockfile_lookup,
            patch.object(
                client, "_credentials_from_process", return_value=None,
            ) as process_lookup,
        ):
            with self.assertRaises(LcuUnavailable):
                client._credentials()
            with self.assertRaises(LcuUnavailable):
                client._credentials()

        lockfile_lookup.assert_called_once_with()
        process_lookup.assert_called_once_with()

    def test_process_discovered_credentials_are_reused(self) -> None:
        client = LcuClient()
        with (
            patch.object(
                client, "_credentials_from_lockfile", return_value=None,
            ) as lockfile_lookup,
            patch.object(
                client, "_credentials_from_process",
                return_value=(3210, "cached-token"),
            ) as process_lookup,
        ):
            self.assertEqual(client._credentials(), (3210, "cached-token"))
            self.assertEqual(client._credentials(), (3210, "cached-token"))

        lockfile_lookup.assert_called_once_with()
        process_lookup.assert_called_once_with()

    def test_connection_failure_invalidates_cache_for_next_request(self) -> None:
        client = LcuClient()
        discovered = [(3210, "old-token"), (6543, "new-token")]
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{}'

        with (
            patch.object(
                client, "_credentials_from_lockfile",
                side_effect=discovered,
            ) as lookup,
            patch(
                "lol_support_advisor.lcu.urlopen",
                side_effect=[URLError("connection lost"), response],
            ) as open_url,
        ):
            with self.assertRaisesRegex(LcuUnavailable, "연결 실패"):
                client.get("/first")
            self.assertEqual(client.get("/second"), {})

        self.assertEqual(lookup.call_count, 2)
        self.assertIn("127.0.0.1:3210", open_url.call_args_list[0].args[0].full_url)
        self.assertIn("127.0.0.1:6543", open_url.call_args_list[1].args[0].full_url)

    def test_authentication_failure_invalidates_cache_for_next_request(self) -> None:
        client = LcuClient()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{}'
        unauthorized = HTTPError(
            "https://127.0.0.1:3210/first", 401, "Unauthorized",
            hdrs=None, fp=BytesIO(b'{}'),
        )

        with (
            patch.object(
                client, "_credentials_from_lockfile",
                side_effect=[(3210, "expired-token"), (6543, "fresh-token")],
            ) as lookup,
            patch(
                "lol_support_advisor.lcu.urlopen",
                side_effect=[unauthorized, response],
            ),
        ):
            with self.assertRaisesRegex(LcuUnavailable, "HTTP 401"):
                client.get("/first")
            self.assertEqual(client.get("/second"), {})

        self.assertEqual(lookup.call_count, 2)


if __name__ == "__main__":
    unittest.main()
