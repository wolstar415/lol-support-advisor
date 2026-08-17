from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.storage import Storage
from lol_support_advisor.models import (
    ChampionBuildGuide, GamePrediction, OpggSnapshot,
    OpggSynergySnapshot, OpggSynergyStat,
)


def match_payload(
    match_id: str, my_win: bool, my_champion: str, enemy_support: str,
    game_creation: int = 1000,
    my_position: str = "UTILITY",
    enemy_position: str = "UTILITY",
) -> dict:
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameCreation": game_creation,
            "queueId": 420,
            "participants": [
                {
                    "puuid": "mine", "teamId": 100, "teamPosition": my_position,
                    "championName": my_champion, "win": my_win, "kills": 2,
                    "deaths": 2, "assists": 10, "visionScore": 50,
                    "riotIdGameName": "Me", "riotIdTagline": "KR1",
                },
                {
                    "puuid": "enemy", "teamId": 200, "teamPosition": enemy_position,
                    "championName": enemy_support, "win": not my_win,
                    "riotIdGameName": "Enemy", "riotIdTagline": "KR2",
                },
                {
                    "puuid": "ally", "teamId": 100, "teamPosition": "BOTTOM",
                    "championName": "Jinx", "win": my_win,
                    "riotIdGameName": "Ally", "riotIdTagline": "KR3",
                },
            ],
        },
    }


