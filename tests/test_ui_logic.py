from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from lol_support_advisor.ui import (
    AdvisorApp, adc_flow_hint, allied_adc_member, candidate_score,
    behavior_strength_signals, behavior_weakness_signals,
    cache_manager_champion_ids,
    final_item_builds, matchup_build_reason,
    estimate_live_game_prediction,
    matchup_final_item_builds, matchup_item_groups, matchup_rune_index,
    lane_matchup_from_snapshot, lane_matchup_label, lane_matchup_snapshot_fresh,
    matchup_counter_for_candidate,
    opgg_recent_form, participant_performance_ranks, representative_build_item,
    recent_match_ids_from_payload, streak_badge_text, support_archetype,
    team_objective_counts,
)
from lol_support_advisor.icons import ItemIconCache
from lol_support_advisor.models import (
    BuildAsset, BuildItemGroup, DraftMember, DraftSnapshot, LaneMatchupStat,
    LiveGameSnapshot, LivePlayer,
    OpggCounter, OpggMcpChampionStat, OpggMcpRecentMatch,
    OpggMcpSummonerProfile, OpggSnapshot, OpggSynergyStat,
    PersonalStat, PlayerBehaviorStat, PlayerProfileStat, RuneBuild,
)


class DuoEvidenceTests(unittest.TestCase):
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
        self.assertEqual(streak_badge_text(3), "3연승 중")
        self.assertEqual(streak_badge_text(-10, "잔나 "), "잔나 10+연패 중")
        self.assertEqual(streak_badge_text(1), "")

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
        app = SimpleNamespace(registry=SimpleNamespace(by_id={"Janna": (40, "잔나")}))
        player = LivePlayer("Janna", "잔나", "Player", "KR1", "ORDER")
        opgg = OpggMcpSummonerProfile(
            riot_id="Player#KR1", game_name="Player", tag_line="KR1",
            tier="EMERALD", division="II", league_points=64,
            season_wins=31, season_losses=22, fetched_at="2026-08-18T01:00:00",
            champion_stats=[OpggMcpChampionStat(40, "잔나", 12, 8, 4)],
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
