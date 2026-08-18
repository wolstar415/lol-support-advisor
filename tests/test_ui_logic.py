from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from lol_support_advisor.ui import (
    AdvisorApp, adc_flow_hint, allied_adc_member, auto_hover_recommendation,
    candidate_score,
    behavior_strength_signals, behavior_weakness_signals,
    build_guide_has_statistics, build_loadout_stat_text,
    cache_manager_champion_ids,
    choose_lux_auto_ban_target_ms,
    final_item_builds, matchup_build_reason,
    game_prediction_display_signature, local_draft_selection,
    live_active_context_signature, live_roster_signature,
    lux_auto_ban_deadline_after_timer_sample,
    lux_auto_ban_monitor_due,
    projected_lux_auto_ban_remaining_ms,
    estimate_live_game_prediction,
    matchup_final_item_builds, matchup_item_groups, matchup_rune_index,
    lane_matchup_from_snapshot, lane_matchup_label, lane_matchup_snapshot_fresh,
    matchup_counter_for_candidate,
    opgg_recent_form, participant_performance_ranks, representative_build_item,
    recent_match_ids_from_payload, streak_badge_text, support_archetype,
    recommendation_action_available, recommendation_draft_context_signature,
    exact_lp_badge_text, recent_exact_lp_summary, recent_prediction_accuracy,
    team_objective_counts, duo_group_visuals,
)
from lol_support_advisor.history import (
    HistoryOverview, MatchHistoryEntry, MatchLpChange,
)
from lol_support_advisor.icons import ItemIconCache
from lol_support_advisor.models import (
    BuildAsset, BuildItemGroup, ChampionBuildGuide, DraftBan, DraftMember,
    DraftSnapshot, GamePrediction,
    LaneMatchupStat,
    LiveGameSnapshot, LivePlayer,
    OpggCounter, OpggMcpChampionStat, OpggMcpRecentMatch,
    OpggMcpSummonerProfile, OpggSnapshot, OpggSynergyStat,
    PersonalStat, PlayerBehaviorStat, PlayerProfileStat, Recommendation, RuneBuild,
    SummonerSpellBuild,
)