class StorageTests(unittest.TestCase):
    def test_integer_setting_uses_default_and_persists_valid_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            self.assertEqual(
                storage.get_int_setting("cache_hours", 24, 1, 720), 24
            )

            storage.set_setting("cache_hours", "36")
            reopened = Storage(Path(temp_dir) / "advisor.db")
            self.assertEqual(
                reopened.get_int_setting("cache_hours", 24, 1, 720), 36
            )

    def test_integer_setting_falls_back_for_invalid_and_out_of_range_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            storage.set_setting("cache_hours", "not-a-number")
            self.assertEqual(
                storage.get_int_setting("cache_hours", 24, 1, 720), 24
            )

            storage.set_setting("cache_hours", "0")
            self.assertEqual(
                storage.get_int_setting("cache_hours", 24, 1, 720), 24
            )
            storage.set_setting("cache_hours", "9999")
            self.assertEqual(
                storage.get_int_setting("cache_hours", 24, 1, 720), 24
            )

    def test_game_prediction_links_to_solo_match_and_keeps_accuracy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            start = datetime(2026, 8, 18, 12, 0, 0)
            match = match_payload(
                "KR_PREDICTION", True, "Janna", "Leona",
                int(start.timestamp() * 1000),
            )
            match["info"]["gameDuration"] = 1800
            prediction = GamePrediction(
                prediction_key="game-1",
                captured_at=(start + timedelta(seconds=30)).isoformat(),
                active_riot_id="Me#KR1",
                active_champion_id="Janna",
                ally_champion_ids=("Janna", "Jinx"),
                enemy_champion_ids=("Leona",),
                ally_riot_ids=("Me#KR1", "Ally#KR3"),
                enemy_riot_ids=("Enemy#KR2",),
                win_probability=56.4,
                predicted_win=True,
                confidence="보통",
                evidence=("라인 상성 53.2%",),
                evidence_score=0.62,
            )
            storage.save_game_prediction(prediction)

            self.assertEqual(storage.resolve_game_predictions([match]), 1)
            loaded = storage.load_game_predictions(["KR_PREDICTION"])[
                "KR_PREDICTION"
            ]
            self.assertEqual(loaded.win_probability, 56.4)
            self.assertTrue(loaded.actual_win)
            self.assertTrue(loaded.correct)

    def test_jungle_tendency_uses_cached_solo_queue_challenge_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            matches = []
            for index in range(3):
                match = match_payload(
                    f"KR_JUNGLE_{index}", True, "LeeSin", "Viego",
                    5000 - index, my_position="JUNGLE", enemy_position="JUNGLE",
                )
                match["info"]["participants"][0]["challenges"] = {
                    "takedownsFirstXMinutes": 3,
                    "killsOnLanersEarlyJungleAsJungler": 1,
                    "jungleCsBefore10Minutes": 48,
                    "enemyJungleMonsterKills": 14,
                    "epicMonsterKillsWithin30SecondsOfSpawn": 0.5,
                    "earlyLaningPhaseGoldExpAdvantage": 1,
                    "killParticipation": 0.72,
                    "visionScorePerMinute": 1.9,
                }
                match["info"]["participants"][0]["firstBloodKill"] = index == 0
                match["info"]["participants"][0]["firstBloodAssist"] = index == 1
                match["info"]["participants"][0]["detectorWardsPlaced"] = 4
                matches.append(match)
            storage.save_matches(matches)

            stat = storage.jungle_tendency("mine", "LeeSin")

            self.assertEqual(stat.status, "OK")
            self.assertEqual(stat.games, 3)
            self.assertTrue(stat.champion_specific)
            self.assertEqual(stat.early_takedowns, 3.0)
            self.assertIn("초반 개입 적극", stat.labels)
            self.assertIn("상대 정글 침투", stat.labels)
            self.assertIn("생성 직후 오브젝트", stat.labels)

            behavior = storage.player_behavior(
                "mine", "LeeSin", position="JUNGLE"
            )
            self.assertEqual(behavior.games, 3)
            self.assertAlmostEqual(behavior.first_blood_rate or 0.0, 66.67, places=1)
            self.assertEqual(behavior.early_advantage_rate, 100.0)
            self.assertAlmostEqual(behavior.kill_participation or 0.0, 72.0)
            self.assertIn("선취점 관여 잦음", behavior.labels)
            self.assertIn("초반 라인 우위", behavior.labels)
            self.assertIn("합류 적극", behavior.labels)

    def test_cache_jobs_have_independent_daily_cooldowns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            now = datetime(2026, 8, 18, 12, 0, 0)
            storage.mark_cache_job_success("opgg_meta_all", now)
            self.assertEqual(
                int(storage.cache_job_cooldown_remaining(
                    "opgg_meta_all", now + timedelta(hours=3)
                ).total_seconds()),
                21 * 60 * 60,
            )
            self.assertEqual(
                storage.cache_job_cooldown_remaining("opgg_builds_all", now),
                timedelta(0),
            )

    def test_opgg_adc_support_synergy_cache_round_trip_and_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            snapshot = OpggSynergySnapshot(
                ally_champion_key=222, ally_champion_id="Jinx",
                ally_champion_name_ko="징크스", fetched_at=datetime.now().isoformat(),
                synergies=[OpggSynergyStat(
                    champion_key=412, champion_id="Thresh",
                    champion_name_ko="쓰레쉬", games=3817, wins=2122,
                    win_rate=56.0, synergy_rank=1, synergy_tier=1,
                )], status="OK",
            )
            storage.save_opgg_synergy_snapshot(snapshot)
            loaded = storage.load_opgg_synergy_snapshot(
                "jinx", max_age=timedelta(hours=1),
            )
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.synergy_for("Thresh").games, 3817)
            self.assertIsNone(storage.load_opgg_synergy_snapshot(
                "Jinx", max_age=timedelta(seconds=-1),
            ))

    def test_build_guide_cache_and_cooldown_are_per_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            updated = datetime(2026, 8, 17, 12, 0, 0)
            storage.save_build_guide(ChampionBuildGuide(
                champion_id="Poppy", champion_name_ko="뽀삐", position="SUPPORT",
                updated_at=updated.isoformat(timespec="seconds"),
            ))
            storage.save_build_guide(ChampionBuildGuide(
                champion_id="Poppy", champion_name_ko="뽀삐", position="JUNGLE",
                patch="16.16", updated_at=updated.isoformat(timespec="seconds"),
            ))
            self.assertEqual(
                storage.load_build_guide("Poppy", "JUNGLE").patch, "16.16"
            )
            remaining = storage.build_guide_cooldown_remaining(
                "Poppy", "SUPPORT", updated + timedelta(minutes=20)
            )
            self.assertEqual(int(remaining.total_seconds()), (23 * 60 + 40) * 60)

            support_guides = storage.load_build_guides_for_position("SUPPORT")
            jungle_guides = storage.load_build_guides_for_position("JUNGLE")
            self.assertEqual(set(support_guides), {"Poppy"})
            self.assertEqual(jungle_guides["Poppy"].patch, "16.16")

    def test_opgg_cache_is_separated_by_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            storage.save_opgg_snapshot(OpggSnapshot(
                enemy_support_id="Poppy", enemy_support_name_ko="뽀삐",
                position="SUPPORT", patch="26.1", updated_at="support",
            ))
            storage.save_opgg_snapshot(OpggSnapshot(
                enemy_support_id="Poppy", enemy_support_name_ko="뽀삐",
                position="JUNGLE", patch="26.2", updated_at="jungle",
            ))
            self.assertEqual(
                storage.load_opgg_snapshot("Poppy", "SUPPORT").updated_at, "support"
            )
            self.assertEqual(
                storage.load_opgg_snapshot("Poppy", "JUNGLE").updated_at, "jungle"
            )
            self.assertEqual(
                set(storage.load_opgg_snapshots_for_position("SUPPORT")), {"Poppy"}
            )
            self.assertEqual(
                storage.load_opgg_snapshots_for_position("JUNGLE")["Poppy"].patch,
                "26.2",
            )

    def test_position_catalog_is_cached_per_role_for_one_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            now = datetime.now()
            storage.save_opgg_position_catalog(
                "SUPPORT", "26.17", ["Leona", "Xerath", "Leona"], now
            )
            self.assertEqual(
                storage.load_opgg_position_catalog("SUPPORT"),
                ("26.17", ["Leona", "Xerath"]),
            )
            self.assertIsNone(storage.load_opgg_position_catalog(
                "SUPPORT", max_age=timedelta(seconds=-1)
            ))
    def test_cooldown_and_personal_matchup_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            now = datetime(2026, 8, 17, 12, 0, 0)
            storage.mark_opgg_success(now)
            remaining = storage.opgg_cooldown_remaining(now + timedelta(minutes=20))
            self.assertEqual(int(remaining.total_seconds()), 40 * 60)
            storage.mark_riot_sync(now)
            riot_remaining = storage.riot_sync_cooldown_remaining(now + timedelta(minutes=4))
            self.assertEqual(
                int(riot_remaining.total_seconds()), (23 * 60 + 56) * 60
            )
            storage.save_matches([
                match_payload("KR_1", True, "Janna", "Leona"),
                match_payload("KR_2", False, "Janna", "Leona"),
                match_payload("KR_3", True, "Braum", "Leona"),
            ])
            stat = storage.personal_stat("mine", "Janna", "Leona")
            self.assertEqual((stat.games, stat.wins, stat.losses), (2, 1, 1))
            self.assertEqual((stat.matchup_games, stat.matchup_wins), (2, 1))
            self.assertAlmostEqual(stat.kda or 0, 6.0)
            jinx_combo = storage.personal_stat(
                "mine", "Janna", "Leona", "Jinx"
            )
            self.assertEqual(
                (jinx_combo.ally_adc_games, jinx_combo.ally_adc_wins), (2, 1)
            )
            self.assertEqual(jinx_combo.ally_adc_win_rate, 50.0)
            batch = storage.personal_stats("mine", ["Janna", "Braum"], "Leona")
            self.assertEqual((batch["Janna"].games, batch["Braum"].games), (2, 1))
            self.assertEqual(storage.relationship_record("mine", "ally"), (3, 2, 0, 0))
            self.assertEqual(storage.relationship_record("mine", "enemy"), (0, 0, 3, 2))
            self.assertEqual(storage.player_champion_record("enemy", "Leona"), (3, 1))
            self.assertEqual(storage.find_puuid_by_riot_id("Enemy#KR2"), "enemy")
            self.assertEqual(storage.pair_same_team_games("mine", "ally"), 3)
            self.assertIn(("Enemy", "KR2"), storage.recent_riot_ids("mine"))
            self.assertEqual(storage.count_player_matches("mine"), 3)
            self.assertEqual(
                {match["metadata"]["matchId"] for match in storage.player_matches("mine")},
                {"KR_1", "KR_2", "KR_3"},
            )

    def test_relationship_summary_includes_recency_and_last_meeting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            storage.save_matches([
                match_payload("KR_OLD", False, "Braum", "Leona", 1000),
                match_payload("KR_MID", True, "Janna", "Leona", 2000),
                match_payload("KR_NEW", True, "Nami", "Leona", 3000),
            ])
            ally = storage.relationship_summary("mine", "ally")
            self.assertEqual((ally["together_games"], ally["together_wins"]), (3, 2))
            self.assertEqual(ally["last_met_game_number"], 1)
            self.assertTrue(ally["last_met_same_team"])
            self.assertTrue(ally["last_met_my_win"])
            self.assertEqual(ally["last_met_my_champion_id"], "Nami")
            self.assertEqual(ally["last_met_other_champion_id"], "Jinx")
            self.assertEqual(ally["recent_10_together_games"], 3)
            latest = storage.latest_player_match("mine")
            self.assertIsNotNone(latest)
            self.assertEqual(latest["champion_id"], "Nami")
            self.assertEqual((latest["kills"], latest["deaths"], latest["assists"]), (2, 2, 10))
            self.assertTrue(latest["won"])

    def test_personal_matchup_stats_follow_current_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            storage.save_matches([
                match_payload(
                    "KR_JGL_WIN", True, "Poppy", "LeeSin", 3000,
                    my_position="JUNGLE", enemy_position="JUNGLE",
                ),
                match_payload(
                    "KR_JGL_LOSS", False, "Poppy", "LeeSin", 2000,
                    my_position="JUNGLE", enemy_position="JUNGLE",
                ),
                match_payload(
                    "KR_SUP", True, "Poppy", "Leona", 1000,
                    my_position="UTILITY", enemy_position="UTILITY",
                ),
            ])
            jungle = storage.personal_stat(
                "mine", "Poppy", "LeeSin", position="JUNGLE"
            )
            support = storage.personal_stat(
                "mine", "Poppy", "Leona", position="SUPPORT"
            )
            self.assertEqual((jungle.games, jungle.matchup_games), (2, 2))
            self.assertEqual((jungle.wins, jungle.matchup_wins), (1, 1))
            self.assertEqual((support.games, support.matchup_games), (1, 1))

    def test_development_key_refresh_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            saved_at = datetime(2026, 8, 17, 12, 0, 0)
            storage.set_riot_api_key("local-development-key", saved_at)
            remaining = storage.riot_api_key_refresh_remaining(
                saved_at + timedelta(hours=23)
            )
            self.assertEqual(int(remaining.total_seconds()), 60 * 60)
            self.assertFalse(storage.riot_api_key_needs_refresh(saved_at + timedelta(hours=23)))
            self.assertTrue(storage.riot_api_key_needs_refresh(saved_at + timedelta(hours=24)))
            storage.mark_riot_api_key_invalid()
            self.assertEqual(storage.riot_api_key_refresh_remaining(saved_at), timedelta(0))


if __name__ == "__main__":
    unittest.main()