class DuoEvidenceTests(unittest.TestCase):
    def test_enemy_ban_hover_rerenders_only_enemy_ban_strip(self) -> None:
        class FakeWidget:
            def configure(self, **_values: object) -> None:
                pass

        app = AdvisorApp.__new__(AdvisorApp)
        app.draft = DraftSnapshot(my_role="SUPPORT")
        app.draft.refresh_snapshot_id()
        app.recommendations = []
        app.recommendation_snapshot_id = ""
        app.recommendation_context_signature = ""
        app._pick_order_change_notice = ""
        app._selection_panel_revisions = {}
        app._selection_panel_signatures = {}
        for name in (
            "pick_order_label", "enemy_instruction_label",
            "enemy_unknown_button", "enemy_support_label", "stale_label",
            "ally_bans_frame", "enemy_bans_frame",
            "ally_picks_frame", "enemy_picks_frame",
        ):
            setattr(app, name, FakeWidget())
        rendered: list[tuple[str, bool]] = []
        app._render_draft_bans = (
            lambda _frame, ally: rendered.append(("bans", ally))
        )
        app._render_draft_team_slots = (
            lambda _frame, ally: rendered.append(("picks", ally))
        )

        app._render_draft()
        self.assertCountEqual(rendered, [
            ("bans", True), ("bans", False),
            ("picks", True), ("picks", False),
        ])

        rendered.clear()
        app.draft.enemy_ban_actions = [
            DraftBan("Lux", "럭스", "HOVER", actor_cell_id=5, order=1)
        ]
        app.draft.refresh_snapshot_id()
        app._render_draft()
        self.assertEqual(rendered, [("bans", False)])

    def test_lux_auto_ban_target_has_safe_early_window(self) -> None:
        observed: list[tuple[int, int]] = []

        def picker(minimum: int, maximum: int) -> int:
            observed.append((minimum, maximum))
            return 16_500

        self.assertEqual(choose_lux_auto_ban_target_ms(picker), 16_500)
        self.assertEqual(observed, [(15_000, 18_000)])

    def test_stale_recommendation_actions_use_live_preflight_instead_of_locking(self) -> None:
        self.assertTrue(recommendation_action_available(
            "hover", enabled=True, stale=True, demo=False,
        ))
        self.assertTrue(recommendation_action_available(
            "pick", enabled=True, stale=True, demo=False,
        ))
        self.assertTrue(recommendation_action_available(
            "ban", enabled=True, stale=True, demo=False,
        ))

    def test_auto_hover_uses_highest_ranked_available_recommendation(self) -> None:
        recommendations = [
            Recommendation(1, "Lux", "럭스", "포킹", "A", "", "", "", ""),
            Recommendation(2, "Thresh", "쓰레쉬", "유틸", "A", "", "", "", ""),
            Recommendation(3, "Leona", "레오나", "탱커", "B", "", "", "", ""),
        ]
        draft = DraftSnapshot(enemy_bans=["Lux"])
        self.assertEqual(
            auto_hover_recommendation(recommendations, draft).champion_id,
            "Thresh",
        )

    def test_pending_auto_hover_waits_for_safe_phase_then_uses_manual_guard(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        app._recommendation_generation = 7
        app._pending_auto_hover = (7, "Thresh", "SUPPORT")
        app.game_phase = "ChampSelect"
        app.draft = DraftSnapshot(my_role="SUPPORT")
        app._champ_select_inner_phase = "BAN_PICK"
        app._local_pick_action_in_progress = False
        app.registry = SimpleNamespace(by_id={"Thresh": (412, "쓰레쉬")})
        calls: list[tuple[object, ...]] = []
        app._execute_champion_action = lambda *args, **kwargs: calls.append(
            (*args, kwargs)
        )

        app._try_pending_auto_hover()
        self.assertEqual(calls, [])
        self.assertIsNotNone(app._pending_auto_hover)

        app._local_pick_action_in_progress = True
        app._try_pending_auto_hover()
        self.assertIsNone(app._pending_auto_hover)
        self.assertEqual(calls[0][0:2], ("Thresh", "hover"))
        self.assertTrue(calls[0][2]["quiet"])
        self.assertEqual(
            calls[0][2]["expected_current_champion_ids"], {0, 412}
        )

    def test_pending_auto_hover_never_replaces_existing_player_intent(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        app._recommendation_generation = 3
        app._pending_auto_hover = (3, "Thresh", "SUPPORT")
        app.game_phase = "ChampSelect"
        app.draft = DraftSnapshot(
            my_role="SUPPORT",
            my_hover=DraftMember(
                "Malphite", "말파이트", "SUPPORT", "HOVER", 4,
            ),
        )
        app._champ_select_inner_phase = "PLANNING"
        app._local_pick_action_in_progress = False
        app.registry = SimpleNamespace(by_id={"Thresh": (412, "쓰레쉬")})
        calls: list[object] = []
        app._execute_champion_action = lambda *args, **kwargs: calls.append(args)

        app._try_pending_auto_hover()

        self.assertEqual(calls, [])
        self.assertIsNone(app._pending_auto_hover)

    def test_lux_audit_persists_token_free_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = AdvisorApp.__new__(AdvisorApp)
            app._lux_auto_ban_audit_path = Path(temp_dir) / "audit.jsonl"
            app._lux_auto_ban_audit_lock = threading.Lock()
            app._audit_lux_auto_ban(
                "commit_success", action_id=9, remaining_ms=16_500,
            )
            payload = json.loads(
                app._lux_auto_ban_audit_path.read_text(encoding="utf-8")
            )
            self.assertEqual(payload["event"], "commit_success")
            self.assertEqual(payload["action_id"], 9)

    def test_lux_monitor_stale_timer_uses_guarded_local_deadline(self) -> None:
        self.assertFalse(lux_auto_ban_monitor_due(12_000, 11_000, 10.0, 10.34))
        self.assertTrue(lux_auto_ban_monitor_due(12_000, 11_000, 10.0, 10.36))
        self.assertFalse(lux_auto_ban_monitor_due(None, 11_000, 10.0, 9.99))
        self.assertTrue(lux_auto_ban_monitor_due(None, 11_000, 10.0, 10.0))

    def test_lux_monitor_reanchors_when_riot_timer_appears(self) -> None:
        deadline = lux_auto_ban_deadline_after_timer_sample(
            None, 28_000, 11_000, 100.0, 102.0,
        )
        self.assertEqual(deadline, 117.0)
        self.assertFalse(lux_auto_ban_monitor_due(28_000, 11_000, deadline, 102.5))
        self.assertTrue(lux_auto_ban_monitor_due(11_000, 11_000, deadline, 117.0))

    def test_lux_monitor_executes_when_ui_queue_is_not_drained(self) -> None:
        performed = threading.Event()
        calls: list[tuple[int, str, int | None]] = []

        class FakeLcu:
            @staticmethod
            def perform_champion_action(
                champion_key: int, action: str, *,
                expected_action_id: int | None = None,
                expected_current_champion_ids: object | None = None,
                verify_bannable: bool = True,
                pre_commit_check: object | None = None,
            ) -> None:
                if callable(pre_commit_check) and not pre_commit_check():
                    raise LcuActionStateChanged("cancelled")
                calls.append((champion_key, action, expected_action_id))
                if action == "ban":
                    performed.set()

        app = AdvisorApp.__new__(AdvisorApp)
        app.lux_auto_ban_enabled = True
        # This UI-only flag can remain stale while Tk is blocked.  The LCU
        # write lock and fresh preflight, not Tk, serialize the real action.
        app._champion_action_running = True
        app._lux_auto_ban_lock = threading.RLock()
        app._lux_auto_ban_generation = 0
        app._lux_auto_ban_monitoring = False
        app._lux_auto_ban_completed_action_id = None
        app._lux_auto_ban_action_id = None
        app._lux_auto_ban_target_remaining_ms = 0
        app._lux_auto_ban_fallback_deadline = 0.0
        app._lux_auto_ban_last_remaining_ms = None
        app._lux_auto_ban_last_sampled_at = 0.0
        app.lcu = FakeLcu()
        blocked_ui_callbacks: list[object] = []
        app._post_ui = lambda callback: blocked_ui_callbacks.append(callback)
        session = {
            "timer": {
                "phase": "BAN_PICK",
                "adjustedTimeLeftInPhase": 9_000,
            },
            "localPlayerCellId": 4,
            "actions": [[{
                "id": 77, "actorCellId": 4, "type": "ban",
                "isInProgress": True, "completed": False,
            }]],
            "bans": {"myTeamBans": [], "theirTeamBans": []},
        }

        self.assertTrue(app._ensure_lux_auto_ban_monitor(77, session))
        self.assertTrue(performed.wait(timeout=1.0))
        deadline = time.monotonic() + 1.0
        while app._lux_auto_ban_monitoring and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertEqual(calls, [
            (99, "ban_hover", 77), (99, "ban", 77),
        ])
        self.assertFalse(app._lux_auto_ban_monitoring)
        self.assertEqual(app._lux_auto_ban_completed_action_id, 77)
        self.assertGreaterEqual(len(blocked_ui_callbacks), 2)

    def test_lux_watcher_discovers_turn_while_ui_queue_is_blocked(self) -> None:
        performed = threading.Event()
        calls: list[tuple[int, str, int | None]] = []
        session = {
            "timer": {
                "phase": "BAN_PICK",
                "adjustedTimeLeftInPhase": 9_000,
            },
            "localPlayerCellId": 4,
            "actions": [[{
                "id": 91, "actorCellId": 4, "type": "ban",
                "isInProgress": True, "completed": False,
            }]],
            "bans": {"myTeamBans": [], "theirTeamBans": []},
        }

        class FakeLcu:
            @staticmethod
            def champ_select_session() -> dict[str, object]:
                return session

            @staticmethod
            def perform_champion_action(
                champion_key: int, action: str, *,
                expected_action_id: int | None = None,
                expected_current_champion_ids: object | None = None,
                verify_bannable: bool = True,
                pre_commit_check: object | None = None,
            ) -> None:
                if callable(pre_commit_check) and not pre_commit_check():
                    raise LcuActionStateChanged("cancelled")
                calls.append((champion_key, action, expected_action_id))
                if action == "ban":
                    performed.set()

        app = AdvisorApp.__new__(AdvisorApp)
        app.lux_auto_ban_enabled = True
        app._champion_action_running = False
        app._lux_auto_ban_lock = threading.RLock()
        app._lux_auto_ban_generation = 0
        app._lux_auto_ban_monitoring = False
        app._lux_auto_ban_completed_action_id = None
        app._lux_auto_ban_action_id = None
        app._lux_auto_ban_target_remaining_ms = 0
        app._lux_auto_ban_fallback_deadline = 0.0
        app._lux_auto_ban_last_remaining_ms = None
        app._lux_auto_ban_last_sampled_at = 0.0
        app._lux_auto_ban_watcher_running = False
        app._lux_auto_ban_watcher_wake = threading.Event()
        app.lcu = FakeLcu()
        blocked_ui_callbacks: list[object] = []
        app._post_ui = lambda callback: blocked_ui_callbacks.append(callback)

        app._start_lux_auto_ban_watcher()
        self.assertTrue(performed.wait(timeout=1.0))
        time.sleep(0.2)
        app.lux_auto_ban_enabled = False
        app._lux_auto_ban_watcher_wake.set()

        self.assertEqual(calls, [
            (99, "ban_hover", 91), (99, "ban", 91),
        ])
        self.assertGreaterEqual(len(blocked_ui_callbacks), 2)

    def test_lux_monitor_releases_owner_after_unexpected_failure(self) -> None:
        class BrokenLcu:
            @staticmethod
            def perform_champion_action(*_args: object, **_kwargs: object) -> None:
                raise ValueError("malformed transient payload")

        app = AdvisorApp.__new__(AdvisorApp)
        app.lux_auto_ban_enabled = True
        app._champion_action_running = False
        app._lux_auto_ban_lock = threading.RLock()
        app._lux_auto_ban_generation = 3
        app._lux_auto_ban_monitoring = True
        app._lux_auto_ban_completed_action_id = None
        app._lux_auto_ban_action_id = 92
        app._lux_auto_ban_target_remaining_ms = 11_000
        app._lux_auto_ban_fallback_deadline = 0.0
        app._lux_auto_ban_last_remaining_ms = 9_000
        app._lux_auto_ban_last_sampled_at = 0.0
        app.lcu = BrokenLcu()
        statuses: list[object] = []
        app._post_ui = lambda callback: statuses.append(callback)

        app._run_lux_auto_ban_monitor(
            3, 92, 11_000, 0.0, 9_000,
        )

        self.assertFalse(app._lux_auto_ban_monitoring)
        self.assertIsNone(app._lux_auto_ban_action_id)
        self.assertIsNone(app._lux_auto_ban_completed_action_id)
        self.assertGreaterEqual(len(statuses), 1)

    def test_lux_monitor_ignores_out_of_order_ui_status_callbacks(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        app._lux_auto_ban_lock = threading.RLock()
        app._lux_auto_ban_generation = 4
        app._lux_auto_ban_action_id = 77
        app._lux_auto_ban_last_sampled_at = 0.0
        app._lux_auto_ban_last_remaining_ms = None
        app._lux_auto_ban_status = "대기"
        callbacks: list[object] = []
        app._post_ui = lambda callback: callbacks.append(callback)
        app._render_automation_toggles = lambda: None

        app._post_lux_auto_ban_status(4, "이전 상태")
        app._post_lux_auto_ban_status(4, "최신 상태")
        app._record_lux_auto_ban_timer_sample(4, 77, 9_000, 11.0)
        app._record_lux_auto_ban_timer_sample(4, 77, 10_000, 10.0)

        callbacks[1]()
        callbacks[0]()
        self.assertEqual(app._lux_auto_ban_status, "최신 상태")
        self.assertEqual(app._lux_auto_ban_last_sampled_at, 11.0)
        self.assertEqual(app._lux_auto_ban_last_remaining_ms, 9_000)

    def test_lux_display_projects_latest_timer_sample_without_driving_action(self) -> None:
        self.assertEqual(
            projected_lux_auto_ban_remaining_ms(12_000, 100.0, 100.25),
            11_750,
        )
        self.assertEqual(
            projected_lux_auto_ban_remaining_ms(12_000, 100.0, 101.0),
            11_000,
        )
        self.assertEqual(
            projected_lux_auto_ban_remaining_ms(500, 100.0, 101.0),
            0,
        )
        self.assertIsNone(
            projected_lux_auto_ban_remaining_ms(None, 100.0, 101.0)
        )

    def test_lux_monitor_cancels_before_write_when_toggle_turns_off(self) -> None:
        calls: list[object] = []

        class FakeLcu:
            @staticmethod
            def champ_select_session() -> dict[str, object]:
                calls.append("session")
                return {}

            @staticmethod
            def perform_champion_action(
                _champion_key: int, action: str, **_kwargs: object,
            ) -> None:
                calls.append(action)

        app = AdvisorApp.__new__(AdvisorApp)
        app.lux_auto_ban_enabled = True
        app._champion_action_running = False
        app._lux_auto_ban_lock = threading.RLock()
        app._lux_auto_ban_generation = 0
        app._lux_auto_ban_monitoring = False
        app._lux_auto_ban_completed_action_id = None
        app._lux_auto_ban_action_id = None
        app._lux_auto_ban_target_remaining_ms = 0
        app._lux_auto_ban_fallback_deadline = 0.0
        app._lux_auto_ban_last_remaining_ms = None
        app._lux_auto_ban_last_sampled_at = 0.0
        app.lcu = FakeLcu()
        app._post_ui = lambda _callback: None
        session = {
            "timer": {
                "phase": "BAN_PICK",
                "adjustedTimeLeftInPhase": 30_000,
            },
            "localPlayerCellId": 4,
            "actions": [[{
                "id": 88, "actorCellId": 4, "type": "ban",
                "isInProgress": True, "completed": False,
                "championId": 0,
            }]],
            "bans": {"myTeamBans": [], "theirTeamBans": []},
        }

        self.assertTrue(app._ensure_lux_auto_ban_monitor(88, session))
        app.lux_auto_ban_enabled = False
        app._reset_lux_auto_ban_schedule()
        time.sleep(0.2)

        self.assertNotIn("ban", calls)
        self.assertFalse(app._lux_auto_ban_monitoring)

    def test_recommendation_context_ignores_ban_hover_but_not_locked_ban(self) -> None:
        member = DraftMember(
            "Malphite", "말파이트", "SUPPORT", "HOVER",
            cell_id=4, pick_order=5,
        )
        draft = DraftSnapshot(
            my_role="SUPPORT", my_pick_order=5, local_player_cell_id=4,
            ally_team_order=[member],
        )
        baseline = recommendation_draft_context_signature(draft)

        draft.enemy_ban_actions = [
            DraftBan("Lux", "럭스", "HOVER", actor_cell_id=5, order=1)
        ]
        self.assertEqual(
            recommendation_draft_context_signature(draft), baseline,
        )

        draft.enemy_bans = ["Lux"]
        self.assertNotEqual(
            recommendation_draft_context_signature(draft), baseline,
        )

    def test_local_draft_selection_keeps_completed_local_pick_visible(self) -> None:
        locked = DraftMember(
            "Malphite", "말파이트", "SUPPORT", "LOCKED", cell_id=4,
        )
        draft = DraftSnapshot(
            local_player_cell_id=4,
            ally_locked=[locked],
            ally_team_order=[locked],
        )
        self.assertIs(local_draft_selection(draft), locked)

        hover = DraftMember(
            "Braum", "브라움", "SUPPORT", "HOVER", cell_id=4,
        )
        draft.my_hover = hover
        self.assertIs(local_draft_selection(draft), hover)

    def test_prediction_display_signature_ignores_capture_time_only(self) -> None:
        first = GamePrediction(
            prediction_key="game-key",
            captured_at="2026-08-18T12:00:00",
            active_riot_id="Me#KR1",
            active_champion_id="Malphite",
            ally_champion_ids=("Malphite",),
            enemy_champion_ids=("Yuumi",),
            ally_riot_ids=("Me#KR1",),
            enemy_riot_ids=("Enemy#KR1",),
            win_probability=52.4,
            predicted_win=True,
            confidence="보통",
            evidence=("라인 상성 +2.4",),
            evidence_score=0.6,
        )
        second = GamePrediction.from_dict({
            **first.to_dict(), "captured_at": "2026-08-18T12:00:03",
        })
        changed = GamePrediction.from_dict({
            **first.to_dict(), "win_probability": 49.9,
            "predicted_win": False,
        })

        self.assertEqual(
            game_prediction_display_signature(first),
            game_prediction_display_signature(second),
        )
        self.assertNotEqual(
            game_prediction_display_signature(first),
            game_prediction_display_signature(changed),
        )

    def test_codex_answer_can_be_parsed_against_requested_draft_after_change(self) -> None:
        class FakeLabel:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            def configure(self, **values: object) -> None:
                self.values.update(values)

        app = AdvisorApp.__new__(AdvisorApp)
        app.registry = SimpleNamespace(
            contains=lambda champion_id: champion_id in {"Braum", "Janna", "Nami"},
        )
        requested = DraftSnapshot(my_role="SUPPORT")
        requested.refresh_snapshot_id()
        current = DraftSnapshot(my_role="SUPPORT", ally_bans=["Lux"])
        current.refresh_snapshot_id()
        app.draft = current
        app.exchange_status = FakeLabel()
        app.recommendations = []
        app.recommendation_snapshot_id = ""
        app._recommendation_apply_error = ""
        app._selection_panel_signatures = {}
        app._render_recommendations = lambda: None
        app._render_prompt_summary = lambda: None
        payload = {
            "schema_version": 2,
            "snapshot_id": requested.snapshot_id,
            "recommendations": [
                {
                    "rank": rank,
                    "champion_id": champion_id,
                    "champion_name_ko": champion_id,
                    "style": "테스트",
                    "blind_safety": "보통",
                    "reason": "이유",
                    "team_synergy": "조합",
                    "lane_plan": "라인",
                    "watch_for": "주의",
                }
                for rank, champion_id in enumerate(("Braum", "Janna", "Nami"), 1)
            ],
        }
        response = (
            "LOL_SUPPORT_V2\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\nEND_LOL_SUPPORT_V2"
        )

        applied = app._apply_recommendation_text(
            response,
            show_dialog=False,
            draft_context=requested,
            render_summary=False,
        )

        self.assertTrue(applied)
        self.assertEqual(len(app.recommendations), 3)
        self.assertEqual(app.recommendation_snapshot_id, requested.snapshot_id)
        self.assertEqual(
            app.recommendation_context_signature,
            recommendation_draft_context_signature(requested),
        )
        self.assertNotEqual(app.recommendation_snapshot_id, current.snapshot_id)

    def test_cache_manager_catalog_prevents_stale_wrong_role_cards(self) -> None:
        self.assertEqual(
            cache_manager_champion_ids(
                {"Leona", "Thresh"}, {"Leona"},
                {"Twitch", "Heimerdinger"}, {"Hecarim"},
            ),
            {"Leona", "Thresh"},
        )
        self.assertEqual(
            cache_manager_champion_ids(
                set(), {"Leona"}, {"Twitch"}, {"Hecarim"},
            ),
            {"Leona", "Twitch", "Hecarim"},
        )

    def test_cache_single_champion_blocks_unsupported_position_request(self) -> None:
        calls: list[tuple[str, object]] = []
        app = AdvisorApp.__new__(AdvisorApp)
        app.demo = False
        app._cache_manager_running = ""
        app.registry = SimpleNamespace(ko_name=lambda _champion_id: "트위치")
        app.storage = SimpleNamespace(
            load_opgg_position_catalog=lambda _position, max_age=None:
            ("16.16", ["Leona", "Thresh"]),
        )
        app._set_cache_manager_message = (
            lambda message, _color: calls.append(("message", message))
        )
        app._refresh_cache_manager_champion_cards = (
            lambda position, champion_id: calls.append(
                ("refresh", (position, champion_id))
            )
        )

        app._cache_single_champion("Twitch", "matchup", "SUPPORT")

        self.assertEqual(app._cache_manager_running, "")
        self.assertIn(("refresh", ("SUPPORT", "Twitch")), calls)
        self.assertIn("요청을 보내지 않았습니다", calls[0][1])

    def test_data_preferences_default_meta_count_and_route_job_ttls(self) -> None:
        calls: list[tuple[str, int]] = []

        class FakeStorage:
            def cache_job_cooldown_remaining(
                self, job_key: str, _now: datetime | None, hours: int,
            ) -> timedelta:
                calls.append((job_key, hours))
                return timedelta(hours=hours)

        app = AdvisorApp.__new__(AdvisorApp)
        app.storage = FakeStorage()
        app._data_preferences = {
            "opgg_meta_cooldown_hours": 6,
            "opgg_build_cooldown_hours": 36,
        }

        self.assertEqual(app._data_preference("opgg_meta_display_count"), 5)
        self.assertEqual(
            app._cache_job_cooldown_remaining("opgg_meta_all"),
            timedelta(hours=6),
        )
        self.assertEqual(
            app._cache_job_cooldown_remaining("opgg_builds_all"),
            timedelta(hours=36),
        )
        self.assertEqual(
            calls, [("opgg_meta_all", 6), ("opgg_builds_all", 36)]
        )

    def test_riot_history_cooldown_is_fixed_to_one_minute(self) -> None:
        calls: list[tuple[datetime | None, int]] = []

        class FakeStorage:
            @staticmethod
            def riot_sync_cooldown_remaining(
                now: datetime | None, minutes: int,
            ) -> timedelta:
                calls.append((now, minutes))
                return timedelta(minutes=minutes)

        app = AdvisorApp.__new__(AdvisorApp)
        app.storage = FakeStorage()
        now = datetime(2026, 8, 18, 12, 0, 0)

        self.assertEqual(
            app._riot_history_cooldown_remaining(now), timedelta(minutes=1)
        )
        self.assertEqual(calls, [(now, 1)])

    def test_startup_history_sync_bypasses_the_one_minute_cache(self) -> None:
        class FakeButton:
            def configure(self, **_values: object) -> None:
                pass

        class FakeStorage:
            @staticmethod
            def get_setting(key: str) -> str:
                return {
                    "riot_game_name": "Me",
                    "riot_tag_line": "KR1",
                    "riot_api_key": "key",
                }.get(key, "")

            @staticmethod
            def riot_api_key_needs_refresh() -> bool:
                return False

        app = AdvisorApp.__new__(AdvisorApp)
        app.storage = FakeStorage()
        app._riot_syncing = False
        app.game_phase = "Lobby"
        app._riot_history_cooldown_remaining = lambda: timedelta(seconds=45)
        app.riot_button = FakeButton()
        jobs: list[object] = []
        app._background = lambda work, _success, _error: jobs.append(work)

        app._sync_riot(automatic=True, startup=True)

        self.assertTrue(app._riot_syncing)
        self.assertEqual(len(jobs), 1)

    def test_pre_game_rank_snapshot_uses_cached_solo_entry(self) -> None:
        class FakeStorage:
            def __init__(self) -> None:
                self.settings = {
                    "riot_game_name": "Me", "riot_tag_line": "KR1",
                    "riot_puuid": "mine",
                }
                self.saved: list[tuple[str, dict, str, str]] = []

            def get_setting(self, key: str, default: str = "") -> str:
                return self.settings.get(key, default)

            def set_setting(self, key: str, value: str) -> None:
                self.settings[key] = value

            @staticmethod
            def load_live_profile_any_age(_riot_id: str) -> tuple[str, dict, str]:
                return "mine", {"solo_entry": {
                    "queueType": "RANKED_SOLO_5x5", "tier": "GOLD",
                    "rank": "I", "leaguePoints": 40, "wins": 10,
                    "losses": 10,
                }}, "2026-08-18T12:00:00"

            def save_rank_snapshot(
                self, puuid: str, entry: dict, *, stage: str,
                session_key: str,
            ) -> object:
                self.saved.append((puuid, entry, stage, session_key))
                return SimpleNamespace(snapshot_id=1, session_key=session_key)

        app = AdvisorApp.__new__(AdvisorApp)
        app.demo = False
        app.storage = FakeStorage()
        app.draft = DraftSnapshot(snapshot_id="DRAFT-1")

        self.assertTrue(app._capture_pre_game_rank_snapshot(force_new=True))

        self.assertEqual(app.storage.saved[0][0:3], (
            "mine", app.storage.saved[0][1], "PRE",
        ))
        session_key = app.storage.settings["rank_snapshot_active_session"]
        self.assertTrue(session_key.startswith("solo-"))
        self.assertEqual(app.storage.settings["rank_snapshot_active_puuid"], "mine")

    def test_post_sync_resolves_and_clears_rank_session(self) -> None:
        class FakeStorage:
            def __init__(self) -> None:
                self.settings = {
                    "riot_game_name": "Me", "riot_tag_line": "KR1",
                    "rank_snapshot_active_session": "solo-session",
                    "rank_snapshot_active_puuid": "mine",
                }
                self.saved_stage = ""

            def get_setting(self, key: str, default: str = "") -> str:
                return self.settings.get(key, default)

            def set_setting(self, key: str, value: str) -> None:
                self.settings[key] = value

            @staticmethod
            def load_live_profile_any_age(_riot_id: str) -> tuple[str, dict, str]:
                return "mine", {"solo_entry": {
                    "queueType": "RANKED_SOLO_5x5", "tier": "GOLD",
                    "rank": "I", "leaguePoints": 63, "wins": 11,
                    "losses": 10,
                }}, "2026-08-18T13:00:00"

            def save_rank_snapshot(
                self, _puuid: str, _entry: dict, *, stage: str,
                session_key: str,
            ) -> object:
                self.saved_stage = stage
                return SimpleNamespace(snapshot_id=2, session_key=session_key)

            @staticmethod
            def player_matches(_puuid: str, limit: int) -> list[dict]:
                return [{"metadata": {"matchId": "KR_1"}}]

            @staticmethod
            def resolve_match_lp_changes(_puuid: str, _matches: object) -> int:
                return 1

            @staticmethod
            def load_rank_snapshots(_puuid: str) -> list[object]:
                return [
                    SimpleNamespace(snapshot_id=1, session_key="solo-session"),
                    SimpleNamespace(snapshot_id=2, session_key="solo-session"),
                ]

            @staticmethod
            def load_match_lp_changes(_match_ids: object) -> dict:
                return {}

        app = AdvisorApp.__new__(AdvisorApp)
        app.storage = FakeStorage()

        self.assertEqual(app._finalize_pending_rank_snapshot("mine"), 1)
        self.assertEqual(app.storage.saved_stage, "POST")
        self.assertEqual(app.storage.settings["rank_snapshot_active_session"], "")
        self.assertEqual(app.storage.settings["rank_snapshot_active_puuid"], "")

    def test_history_load_batch_attaches_exact_lp(self) -> None:
        entry = MatchHistoryEntry(
            match_id="KR_1", game_creation=1, duration_seconds=1800,
            queue_id=420, champion_id="Janna", position="SUPPORT", won=True,
            kills=1, deaths=2, assists=10, kda=5.5, cs=30,
            cs_per_minute=1.0, vision_score=50, damage_to_champions=5000,
            damage_taken=4000, gold_earned=8000, kill_participation=60.0,
            items=(), ally_champions=(), enemy_champions=(),
        )
        overview = HistoryOverview(entries=[entry], games=1, wins=1)
        change = MatchLpChange(
            match_id="KR_1", puuid="mine", before_snapshot_id=1,
            after_snapshot_id=2, before_tier="GOLD", before_division="I",
            before_lp=40, after_tier="GOLD", after_division="I",
            after_lp=63, lp_delta=23, confidence="EXACT",
            resolved_at="2026-08-18T13:00:00",
        )

        class FakeLabel:
            def configure(self, **_values: object) -> None:
                pass

        class FakeStorage:
            loaded_ids: list[str] = []

            @staticmethod
            def get_setting(key: str, default: str = "") -> str:
                return {
                    "riot_game_name": "Me", "riot_tag_line": "KR1",
                    "riot_puuid": "mine",
                }.get(key, default)

            @staticmethod
            def find_puuid_by_riot_id(_riot_id: str) -> str:
                return "mine"

            @staticmethod
            def match_revision() -> tuple[int, int]:
                return 1, 1

            @staticmethod
            def player_matches(_puuid: str, limit: int) -> list[dict]:
                return []

            @staticmethod
            def resolve_game_predictions(_matches: object) -> int:
                return 0

            @classmethod
            def load_match_lp_changes(cls, match_ids: object) -> dict:
                cls.loaded_ids = list(match_ids)
                return {"KR_1": change}

            @staticmethod
            def load_game_predictions(_match_ids: object) -> dict:
                return {}

        app = AdvisorApp.__new__(AdvisorApp)
        app.storage = FakeStorage()
        app._history_loading = False
        app._history_reload_requested = False
        app._history_revision = None
        app._history_visible_count = 10
        app.history_overview = None
        app.history_status_label = FakeLabel()
        app._render_history = lambda: None
        app._background = lambda work, success, _error: success(work())

        with patch("lol_support_advisor.ui.analyze_history", return_value=overview):
            app._ensure_history_loaded(force=True)

        self.assertEqual(app.storage.loaded_ids, ["KR_1"])
        self.assertEqual(app.history_overview.entries[0].lp_delta, 23)
        self.assertEqual(app.history_overview.recent_20_lp_sum, 23)

    def test_rune_style_wheel_only_scrolls_page(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        routed_events: list[object] = []
        app._on_mousewheel = lambda event: routed_events.append(event) or "break"
        event = object()

        result = app._on_rune_style_mousewheel(event)

        self.assertEqual(result, "break")
        self.assertEqual(routed_events, [event])

    def test_live_prediction_combines_team_profiles_and_lane_matchups(self) -> None:
        players: list[LivePlayer] = []
        profiles: dict[str, PlayerProfileStat] = {}
        positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")
        for index, position in enumerate(positions):
            ally = LivePlayer(
                f"Ally{index}", f"아군{index}", f"Ally{index}", "KR1",
                "ORDER", position, is_active_player=index == 4,
            )
            enemy = LivePlayer(
                f"Enemy{index}", f"적군{index}", f"Enemy{index}", "KR2",
                "CHAOS", position,
            )
            players.extend((ally, enemy))
            profiles[ally.riot_id] = PlayerProfileStat(
                tier="GOLD", rank="I", season_wins=60, season_losses=40,
                champion_games=30, champion_wins=18, recent_games=10,
                recent_wins=6, status="OK",
            )
            profiles[enemy.riot_id] = PlayerProfileStat(
                tier="GOLD", rank="III", season_wins=40, season_losses=60,
                champion_games=30, champion_wins=12, recent_games=10,
                recent_wins=4, status="OK",
            )
        snapshot = LiveGameSnapshot(
            players=players, active_team="ORDER", active_riot_id="Ally4#KR1",
        )
        matchups = {
            position: LaneMatchupStat(
                position, f"Ally{index}", f"아군{index}",
                f"Enemy{index}", f"적군{index}", ally_win_rate=53.0,
            )
            for index, position in enumerate(positions)
        }

        prediction = estimate_live_game_prediction(
            snapshot, profiles, matchups, {},
            captured_at=datetime(2026, 8, 18, 12, 0, 0),
        )

        self.assertGreater(prediction.win_probability, 50.0)
        self.assertTrue(prediction.predicted_win)
        self.assertEqual(prediction.confidence, "높음")
        self.assertEqual(len(prediction.ally_champion_ids), 5)
        self.assertTrue(prediction.prediction_key)

    def test_live_prediction_uses_both_teams_previous_game_condition(self) -> None:
        ally = LivePlayer(
            "Thresh", "쓰레쉬", "Me", "KR1", "ORDER", "SUPPORT",
            is_active_player=True,
        )
        enemy = LivePlayer(
            "Leona", "레오나", "Enemy", "KR1", "CHAOS", "SUPPORT",
        )
        snapshot = LiveGameSnapshot(
            players=[ally, enemy], active_team="ORDER", active_riot_id=ally.riot_id,
        )
        common = dict(
            tier="GOLD", rank="II", season_wins=50, season_losses=50,
            champion_games=20, champion_wins=10, recent_games=10,
            recent_wins=5, status="OK",
        )
        profiles = {
            ally.riot_id: PlayerProfileStat(
                **common,
                last_game_champion_id="Thresh", last_game_kills=8,
                last_game_deaths=2, last_game_assists=15,
                last_game_won=True, last_op_score_rank=1,
            ),
            enemy.riot_id: PlayerProfileStat(
                **common,
                last_game_champion_id="Leona", last_game_kills=0,
                last_game_deaths=11, last_game_assists=2,
                last_game_won=False, last_op_score_rank=10,
            ),
        }

        prediction = estimate_live_game_prediction(snapshot, profiles, {}, {})

        self.assertGreater(prediction.win_probability, 50.0)
        self.assertTrue(any(
            "직전판 컨디션" in evidence for evidence in prediction.evidence
        ))

    def test_live_prediction_damps_recent_form_by_paired_position_coverage(self) -> None:
        positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")

        def prediction_for(pair_count: int) -> GamePrediction:
            players: list[LivePlayer] = []
            profiles: dict[str, PlayerProfileStat] = {}
            for index, position in enumerate(positions[:pair_count]):
                ally = LivePlayer(
                    "Janna", "잔나", f"Ally{index}", "KR1", "ORDER", position,
                    is_active_player=index == 0,
                )
                enemy = LivePlayer(
                    "Leona", "레오나", f"Enemy{index}", "KR1", "CHAOS", position,
                )
                players.extend((ally, enemy))
                common = dict(
                    tier="GOLD", rank="II", season_wins=50, season_losses=50,
                    champion_games=20, champion_wins=10, status="OK",
                )
                profiles[ally.riot_id] = PlayerProfileStat(
                    **common, recent_games=10, recent_wins=10,
                )
                profiles[enemy.riot_id] = PlayerProfileStat(
                    **common, recent_games=10, recent_wins=0,
                )
            return estimate_live_game_prediction(
                LiveGameSnapshot(players=players, active_team="ORDER"),
                profiles, {}, {},
            )

        one_pair = prediction_for(1)
        five_pairs = prediction_for(5)

        self.assertGreater(five_pairs.win_probability, one_pair.win_probability)
        self.assertLessEqual(one_pair.win_probability - 50.0, 0.7)
        self.assertEqual(five_pairs.win_probability, 53.0)

    def test_live_prediction_recent_family_is_symmetric_and_capped(self) -> None:
        positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")
        players: list[LivePlayer] = []
        profiles: dict[str, PlayerProfileStat] = {}
        swapped: dict[str, PlayerProfileStat] = {}
        for index, position in enumerate(positions):
            ally = LivePlayer(
                "Janna", "잔나", f"Ally{index}", "KR1", "ORDER", position,
                is_active_player=index == 0,
            )
            enemy = LivePlayer(
                "Leona", "레오나", f"Enemy{index}", "KR1", "CHAOS", position,
            )
            players.extend((ally, enemy))
            common = dict(
                tier="GOLD", rank="II", season_wins=50, season_losses=50,
                champion_games=20, champion_wins=10, status="OK",
            )
            strong = PlayerProfileStat(
                **common, recent_games=10, recent_wins=10,
                recent_kills=100, recent_deaths=1, recent_assists=100,
                overall_streak=10, last_game_champion_id="Janna",
                last_game_kills=20, last_game_deaths=0, last_game_assists=20,
                last_game_won=True, last_op_score_rank=1,
            )
            weak = PlayerProfileStat(
                **common, recent_games=10, recent_wins=0,
                recent_kills=0, recent_deaths=100, recent_assists=0,
                overall_streak=-10, last_game_champion_id="Leona",
                last_game_kills=0, last_game_deaths=20, last_game_assists=0,
                last_game_won=False, last_op_score_rank=10,
            )
            profiles[ally.riot_id], profiles[enemy.riot_id] = strong, weak
            swapped[ally.riot_id], swapped[enemy.riot_id] = weak, strong
        snapshot = LiveGameSnapshot(players=players, active_team="ORDER")

        favorable = estimate_live_game_prediction(snapshot, profiles, {}, {})
        unfavorable = estimate_live_game_prediction(snapshot, swapped, {}, {})

        self.assertLessEqual(favorable.win_probability, 53.5)
        self.assertAlmostEqual(
            favorable.win_probability + unfavorable.win_probability,
            100.0,
        )

    def test_live_prediction_ignores_unpaired_recent_form(self) -> None:
        ally = LivePlayer(
            "Janna", "잔나", "Ally", "KR1", "ORDER", "SUPPORT",
            is_active_player=True,
        )
        enemy = LivePlayer(
            "Leona", "레오나", "Enemy", "KR1", "CHAOS", "SUPPORT",
        )
        common = dict(
            tier="GOLD", rank="II", season_wins=50, season_losses=50,
            champion_games=20, champion_wins=10, status="OK",
        )
        profiles = {
            ally.riot_id: PlayerProfileStat(
                **common, recent_games=10, recent_wins=10,
                recent_kills=100, recent_deaths=1, recent_assists=100,
                overall_streak=10,
            ),
            enemy.riot_id: PlayerProfileStat(**common),
        }

        prediction = estimate_live_game_prediction(
            LiveGameSnapshot(players=[ally, enemy], active_team="ORDER"),
            profiles, {}, {},
        )

        self.assertEqual(prediction.win_probability, 50.0)
        self.assertFalse(any(
            "최근 폼" in evidence for evidence in prediction.evidence
        ))

    def test_recent_prediction_accuracy_ignores_matches_older_than_twenty(self) -> None:
        entries = [
            SimpleNamespace(prediction_correct=index % 2 == 0)
            for index in range(20)
        ] + [SimpleNamespace(prediction_correct=True) for _ in range(5)]

        self.assertEqual(recent_prediction_accuracy(entries), (10, 20, 50.0))

    def test_lp_text_shows_only_exact_observations(self) -> None:
        exact = SimpleNamespace(lp_delta=23, lp_confidence="EXACT")
        transition = SimpleNamespace(lp_delta=None, lp_confidence="TRANSITION")
        inferred = SimpleNamespace(lp_delta=18, lp_confidence="INFERRED")

        self.assertEqual(exact_lp_badge_text(exact), "+23 LP")
        self.assertEqual(exact_lp_badge_text(transition), "")
        self.assertEqual(exact_lp_badge_text(inferred), "")
        self.assertEqual(
            recent_exact_lp_summary([exact, transition, inferred]),
            (23, 1, 3),
        )

    def test_recent_match_id_page_uses_daily_cache_but_keeps_stale_fallback(self) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        payload = {
            "recent_match_ids": ["KR_3", "KR_2", "KR_1"],
            "recent_match_ids_checked_at": (
                now - timedelta(hours=23, minutes=59)
            ).isoformat(),
        }
        self.assertEqual(
            recent_match_ids_from_payload(payload, now),
            ["KR_3", "KR_2", "KR_1"],
        )
        payload["recent_match_ids_checked_at"] = (
            now - timedelta(hours=24, minutes=1)
        ).isoformat()
        self.assertIsNone(recent_match_ids_from_payload(payload, now))
        self.assertEqual(
            recent_match_ids_from_payload(payload, now, max_age=None),
            ["KR_3", "KR_2", "KR_1"],
        )

    def test_opgg_recent_form_separates_overall_and_current_champion_streak(self) -> None:
        def match(
            result: str, champion_key: int, kills: int, deaths: int, assists: int,
            score: float, rank: int,
        ) -> OpggMcpRecentMatch:
            return OpggMcpRecentMatch(
                match_id=f"match-{result}-{champion_key}-{kills}", created_at="",
                game_type="SOLORANKED", champion_key=champion_key,
                champion_name="잔나" if champion_key == 40 else "레오나",
                position="SUPPORT", result=result, kills=kills, deaths=deaths,
                assists=assists, op_score=score, op_score_rank=rank,
            )

        profile = OpggMcpSummonerProfile(
            riot_id="Player#KR1", game_name="Player", tag_line="KR1",
            recent_matches=[
                replace(match("WIN", 89, 20, 0, 20, 10.0, 1), game_type="ARAM"),
                replace(match("WIN", 89, 10, 0, 10, 9.0, 1), result="REMAKE"),
                match("WIN", 40, 1, 2, 13, 7.8, 2),
                match("WIN", 89, 2, 3, 10, 6.2, 4),
                match("LOSE", 89, 0, 5, 4, 3.0, 9),
                match("WIN", 40, 1, 1, 16, 8.4, 1),
                match("LOSE", 89, 2, 6, 8, 4.1, 8),
                match("WIN", 40, 0, 2, 12, 7.0, 3),
            ],
            recent_matches_status="OK", status="OK",
        )
        form = opgg_recent_form(profile, 40)
        self.assertEqual((form["recent_games"], form["recent_wins"]), (6, 4))
        self.assertEqual(form["overall_streak"], 2)
        self.assertEqual(form["champion_streak"], 3)
        self.assertEqual((form["recent_kills"], form["recent_deaths"]), (6, 19))
        self.assertEqual(form["last_game_champion_key"], 40)
        self.assertEqual(
            (
                form["last_game_kills"], form["last_game_deaths"],
                form["last_game_assists"], form["last_game_won"],
                form["last_op_score_rank"],
            ),
            (1, 2, 13, True, 2),
        )
        self.assertEqual(streak_badge_text(3), "3연승 중")
        self.assertEqual(streak_badge_text(-10, "잔나 "), "잔나 10+연패 중")
        self.assertEqual(streak_badge_text(1), "")

    def test_opgg_recent_form_uses_newest_completed_solo_match(self) -> None:
        older = OpggMcpRecentMatch(
            match_id="older", created_at="2026-08-18T01:00:00",
            game_type="SOLORANKED", champion_key=40, champion_name="잔나",
            position="SUPPORT", result="WIN", kills=2, deaths=1, assists=10,
            op_score=8.0, op_score_rank=2,
        )
        newest = OpggMcpRecentMatch(
            match_id="newest", created_at="2026-08-18T02:00:00",
            game_type="SOLORANKED", champion_key=89, champion_name="레오나",
            position="SUPPORT", result="LOSE", kills=0, deaths=7, assists=5,
            op_score=3.5, op_score_rank=9,
        )
        profile = OpggMcpSummonerProfile(
            riot_id="Player#KR1", game_name="Player", tag_line="KR1",
            recent_matches=[older, newest], recent_matches_status="OK",
            status="OK",
        )

        form = opgg_recent_form(profile, 40)

        self.assertEqual(form["last_game_champion_key"], 89)
        self.assertEqual(form["last_game_won"], False)
        self.assertEqual(form["last_op_score_rank"], 9)

    def test_behavior_signals_separate_strengths_and_actionable_weaknesses(self) -> None:
        stat = PlayerBehaviorStat(
            games=12, first_blood_rate=33.0, early_advantage_rate=58.0,
            kill_participation=47.0, average_deaths=6.4,
            vision_per_minute=1.1, control_wards=1.8,
        )
        strengths = behavior_strength_signals(stat, "SUPPORT")
        weaknesses = behavior_weakness_signals(stat, "SUPPORT")
        self.assertTrue(any("선취점" in value for value in strengths))
        self.assertTrue(any("평균 데스" in value for value in weaknesses))
        self.assertTrue(any("시야" in value for value in weaknesses))

    def test_behavior_signals_do_not_overclaim_tiny_samples(self) -> None:
        stat = PlayerBehaviorStat(
            games=2, first_blood_rate=100.0, early_advantage_rate=0.0,
            average_deaths=10.0, vision_per_minute=0.1,
        )
        self.assertEqual(
            behavior_strength_signals(stat, "SUPPORT"),
            ["표본 2경기 · 강점 판단 보류"],
        )
        self.assertEqual(
            behavior_weakness_signals(stat, "SUPPORT"),
            ["표본 2경기 · 약점 단정 보류"],
        )

    def test_match_performance_ranking_covers_all_ten_players(self) -> None:
        participants = [
            {
                "puuid": f"p{index}", "teamId": 100 if index < 5 else 200,
                "kills": index, "deaths": 2, "assists": 10 - index,
                "totalDamageDealtToChampions": 1000 * (index + 1),
                "visionScore": 10 + index, "goldEarned": 7000 + index * 500,
                "win": index < 5,
            }
            for index in range(10)
        ]
        ranks = participant_performance_ranks(participants)
        self.assertEqual(len(ranks), 10)
        self.assertEqual(set(ranks.values()), set(range(1, 11)))

    def test_opgg_profile_overlays_other_player_champion_record(self) -> None:
        app = SimpleNamespace(registry=SimpleNamespace(
            by_id={"Janna": (40, "잔나")},
            by_key={89: ("Leona", "레오나")},
        ))
        player = LivePlayer("Janna", "잔나", "Player", "KR1", "ORDER")
        opgg = OpggMcpSummonerProfile(
            riot_id="Player#KR1", game_name="Player", tag_line="KR1",
            tier="EMERALD", division="II", league_points=64,
            season_wins=31, season_losses=22, fetched_at="2026-08-18T01:00:00",
            champion_stats=[OpggMcpChampionStat(40, "잔나", 12, 8, 4)],
            recent_matches=[OpggMcpRecentMatch(
                match_id="KR_LAST", created_at="2026-08-18T00:50:00",
                game_type="SOLORANKED", champion_key=89,
                champion_name="레오나", position="SUPPORT", result="LOSE",
                kills=1, deaths=7, assists=8, op_score=4.2, op_score_rank=8,
            )],
            recent_matches_status="OK",
            status="OK",
        )
        merged = AdvisorApp._profile_with_opgg(
            app, PlayerProfileStat(status="LOADING"), opgg, player
        )
        self.assertEqual(merged.champion_data_source, "OPGG")
        self.assertEqual((merged.champion_games, merged.champion_wins), (12, 8))
        self.assertEqual((merged.tier, merged.rank, merged.league_points), (
            "EMERALD", "II", 64,
        ))
        self.assertEqual(merged.last_game_champion_id, "Leona")
        self.assertEqual(
            (
                merged.last_game_kills, merged.last_game_deaths,
                merged.last_game_assists, merged.last_game_won,
                merged.last_op_score_rank,
            ),
            (1, 7, 8, False, 8),
        )

    def test_missing_opgg_top_champion_is_not_reported_as_zero_percent(self) -> None:
        app = SimpleNamespace(registry=SimpleNamespace(by_id={"Thresh": (412, "쓰레쉬")}))
        player = LivePlayer("Thresh", "쓰레쉬", "Player", "KR1", "ORDER")
        opgg = OpggMcpSummonerProfile(
            riot_id="Player#KR1", game_name="Player", tag_line="KR1",
            tier="GOLD", division="I", season_wins=20, season_losses=20,
            fetched_at="2026-08-18T01:00:00", status="OK",
        )
        merged = AdvisorApp._profile_with_opgg(
            app, PlayerProfileStat(status="LOADING"), opgg, player
        )
        self.assertEqual(merged.champion_data_source, "OPGG_NOT_LISTED")
        self.assertIsNone(merged.champion_win_rate)

    def test_cached_rank_profile_is_available_before_detail_scan(self) -> None:
        profile = AdvisorApp._make_cached_player_profile(
            "player-puuid",
            {"solo_entry": {
                "tier": "EMERALD", "rank": "II", "leaguePoints": 44,
                "wins": 31, "losses": 22,
            }},
            "2026-08-18T00:00:00",
        )
        self.assertEqual(profile.status, "PARTIAL")
        self.assertEqual(profile.tier, "EMERALD")
        self.assertEqual(profile.season_wins, 31)
        self.assertEqual(profile.season_losses, 22)

    def test_live_cards_show_lane_first_pick_and_counter_pick(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        app.draft = AdvisorApp._demo_draft(SimpleNamespace())
        app.storage = SimpleNamespace(get_setting=lambda _key: "")
        app.live_game = AdvisorApp._demo_live_game(SimpleNamespace())
        self.assertTrue(app._attach_draft_pick_context(app.live_game))
        ally_top = next(
            player for player in app.live_game.allies if player.position == "TOP"
        )
        enemy_top = next(
            player for player in app.live_game.enemies if player.position == "TOP"
        )
        ally_text, _ally_color = app._live_pick_relation(ally_top)
        enemy_text, _enemy_color = app._live_pick_relation(enemy_top)
        self.assertIn("선픽", ally_text)
        self.assertIn("전체 1턴", ally_text)
        self.assertIn("후픽", enemy_text)
        self.assertIn("전체 2턴", enemy_text)

    def test_lane_matchup_uses_opgg_candidate_rate_and_reverse_rate(self) -> None:
        snapshot = OpggSnapshot(
            enemy_support_id="Jax",
            enemy_support_name_ko="잭스",
            position="TOP",
            patch="16.17",
            updated_at="2026-08-18T10:00:00",
            weak_picks=[OpggCounter("Garen", "가렌", 48.2, 4321)],
            raw_status="OK",
        )
        stat = lane_matchup_from_snapshot(
            "TOP", "Garen", "가렌", "Jax", "잭스", snapshot, cached=True,
        )
        self.assertEqual(stat.ally_win_rate, 48.2)
        self.assertEqual(stat.enemy_win_rate, 51.8)
        self.assertEqual(stat.games, 4321)
        self.assertTrue(stat.cached)
        self.assertEqual(lane_matchup_label(stat.ally_win_rate), "불리 상성")
        self.assertEqual(lane_matchup_label(stat.enemy_win_rate), "유리 상성")

    def test_lane_matchup_cache_is_fresh_for_one_day(self) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        fresh = OpggSnapshot(
            None, None, updated_at=(now - timedelta(hours=23, minutes=59)).isoformat()
        )
        stale = OpggSnapshot(
            None, None, updated_at=(now - timedelta(hours=24, minutes=1)).isoformat()
        )
        self.assertTrue(lane_matchup_snapshot_fresh(fresh, now))
        self.assertFalse(lane_matchup_snapshot_fresh(stale, now))

    def test_hover_candidate_uses_enemy_matchup_table_without_reversing_rate(self) -> None:
        xerath = OpggCounter("Xerath", "제라스", 52.7, 8120)
        snapshot = OpggSnapshot(
            enemy_support_id="Leona", enemy_support_name_ko="레오나",
            position="SUPPORT", counters=[xerath], raw_status="OK",
        )
        self.assertIs(
            matchup_counter_for_candidate(snapshot, "Xerath"), xerath
        )
        self.assertIsNone(matchup_counter_for_candidate(snapshot, "Janna"))
        self.assertIsNone(matchup_counter_for_candidate(None, "Xerath"))

    def test_live_lane_pairs_match_same_positions(self) -> None:
        snapshot = LiveGameSnapshot(
            active_team="ORDER",
            players=[
                LivePlayer("Garen", "가렌", "A", "KR1", "ORDER", "TOP"),
                LivePlayer("Jax", "잭스", "B", "KR1", "CHAOS", "TOP"),
                LivePlayer("Janna", "잔나", "C", "KR1", "ORDER", "UTILITY"),
                LivePlayer("Leona", "레오나", "D", "KR1", "CHAOS", "SUPPORT"),
            ],
        )
        pairs = AdvisorApp._live_lane_pairs(snapshot)
        self.assertEqual([position for position, _ally, _enemy in pairs], ["TOP", "SUPPORT"])
        self.assertEqual(pairs[0][1].champion_id, "Garen")
        self.assertEqual(pairs[0][2].champion_id, "Jax")

    def test_live_duo_check_splits_both_current_teams(self) -> None:
        players = [
            LivePlayer("Janna", "잔나", "Ally", "KR1", "ORDER"),
            LivePlayer("Leona", "레오나", "Enemy", "KR2", "CHAOS"),
        ]
        allies, enemies = AdvisorApp._live_team_groups(players, "ORDER")
        self.assertEqual([player.riot_id for player in allies], ["Ally#KR1"])
        self.assertEqual([player.riot_id for player in enemies], ["Enemy#KR2"])

    def test_duo_groups_share_pair_color_and_separate_other_pairs(self) -> None:
        players = [
            LivePlayer("Garen", "가렌", "AllyTop", "KR1", "ORDER"),
            LivePlayer("LeeSin", "리 신", "AllyJgl", "KR1", "ORDER"),
            LivePlayer("Ahri", "아리", "AllyMid", "KR1", "ORDER"),
            LivePlayer("Jinx", "징크스", "AllyAdc", "KR1", "ORDER"),
            LivePlayer("Leona", "레오나", "EnemyTop", "KR1", "CHAOS"),
            LivePlayer("Viego", "비에고", "EnemyJgl", "KR1", "CHAOS"),
        ]
        pairs = {
            "allytop#kr1": [("ALLYJGL#KR1", "매우 유력", "직전 2경기 동팀")],
            "AllyJgl#KR1": [("AllyTop#KR1", "매우 유력", "직전 2경기 동팀")],
            "AllyMid#KR1": [("AllyAdc#KR1", "매우 유력", "3회 동팀")],
            "AllyAdc#KR1": [("AllyMid#KR1", "매우 유력", "3회 동팀")],
            "EnemyTop#KR1": [("EnemyJgl#KR1", "유력", "2회 동팀")],
            "EnemyJgl#KR1": [("EnemyTop#KR1", "유력", "2회 동팀")],
        }
        visuals = duo_group_visuals(players, pairs)

        self.assertEqual(visuals["AllyTop#KR1"][:2], visuals["AllyJgl#KR1"][:2])
        self.assertEqual(visuals["AllyMid#KR1"][:2], visuals["AllyAdc#KR1"][:2])
        self.assertEqual(visuals["EnemyTop#KR1"][:2], visuals["EnemyJgl#KR1"][:2])
        colors = {
            visuals["AllyTop#KR1"][1],
            visuals["AllyMid#KR1"][1],
            visuals["EnemyTop#KR1"][1],
        }
        self.assertEqual(len(colors), 3)
        self.assertEqual(visuals["AllyTop#KR1"][2], "AllyJgl#KR1")
        self.assertEqual(visuals["AllyJgl#KR1"][2], "AllyTop#KR1")

    def test_duo_group_assignment_is_deterministic_and_prefers_strong_edge(self) -> None:
        players = [
            LivePlayer("Garen", "가렌", "A", "KR1", "ORDER"),
            LivePlayer("LeeSin", "리 신", "B", "KR1", "ORDER"),
            LivePlayer("Ahri", "아리", "C", "KR1", "ORDER"),
            LivePlayer("Leona", "레오나", "Enemy", "KR1", "CHAOS"),
        ]
        pairs = {
            "A#KR1": [
                ("B#KR1", "가능", "1회 동팀"),
                ("C#KR1", "매우 유력", "직전 2경기 동팀"),
                ("Enemy#KR1", "매우 유력", "상대팀이라 무시"),
            ],
            "C#KR1": [("A#KR1", "매우 유력", "직전 2경기 동팀")],
            "B#KR1": [("A#KR1", "가능", "1회 동팀")],
        }
        reversed_pairs = dict(reversed(list(pairs.items())))
        first = duo_group_visuals(players, pairs)
        second = duo_group_visuals(players, reversed_pairs)
        reversed_roster = duo_group_visuals(list(reversed(players)), pairs)

        self.assertEqual(first, second)
        self.assertEqual(first, reversed_roster)
        self.assertEqual(first["A#KR1"][2], "C#KR1")
        self.assertEqual(first["C#KR1"][2], "A#KR1")
        self.assertNotIn("B#KR1", first)
        self.assertNotIn("Enemy#KR1", first)

    def test_card_signature_tracks_derived_duo_group_color(self) -> None:
        players = [
            LivePlayer("Garen", "가렌", "C", "KR1", "ORDER", "TOP"),
            LivePlayer("LeeSin", "리 신", "D", "KR1", "ORDER", "JUNGLE"),
            LivePlayer("Ahri", "아리", "A", "KR1", "ORDER", "MIDDLE"),
            LivePlayer("Jinx", "징크스", "B", "KR1", "ORDER", "BOTTOM"),
        ]
        app = AdvisorApp.__new__(AdvisorApp)
        app.live_game = LiveGameSnapshot(players=players, active_team="ORDER")
        app.player_profiles = {
            player.riot_id: PlayerProfileStat(status="LOADING") for player in players
        }
        app.lane_matchups = {}
        app.duo_pairs = {
            "A#KR1": [("B#KR1", "유력", "2회 동팀")],
            "B#KR1": [("A#KR1", "유력", "2회 동팀")],
        }
        first = app._single_play_card_signature(players[2])
        app.duo_pairs.update({
            "C#KR1": [("D#KR1", "매우 유력", "직전 2경기 동팀")],
            "D#KR1": [("C#KR1", "매우 유력", "직전 2경기 동팀")],
        })
        self.assertNotEqual(first, app._single_play_card_signature(players[2]))

    def test_play_cards_do_not_rebuild_when_only_game_time_changes(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        player = LivePlayer(
            "Janna", "잔나", "Player", "KR1", "ORDER", "UTILITY", 12, True
        )
        app.live_game = LiveGameSnapshot(
            players=[player], active_team="ORDER", game_time=10,
        )
        app.player_profiles = {player.riot_id: PlayerProfileStat(status="LOADING")}
        app.duo_pairs = {}
        app.lane_matchups = {}
        first = app._play_card_state_signature()
        app.live_game.game_time = 13
        self.assertEqual(first, app._play_card_state_signature())
        app.player_profiles[player.riot_id] = PlayerProfileStat(
            season_wins=10, season_losses=5, status="OK"
        )
        self.assertNotEqual(first, app._play_card_state_signature())

    def test_play_card_signature_changes_only_with_lane_matchup_data(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        player = LivePlayer(
            "Garen", "가렌", "Player", "KR1", "ORDER", "TOP"
        )
        app.live_game = LiveGameSnapshot(players=[player], active_team="ORDER")
        app.player_profiles = {player.riot_id: PlayerProfileStat(status="LOADING")}
        app.duo_pairs = {}
        app._duo_checking = False
        app._duo_checked_signature = ""
        app.lane_matchups = {}
        first = app._single_play_card_signature(player)
        app.lane_matchups["TOP"] = LaneMatchupStat(
            "TOP", "Garen", "가렌", "Jax", "잭스", ally_win_rate=48.2,
        )
        self.assertNotEqual(first, app._single_play_card_signature(player))

    def test_lane_matchup_keeps_game_and_laning_rates_separate(self) -> None:
        stat = LaneMatchupStat(
            "JUNGLE", "LeeSin", "리 신", "Viego", "비에고",
            ally_win_rate=51.3, ally_laning_win_rate=54.6,
        )
        self.assertEqual(stat.enemy_win_rate, 48.7)
        self.assertEqual(stat.enemy_laning_win_rate, 45.4)
        missing = LaneMatchupStat(
            "TOP", "Garen", "가렌", "Jax", "잭스", ally_win_rate=49.0,
        )
        self.assertIsNone(missing.enemy_laning_win_rate)

    def test_duo_progress_does_not_rebuild_every_player_card(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        player = LivePlayer(
            "Janna", "잔나", "Player", "KR1", "ORDER", "UTILITY"
        )
        app.player_profiles = {player.riot_id: PlayerProfileStat(status="OK")}
        app.duo_pairs = {}
        app.lane_matchups = {}
        app._duo_checking = False
        app._duo_checked_signature = ""
        first = app._single_play_card_signature(player)
        app._duo_checking = True
        app._duo_checked_signature = "LIVE-1"
        self.assertEqual(first, app._single_play_card_signature(player))
        app.duo_pairs[player.riot_id] = [
            ("Friend#KR1", "유력", "현재판과 직전판 동팀")
        ]
        self.assertNotEqual(first, app._single_play_card_signature(player))

    def test_play_insight_render_uses_trailing_debounce(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.pending: dict[str, tuple[int, object]] = {}
                self.cancelled: list[str] = []
                self.serial = 0

            def after(self, delay: int, callback: object) -> str:
                self.serial += 1
                callback_id = f"after-{self.serial}"
                self.pending[callback_id] = (delay, callback)
                return callback_id

            def after_cancel(self, callback_id: str) -> None:
                self.cancelled.append(callback_id)
                self.pending.pop(callback_id, None)

        app = AdvisorApp.__new__(AdvisorApp)
        app.root = FakeRoot()
        app._play_insight_after_id = None
        rendered: list[bool] = []
        app._current_main_tab_index = lambda: 1
        app._render_play_insights = lambda: rendered.append(True)

        app._schedule_play_insight_render()
        first_id = app._play_insight_after_id
        app._schedule_play_insight_render()

        self.assertIn(first_id, app.root.cancelled)
        self.assertEqual(len(app.root.pending), 1)
        callback_id, (delay, callback) = next(iter(app.root.pending.items()))
        self.assertEqual(delay, 380)
        callback()
        self.assertEqual(rendered, [True])
        self.assertIsNone(app._play_insight_after_id)
        self.assertEqual(callback_id, "after-2")

    def test_play_summary_skips_identical_inputs(self) -> None:
        class FakeLabel:
            def configure(self, **_kwargs: object) -> None:
                pass

        app = AdvisorApp.__new__(AdvisorApp)
        player = LivePlayer(
            "Janna", "잔나", "Player", "KR1", "ORDER", "UTILITY", 1, True,
        )
        app.live_game = LiveGameSnapshot(
            players=[player], active_team="ORDER", active_riot_id=player.riot_id,
        )
        app.player_profiles = {
            player.riot_id: PlayerProfileStat(
                season_wins=10, season_losses=8, recent_games=5,
                recent_wins=3, champion_games=4, champion_wins=2,
                status="OK", updated_at="2026-08-18T01:00:00",
            )
        }
        app.duo_pairs = {}
        app.lane_matchups = {}
        app._lane_matchup_refreshing = set()
        app._live_signature = "LIVE-1"
        app._play_summary_signature = ""
        app.play_metrics = {
            key: (FakeLabel(), FakeLabel())
            for key in ("ally", "enemy", "cache", "duo", "matchup", "prediction")
        }
        updates: list[bool] = []
        app._update_live_prediction = lambda: updates.append(True)

        app._render_play_summary()
        first_signature = app._play_summary_state_signature()
        app.player_profiles[player.riot_id].updated_at = "2026-08-18T01:01:00"
        self.assertEqual(first_signature, app._play_summary_state_signature())
        app._render_play_summary()
        self.assertEqual(updates, [True])

        app.player_profiles[player.riot_id].recent_wins = 4
        app._render_play_summary()
        self.assertEqual(updates, [True, True])

    def test_live_active_context_changes_without_reloading_roster(self) -> None:
        waiting = LivePlayer(
            "Janna", "잔나", "Player", "KR1", "ORDER", "UTILITY",
            is_active_player=False,
        )
        ready = replace(waiting, is_active_player=True)
        first = LiveGameSnapshot(players=[waiting], active_team="ORDER")
        second = LiveGameSnapshot(
            players=[ready], active_team="ORDER", active_riot_id=ready.riot_id,
        )

        self.assertEqual(
            live_roster_signature(first), live_roster_signature(second),
        )
        self.assertNotEqual(
            live_active_context_signature(first),
            live_active_context_signature(second),
        )

    def test_live_poll_patches_active_player_without_profile_reload(self) -> None:
        waiting = LivePlayer(
            "Janna", "잔나", "Player", "KR1", "ORDER", "UTILITY",
            is_active_player=False,
        )
        ready = replace(waiting, is_active_player=True)
        previous = LiveGameSnapshot(players=[waiting], active_team="ORDER")
        current = LiveGameSnapshot(
            players=[ready], active_team="ORDER", active_riot_id=ready.riot_id,
        )
        scheduled: list[tuple[int, object]] = []
        rendered: list[bool] = []
        reloads: list[str] = []
        profile = PlayerProfileStat(status="OK")
        app = AdvisorApp.__new__(AdvisorApp)
        app.demo = False
        app.game_phase = "InProgress"
        app._live_polling = False
        app.live_client = SimpleNamespace(snapshot=lambda: current)
        app.root = SimpleNamespace(
            after=lambda delay, callback: scheduled.append((delay, callback))
        )
        app._background = lambda work, success, _error: success(work())
        app._attach_draft_pick_context = lambda _snapshot: None
        app.live_game = previous
        app._live_signature = live_roster_signature(previous)
        app._live_active_signature = live_active_context_signature(previous)
        app.player_profiles = {waiting.riot_id: profile}
        app._lane_opponent_analysis_context = object()
        app._lane_opponent_personal_stat = PersonalStat(games=1)
        app._lane_opponent_behavior = PlayerBehaviorStat(games=1)
        app._my_account_analysis_context = object()
        app._my_personal_stat = PersonalStat(games=1)
        app._my_behavior = PlayerBehaviorStat(games=1)
        app._play_insight_signature = "old"
        app._render_play = lambda: rendered.append(True)
        app._load_live_profiles = lambda: reloads.append("riot")
        app._load_opgg_live_profiles = lambda: reloads.append("opgg")
        app._check_live_duos = lambda: reloads.append("duo")

        app._poll_live()

        self.assertEqual(reloads, [])
        self.assertEqual(rendered, [True])
        self.assertIs(app.player_profiles[ready.riot_id], profile)
        self.assertEqual(
            app._live_active_signature,
            live_active_context_signature(current),
        )
        self.assertIsNone(app._lane_opponent_personal_stat)
        self.assertIsNone(app._my_personal_stat)
        self.assertIn(3000, [delay for delay, _callback in scheduled])

    def test_prediction_bar_updates_without_clearing_insights(self) -> None:
        class FakeWidget:
            def __init__(self) -> None:
                self.manager = ""
                self.values: dict[str, object] = {}
                self.pack_values: dict[str, object] = {}

            def configure(self, **kwargs: object) -> None:
                self.values.update(kwargs)

            def winfo_manager(self) -> str:
                return self.manager

            def pack(self, **kwargs: object) -> None:
                self.manager = "pack"
                self.pack_values.update(kwargs)

            def pack_forget(self) -> None:
                self.manager = ""

        app = AdvisorApp.__new__(AdvisorApp)
        app.play_prediction_frame = FakeWidget()
        app.play_prediction_value = FakeWidget()
        app.play_prediction_detail = FakeWidget()
        app.play_insight_body = FakeWidget()
        app._play_prediction_signature = ()
        app._live_prediction = GamePrediction(
            prediction_key="game-1", captured_at="2026-08-18T01:00:00",
            active_riot_id="Player#KR1", active_champion_id="Janna",
            ally_champion_ids=("Janna",), enemy_champion_ids=("Leona",),
            ally_riot_ids=("Player#KR1",), enemy_riot_ids=("Enemy#KR1",),
            win_probability=53.2, predicted_win=True, confidence="보통",
            evidence=("시즌 아군 52% · 적군 49%",), evidence_score=0.5,
        )
        app._clear = lambda _frame: self.fail("prediction must not clear insights")

        app._render_play_prediction()

        self.assertEqual(app.play_prediction_frame.manager, "pack")
        self.assertIn("53.2%", str(app.play_prediction_value.values.get("text")))
        self.assertIs(
            app.play_prediction_frame.pack_values.get("before"),
            app.play_insight_body,
        )

    def test_saved_accuracy_baseline_does_not_freeze_live_candidate(self) -> None:
        class FakeLabel:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            def configure(self, **values: object) -> None:
                self.values.update(values)

        players = [
            LivePlayer(
                "Janna" if index < 5 else "Leona",
                "잔나" if index < 5 else "레오나",
                f"Player{index}", "KR1",
                "ORDER" if index < 5 else "CHAOS",
                ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")[index % 5],
                is_active_player=index == 0,
            )
            for index in range(10)
        ]
        baseline = GamePrediction(
            prediction_key="same-game", captured_at="2026-08-18T01:00:00",
            active_riot_id=players[0].riot_id, active_champion_id="Janna",
            ally_champion_ids=("Janna",) * 5,
            enemy_champion_ids=("Leona",) * 5,
            ally_riot_ids=tuple(player.riot_id for player in players[:5]),
            enemy_riot_ids=tuple(player.riot_id for player in players[5:]),
            win_probability=48.0, predicted_win=False, confidence="보통",
            evidence=("저장 기준",), evidence_score=0.6,
        )
        candidate = replace(
            baseline, captured_at="2026-08-18T01:00:10",
            win_probability=57.0, predicted_win=True,
            evidence=("최신 후보",),
        )
        value, detail = FakeLabel(), FakeLabel()
        app = AdvisorApp.__new__(AdvisorApp)
        app.demo = False
        app.root = SimpleNamespace(after_cancel=lambda _callback_id: None)
        app.storage = SimpleNamespace(
            load_game_prediction_by_key=lambda _key: baseline,
        )
        app.play_metrics = {"prediction": (value, detail)}
        app.live_game = LiveGameSnapshot(players=players, active_team="ORDER")
        app.player_profiles = {}
        app.lane_matchups = {}
        app.duo_pairs = {}
        app._live_signature = "LIVE"
        app._prediction_baseline_key = ""
        app._prediction_baseline = None
        app._prediction_settle_after_id = None
        app._prediction_save_after_id = None
        app._prediction_saved_signature = ""

        with patch(
            "lol_support_advisor.ui.estimate_live_game_prediction",
            return_value=candidate,
        ):
            app._update_live_prediction()

        self.assertIs(app._prediction_baseline, baseline)
        self.assertIs(app._live_prediction, candidate)
        self.assertIn("57.0%", str(value.values.get("text")))

    def test_prediction_baseline_waits_until_loaders_settle(self) -> None:
        class FakeLabel:
            def configure(self, **_values: object) -> None:
                pass

        class FakeRoot:
            def __init__(self) -> None:
                self.pending: dict[str, tuple[int, object]] = {}
                self.serial = 0

            def after(self, delay: int, callback: object) -> str:
                self.serial += 1
                callback_id = f"after-{self.serial}"
                self.pending[callback_id] = (delay, callback)
                return callback_id

            def after_cancel(self, callback_id: str) -> None:
                self.pending.pop(callback_id, None)

        players = [
            LivePlayer(
                "Janna", "잔나", f"Player{index}", "KR1",
                "ORDER" if index < 5 else "CHAOS",
                ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")[index % 5],
                is_active_player=index == 0,
            )
            for index in range(10)
        ]
        candidate = GamePrediction(
            prediction_key="settling-game", captured_at="2026-08-18T01:00:00",
            active_riot_id=players[0].riot_id, active_champion_id="Janna",
            ally_champion_ids=("Janna",) * 5,
            enemy_champion_ids=("Janna",) * 5,
            ally_riot_ids=tuple(player.riot_id for player in players[:5]),
            enemy_riot_ids=tuple(player.riot_id for player in players[5:]),
            win_probability=52.0, predicted_win=True, confidence="보통",
            evidence=("수집 중",), evidence_score=0.5,
        )
        app = AdvisorApp.__new__(AdvisorApp)
        app.demo = False
        app.root = FakeRoot()
        app.storage = SimpleNamespace(load_game_prediction_by_key=lambda _key: None)
        app.play_metrics = {"prediction": (FakeLabel(), FakeLabel())}
        app.live_game = LiveGameSnapshot(players=players, active_team="ORDER")
        app.player_profiles = {}
        app.lane_matchups = {}
        app.duo_pairs = {}
        app._live_signature = "LIVE"
        app._lane_matchup_refreshing = set()
        app._profiles_loading = True
        app._opgg_profiles_loading = False
        app._duo_checking = False
        app._prediction_baseline_key = ""
        app._prediction_baseline = None
        app._prediction_settle_after_id = None
        app._prediction_save_after_id = None
        app._prediction_save_queued = None
        app._prediction_saved_signature = ""

        with patch(
            "lol_support_advisor.ui.estimate_live_game_prediction",
            return_value=candidate,
        ):
            app._update_live_prediction()
            self.assertTrue(any(
                delay >= 11_000 for delay, _callback in app.root.pending.values()
            ))
            self.assertIsNone(app._prediction_save_after_id)

            app._profiles_loading = False
            app._update_live_prediction()

        self.assertTrue(any(
            delay == 550 for delay, _callback in app.root.pending.values()
        ))
        self.assertIsNotNone(app._prediction_save_after_id)

    def test_draft_pick_context_is_read_from_storage_only_once(self) -> None:
        reads: list[str] = []
        cached = {
            "ally": {"Janna": [5, 5]},
            "enemy": {"Leona": [5, 6]},
        }
        app = AdvisorApp.__new__(AdvisorApp)
        app.draft = DraftSnapshot()
        app.storage = SimpleNamespace(
            get_setting=lambda key: (
                reads.append(key) or json.dumps(cached, ensure_ascii=False)
            )
        )
        app._cached_draft_pick_context = None
        app._draft_pick_context_cache_loaded = False

        first = app._draft_pick_context_candidates()
        second = app._draft_pick_context_candidates()

        self.assertEqual(reads, ["last_draft_pick_context"])
        self.assertEqual(first, second)
        self.assertEqual(first[0]["ally"]["Janna"], [5, 5])

    def test_identical_live_profile_does_not_schedule_any_ui_work(self) -> None:
        player = LivePlayer(
            "Janna", "잔나", "Player", "KR1", "ORDER", "UTILITY",
        )
        profile = PlayerProfileStat(
            tier="GOLD", rank="I", season_wins=20, season_losses=18,
            status="OK", updated_at="2026-08-18T01:00:00",
        )
        app = AdvisorApp.__new__(AdvisorApp)
        app._live_signature = "LIVE-1"
        app.live_game = LiveGameSnapshot(players=[player], active_team="ORDER")
        app.player_profiles = {player.riot_id: profile}
        app.opgg_player_profiles = {}
        scheduled: list[object] = []
        app.root = SimpleNamespace(after=lambda *_args: scheduled.append(_args))
        app._schedule_play_render = lambda: scheduled.append("render")

        app._apply_live_profile(
            player.riot_id,
            replace(profile, updated_at="2026-08-18T01:01:00"),
            "LIVE-1",
        )

        self.assertEqual(scheduled, [])

    def test_player_icon_ready_patches_label_without_card_render(self) -> None:
        image = object()

        class FakeLabel:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            @staticmethod
            def winfo_exists() -> bool:
                return True

            def configure(self, **values: object) -> None:
                self.values.update(values)

        label = FakeLabel()
        app = AdvisorApp.__new__(AdvisorApp)
        app.icon_cache = SimpleNamespace(get=lambda _champion, _size: image)

        app._update_player_card_icon(label, "Janna", 68)

        self.assertIs(label.values["image"], image)
        self.assertEqual(label.values["text"], "")

    def test_prediction_save_is_debounced_and_runs_in_background(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.pending: dict[str, object] = {}
                self.serial = 0

            def after(self, _delay: int, callback: object) -> str:
                self.serial += 1
                callback_id = f"save-{self.serial}"
                self.pending[callback_id] = callback
                return callback_id

            def after_cancel(self, callback_id: str) -> None:
                self.pending.pop(callback_id, None)

        prediction = GamePrediction(
            prediction_key="game-1", captured_at="2026-08-18T01:00:00",
            active_riot_id="Player#KR1", active_champion_id="Janna",
            ally_champion_ids=("Janna",), enemy_champion_ids=("Leona",),
            ally_riot_ids=("Player#KR1",), enemy_riot_ids=("Enemy#KR1",),
            win_probability=53.2, predicted_win=True, confidence="보통",
            evidence=("시즌 표본",), evidence_score=0.5,
        )
        saved: list[GamePrediction] = []
        app = AdvisorApp.__new__(AdvisorApp)
        app.root = FakeRoot()
        app.storage = SimpleNamespace(
            save_game_prediction=lambda value: saved.append(value)
        )
        app._prediction_saved_signature = ""
        app._prediction_save_pending_signature = ""
        app._prediction_save_after_id = None
        app._prediction_save_running = False
        app._prediction_save_queued = None
        app._background = lambda work, success, _error: success(work())

        app._schedule_game_prediction_save(prediction, "settled")
        self.assertEqual(saved, [])
        callback = next(iter(app.root.pending.values()))
        callback()

        self.assertEqual(saved, [prediction])
        self.assertEqual(app._prediction_saved_signature, "settled")

    def test_prediction_writes_are_single_flight_and_finish_with_latest(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.pending: dict[str, object] = {}
                self.serial = 0

            def after(self, _delay: int, callback: object) -> str:
                self.serial += 1
                callback_id = f"save-{self.serial}"
                self.pending[callback_id] = callback
                return callback_id

            def after_cancel(self, callback_id: str) -> None:
                self.pending.pop(callback_id, None)

        first = GamePrediction(
            prediction_key="game-1", captured_at="2026-08-18T01:00:00",
            active_riot_id="Player#KR1", active_champion_id="Janna",
            ally_champion_ids=("Janna",), enemy_champion_ids=("Leona",),
            ally_riot_ids=("Player#KR1",), enemy_riot_ids=("Enemy#KR1",),
            win_probability=51.0, predicted_win=True, confidence="낮음",
            evidence=("첫 표본",), evidence_score=0.2,
        )
        latest = replace(
            first, captured_at="2026-08-18T01:00:01",
            win_probability=55.0, evidence=("완료 표본",), evidence_score=0.7,
        )
        saved: list[GamePrediction] = []
        workers: list[tuple[object, object, object]] = []
        app = AdvisorApp.__new__(AdvisorApp)
        app.root = FakeRoot()
        app.storage = SimpleNamespace(
            save_game_prediction=lambda value: saved.append(value)
        )
        app._prediction_saved_signature = ""
        app._prediction_save_pending_signature = ""
        app._prediction_save_after_id = None
        app._prediction_save_running = False
        app._prediction_save_queued = None
        app._background = (
            lambda work, success, error: workers.append((work, success, error))
        )

        app._schedule_game_prediction_save(first, "first")
        next(iter(app.root.pending.values()))()
        self.assertEqual(len(workers), 1)

        app._schedule_game_prediction_save(latest, "latest")
        list(app.root.pending.values())[-1]()
        self.assertEqual(len(workers), 1)

        work, success, _error = workers[0]
        success(work())
        self.assertEqual(len(workers), 2)
        work, success, _error = workers[1]
        success(work())

        self.assertEqual(saved, [first, latest])
        self.assertEqual(app._prediction_saved_signature, "latest")
        self.assertFalse(app._prediction_save_running)

    def test_play_insight_sections_only_clear_changed_section(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        lane = object()
        jungle = object()
        app.play_insight_sections = {"lane": lane, "jungle_plan": jungle}
        app._play_insight_section_signatures = {}
        cleared: list[object] = []
        rendered: list[object] = []
        app._clear = lambda section: cleared.append(section)

        app._render_play_insight_section(
            "lane", "lane-v1", lambda section: rendered.append(section),
        )
        app._render_play_insight_section(
            "lane", "lane-v1", lambda section: rendered.append(section),
        )
        app._render_play_insight_section(
            "jungle_plan", "jungle-v1", lambda section: rendered.append(section),
        )

        self.assertEqual(cleared, [lane, jungle])
        self.assertEqual(rendered, [lane, jungle])

    def test_scrollregion_update_debounces_and_skips_same_bbox(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.pending: dict[str, object] = {}
                self.cancelled: list[str] = []
                self.serial = 0

            def after(self, _delay: int, callback: object) -> str:
                self.serial += 1
                callback_id = f"scroll-{self.serial}"
                self.pending[callback_id] = callback
                return callback_id

            def after_cancel(self, callback_id: str) -> None:
                self.cancelled.append(callback_id)
                self.pending.pop(callback_id, None)

        class FakeCanvas:
            def __init__(self) -> None:
                self.configured: list[tuple[int, int, int, int]] = []

            def bbox(self, _target: str) -> tuple[int, int, int, int]:
                return (0, 0, 100, 200)

            def configure(self, *, scrollregion: tuple[int, int, int, int]) -> None:
                self.configured.append(scrollregion)

        app = AdvisorApp.__new__(AdvisorApp)
        app.root = FakeRoot()
        app._scrollregion_after_ids = {}
        app._scrollregion_bounds = {}
        canvas = FakeCanvas()

        app._schedule_scrollregion_update(canvas)
        first_id = app._scrollregion_after_ids[canvas]
        app._schedule_scrollregion_update(canvas)
        self.assertIn(first_id, app.root.cancelled)
        callback = next(iter(app.root.pending.values()))
        callback()
        self.assertEqual(canvas.configured, [(0, 0, 100, 200)])

        app._schedule_scrollregion_update(canvas)
        callback = next(iter(app.root.pending.values()))
        callback()
        self.assertEqual(canvas.configured, [(0, 0, 100, 200)])

    def test_ui_queue_drain_yields_after_sixteen_callbacks(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.scheduled: list[tuple[int, object]] = []

            def after(self, delay: int, callback: object) -> str:
                self.scheduled.append((delay, callback))
                return f"queue-{len(self.scheduled)}"

        app = AdvisorApp.__new__(AdvisorApp)
        app.root = FakeRoot()
        app._ui_queue = __import__("queue").SimpleQueue()
        completed: list[int] = []
        for index in range(20):
            app._ui_queue.put(lambda value=index: completed.append(value))

        app._drain_ui_queue()
        self.assertEqual(completed, list(range(16)))
        delay, callback = app.root.scheduled[-1]
        self.assertEqual(delay, 1)

        callback()
        self.assertEqual(completed, list(range(20)))
        self.assertEqual(app.root.scheduled[-1][0], 80)

    def test_ui_queue_callback_failure_does_not_stop_later_results(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.scheduled: list[tuple[int, object]] = []

            def after(self, delay: int, callback: object) -> str:
                self.scheduled.append((delay, callback))
                return "queue-next"

        app = AdvisorApp.__new__(AdvisorApp)
        app.root = FakeRoot()
        app._ui_queue = __import__("queue").SimpleQueue()
        completed: list[str] = []

        def fail() -> None:
            raise RuntimeError("stale Tk widget")

        app._ui_queue.put(fail)
        app._ui_queue.put(lambda: completed.append("next"))

        app._drain_ui_queue()

        self.assertEqual(completed, ["next"])
        self.assertEqual(app.root.scheduled[-1][0], 80)

    def test_selection_panel_signature_skips_unchanged_panel(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        app._selection_panel_signatures = {}
        app._selection_panel_revisions = {}
        self.assertTrue(app._selection_panel_needs_render("draft", {"pick": 1}))
        self.assertFalse(app._selection_panel_needs_render("draft", {"pick": 1}))
        self.assertTrue(app._selection_panel_needs_render("draft", {"pick": 2}))
        app._selection_panel_revisions["draft"] = 1
        self.assertTrue(app._selection_panel_needs_render("draft", {"pick": 2}))

    def test_cache_champion_refresh_only_updates_existing_buttons(self) -> None:
        class FakeButton:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            def configure(self, **kwargs: object) -> None:
                self.values.update(kwargs)

        build_button = FakeButton()
        matchup_button = FakeButton()
        app = AdvisorApp.__new__(AdvisorApp)
        app.demo = False
        app._cache_manager_running = "champion:build:SUPPORT:Leona"
        app._cache_manager_champion_widgets = {
            ("SUPPORT", "Leona"): {
                "build_button": build_button,
                "matchup_button": matchup_button,
                "build_fresh": False,
                "matchup_fresh": False,
            }
        }

        app._refresh_cache_manager_champion_button_states()

        self.assertEqual(build_button.values["text"], "진행 중")
        self.assertEqual(build_button.values["state"], "disabled")
        self.assertEqual(matchup_button.values["state"], "disabled")

    def test_finished_single_cache_refreshes_only_target_card(self) -> None:
        job_key = "champion:matchup:MIDDLE:Ahri"
        calls: list[tuple[str, object]] = []
        app = AdvisorApp.__new__(AdvisorApp)
        app._cache_manager_running = job_key
        app._set_cache_manager_message = (
            lambda text, color: calls.append(("message", text))
        )
        app._refresh_cache_manager_rows = lambda: calls.append(("rows", None))
        app._refresh_cache_manager_champion_cards = (
            lambda position, champion_id: calls.append(
                ("card", (position, champion_id))
            )
        )

        app._finish_single_champion_cache(job_key, True, "완료")

        self.assertEqual(app._cache_manager_running, "")
        self.assertIn(("card", ("MIDDLE", "Ahri")), calls)

    def test_opgg_detail_view_switches_only_visible_table(self) -> None:
        class FakeFrame:
            def __init__(self) -> None:
                self.visible = False

            def winfo_manager(self) -> str:
                return "pack" if self.visible else ""

            def pack(self, **_kwargs: object) -> None:
                self.visible = True

            def pack_forget(self) -> None:
                self.visible = False

        class FakeWidget:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            def configure(self, **kwargs: object) -> None:
                self.values.update(kwargs)

        saved: list[tuple[str, str]] = []
        app = AdvisorApp.__new__(AdvisorApp)
        app.opgg_table_frames = {
            "OPGG": FakeFrame(), "PERSONAL": FakeFrame(), "MATCHUP": FakeFrame(),
        }
        app.opgg_view_buttons = {
            "OPGG": FakeWidget(), "PERSONAL": FakeWidget(), "MATCHUP": FakeWidget(),
        }
        app.opgg_view_hint = FakeWidget()
        app.storage = SimpleNamespace(
            set_setting=lambda key, value: saved.append((key, value))
        )
        app._set_opgg_detail_view("PERSONAL")
        self.assertFalse(app.opgg_table_frames["OPGG"].visible)
        self.assertTrue(app.opgg_table_frames["PERSONAL"].visible)
        self.assertFalse(app.opgg_table_frames["MATCHUP"].visible)
        self.assertEqual(saved, [("opgg_detail_view", "PERSONAL")])
        self.assertIn("챔피언 전체 성적", app.opgg_view_hint.values["text"])

    def test_demo_draft_shows_all_five_allies_and_my_hover(self) -> None:
        draft = AdvisorApp._demo_draft(SimpleNamespace())
        shown = len(draft.ally_locked) + len(draft.ally_hover) + int(
            draft.my_hover is not None
        )
        self.assertEqual(shown, 5)
        self.assertEqual(draft.my_hover.champion_id, "Janna")
        self.assertEqual(draft.my_hover.cell_id, draft.local_player_cell_id)
        self.assertEqual(draft.my_status, "SELECTING")

    def test_match_detail_rank_uses_cached_player_profile(self) -> None:
        profile = PlayerProfileStat(
            tier="EMERALD", rank="II", league_points=63, status="OK"
        )
        app = SimpleNamespace(
            player_profiles={"Player#KR1": profile},
            storage=SimpleNamespace(load_live_profile_any_age=lambda _riot_id: None),
        )
        text, color = AdvisorApp._detail_participant_rank(app, {
            "riotIdGameName": "Player", "riotIdTagline": "KR1",
        })
        self.assertEqual(text, "EMERALD II\n63LP")
        self.assertNotEqual(color, "")

    def test_rune_click_updates_only_selection_state(self) -> None:
        calls: list[str] = []
        style = SimpleNamespace(slots=[[101, 102]])
        app = SimpleNamespace(
            rune_catalog=SimpleNamespace(style=lambda _style_id: style),
            _rune_primary_style_id=8400,
            _rune_primary_perks=[101],
            _rune_editor_custom=False,
            _refresh_rune_editor_selection_state=lambda: calls.append("selection"),
            _render_build=lambda: calls.append("full"),
        )
        AdvisorApp._select_primary_rune(app, 0, 102)
        self.assertEqual(app._rune_primary_perks, [102])
        self.assertTrue(app._rune_editor_custom)
        self.assertEqual(calls, ["selection"])

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
        self.assertEqual(AdvisorApp._classify_duo_evidence([(0, 1)])[0], "유력")
        self.assertEqual(AdvisorApp._classify_duo_evidence([(3, 4)])[0], "가능")
        self.assertEqual(
            AdvisorApp._classify_duo_evidence([(2, 8), (9, 20)])[0], "가능"
        )
        self.assertIsNone(AdvisorApp._classify_duo_evidence([(5, 7)]))

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

    def test_matchup_build_prefers_defensive_rune_and_item_against_engage(self) -> None:
        rune_builds = [
            RuneBuild("공격", 8200, 8300, [BuildAsset(8229, "신비로운 유성")]),
            RuneBuild("수비", 8400, 8300, [
                BuildAsset(8465, "수호자"), BuildAsset(8473, "뼈 방패"),
            ]),
        ]
        self.assertEqual(matchup_rune_index(rune_builds, "Leona"), 1)
        groups = [BuildItemGroup("핵심 아이템", [
            BuildAsset(3107, "구원"), BuildAsset(3190, "강철의 솔라리 펜던트"),
        ])]
        adjusted = matchup_item_groups(groups, "Leona")
        self.assertEqual(adjusted[0].items[0].asset_id, 3190)
        self.assertEqual(matchup_build_reason("Leona")[0], "이니시 대응")

    def test_build_statistics_format_without_inventing_missing_sample(self) -> None:
        self.assertEqual(
            build_loadout_stat_text(68_728, 52.09667),
            "승률 52.1% · 68,728게임",
        )
        self.assertEqual(build_loadout_stat_text(None, None), "표본 정보 없음")
        guide = ChampionBuildGuide(
            "Thresh", "쓰레쉬", "SUPPORT",
            rune_builds=[RuneBuild(
                "추천 룬", 8400, 8300, [], games=68_728, win_rate=52.1,
            )],
            summoner_spell_builds=[SummonerSpellBuild(
                "추천 스펠", [BuildAsset(4, "점멸"), BuildAsset(14, "점화")],
                games=88_622, win_rate=52.0,
            )],
        )
        self.assertTrue(build_guide_has_statistics(guide))
        self.assertFalse(build_guide_has_statistics(ChampionBuildGuide(
            "Thresh", "쓰레쉬", "SUPPORT", rune_builds=guide.rune_builds,
        )))
        self.assertFalse(build_guide_has_statistics(ChampionBuildGuide(
            "Thresh", "쓰레쉬", "SUPPORT",
        )))

    def test_spell_choice_changes_only_selected_spell_row(self) -> None:
        flash = BuildAsset(4, "점멸")
        ignite = BuildAsset(14, "점화")
        exhaust = BuildAsset(3, "탈진")
        app = AdvisorApp.__new__(AdvisorApp)
        app.build_guide = ChampionBuildGuide(
            "Thresh", "쓰레쉬", "SUPPORT",
            summoner_spells=[flash, ignite],
            summoner_spell_builds=[
                SummonerSpellBuild("조합 1", [flash, ignite], 80_000, 52.0),
                SummonerSpellBuild("조합 2", [exhaust, flash], 20_000, 51.0),
            ],
        )
        app._build_spell_index = 0
        app._flash_slot = "F"
        app._build_spell_choice_widgets = []

        class Row:
            @staticmethod
            def winfo_exists() -> bool:
                return True

        app._build_spell_row = Row()
        rendered: list[tuple[int, bool]] = []
        app._render_spell_assets_row = (
            lambda _row, guide, clear=False:
            rendered.append((app._ordered_spell_assets(guide)[0].asset_id, clear))
        )
        marked: list[bool] = []
        app._mark_build_render_current = lambda: marked.append(True)
        full_render: list[bool] = []
        app._render_build = lambda: full_render.append(True)

        app._set_build_spell_index(1)

        self.assertEqual(app._build_spell_index, 1)
        self.assertEqual(
            [spell.asset_id for spell in app._ordered_spell_assets(app.build_guide)],
            [3, 4],
        )
        self.assertEqual(rendered, [(3, True)])
        self.assertEqual(marked, [True])
        self.assertEqual(full_render, [])

    def test_failed_legacy_build_upgrade_attempt_persists_its_cooldown(self) -> None:
        class Settings:
            values: dict[str, str] = {}

            def get_setting(self, key: str, default: str = "") -> str:
                return self.values.get(key, default)

            def set_setting(self, key: str, value: str) -> None:
                self.values[key] = value

        settings = Settings()
        app = AdvisorApp.__new__(AdvisorApp)
        app.storage = settings
        app._data_preferences = {"opgg_build_cooldown_hours": 24}
        self.assertEqual(
            app._build_statistics_upgrade_remaining(
                "Thresh", "SUPPORT"
            ).total_seconds(),
            0,
        )

        app._mark_build_statistics_upgrade_attempt("Thresh", "SUPPORT")

        restarted = AdvisorApp.__new__(AdvisorApp)
        restarted.storage = settings
        restarted._data_preferences = {"opgg_build_cooldown_hours": 24}
        remaining = restarted._build_statistics_upgrade_remaining(
            "Thresh", "SUPPORT"
        )
        self.assertGreater(remaining, timedelta(hours=23))
        self.assertLessEqual(remaining, timedelta(hours=24))

    def test_preset_representative_item_rotates_core_choices(self) -> None:
        groups = [
            BuildItemGroup("시작 아이템", [BuildAsset(2003, "물약")]),
            BuildItemGroup("핵심 아이템", [
                BuildAsset(3190, "솔라리"), BuildAsset(3109, "기사의 맹세"),
            ]),
        ]
        self.assertEqual(representative_build_item(groups, 0).asset_id, 3190)
        self.assertEqual(representative_build_item(groups, 1).asset_id, 3109)

    def test_final_support_item_builds_have_six_distinct_slots(self) -> None:
        groups = [
            BuildItemGroup("신발", [BuildAsset(3009, "신속의 장화"), BuildAsset(3158, "아이오니아")]),
            BuildItemGroup("서포터 퀘스트 완성", [BuildAsset(3869, "천상의 이의"), BuildAsset(3876, "피의 노래")]),
            BuildItemGroup("핵심 아이템", [
                BuildAsset(3190, "솔라리"), BuildAsset(3109, "기사의 맹세"),
                BuildAsset(3050, "지크"), BuildAsset(3107, "구원"),
            ]),
            BuildItemGroup("상황별 아이템", [
                BuildAsset(3110, "얼어붙은 심장"), BuildAsset(3075, "가시 갑옷"),
                BuildAsset(3222, "미카엘"),
            ]),
        ]
        builds = final_item_builds(groups, "SUPPORT", limit=3)
        self.assertEqual(len(builds), 3)
        for build in builds:
            ids = [item.asset_id for item in build.items]
            self.assertEqual(len(ids), 6)
            self.assertEqual(len(set(ids)), 6)
            self.assertEqual(len(set(ids) & {3009, 3158}), 1)
            self.assertEqual(len(set(ids) & {3869, 3876}), 1)

        matchup_builds = matchup_final_item_builds(
            groups, "Leona", "SUPPORT", limit=2
        )
        self.assertEqual(len(matchup_builds), 2)
        first_ids = [item.asset_id for item in matchup_builds[0].items]
        self.assertEqual(len(first_ids), 6)
        self.assertIn(3190, first_ids)
        self.assertIn(3109, first_ids)
        self.assertIn(3110, first_ids)

    def test_poke_filter_includes_unfavorable_matchups(self) -> None:
        app = AdvisorApp.__new__(AdvisorApp)
        app.draft = DraftSnapshot(my_role="SUPPORT")
        app._support_filter = "POKE"
        app.opgg_snapshot = OpggSnapshot(
            enemy_support_id="Leona", enemy_support_name_ko="레오나",
            counters=[OpggCounter("Janna", "잔나", 53.0, 1000)],
            weak_picks=[
                OpggCounter("Lux", "럭스", 49.0, 900),
                OpggCounter("Zyra", "자이라", 47.0, 700),
            ],
        )
        self.assertEqual(
            [entry.champion_id for entry in app._filtered_counters()],
            ["Lux", "Zyra"],
        )

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

    def test_candidate_score_combines_opgg_and_local_adc_pairing(self) -> None:
        counter = OpggCounter("Thresh", "쓰레쉬", 51.0, 2500)
        base, _ = candidate_score(counter)
        synergy = OpggSynergyStat(
            champion_key=412, champion_id="Thresh", champion_name_ko="쓰레쉬",
            games=2000, wins=1120, win_rate=56.0, synergy_rank=1,
            synergy_tier=1,
        )
        personal = PersonalStat(
            ally_adc_games=10, ally_adc_wins=7, ally_adc_losses=3,
            ally_adc_win_rate=70.0,
        )
        combined, confidence = candidate_score(counter, personal, synergy)
        self.assertGreater(combined, base)
        self.assertEqual(confidence, "높음")

    def test_allied_adc_and_lane_flow_hint_use_hover_or_locked_pick(self) -> None:
        draft = DraftSnapshot(
            my_role="SUPPORT",
            ally_team_order=[
                DraftMember("Jinx", "징크스", "BOTTOM", "HOVER"),
                DraftMember("Janna", "잔나", "SUPPORT", "EMPTY"),
            ],
        )
        self.assertEqual(allied_adc_member(draft).champion_id, "Jinx")
        self.assertIn("보호", adc_flow_hint("Jinx"))


if __name__ == "__main__":
    unittest.main()
