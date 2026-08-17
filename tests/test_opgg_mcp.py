from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from lol_support_advisor.opgg_mcp import (
    mcp_champion_token, parse_champion_synergies_text,
    parse_summoner_matches_text, parse_summoner_profile_text,
)
from lol_support_advisor.storage import Storage


PROFILE_TEXT = """class LolGetSummonerProfile: data
class Data: summoner
class Summoner: game_name,tagline,updated_at,league_stats,ranked_most_champions
class LeagueStat: game_type,tier_info,win,lose
class TierInfo: tier,division,lp
class RankedMostChampions: my_champion_stats
class MyChampionStat: id,play,win,lose,champion_name

LolGetSummonerProfile(Data(Summoner("테스트 유저","KR  1","2026-08-18T01:02:03+09:00",[LeagueStat("SOLORANKED",TierInfo("EMERALD",2,64),31,22)],RankedMostChampions([MyChampionStat(40,12,8,4,"잔나"),MyChampionStat(89,7,3,4,"레오나")]))))
"""

MATCHES_TEXT = """class LolListSummonerMatches: data
class Data: game_history
class GameHistory: id,created_at,game_type,participants
class Participant: summoner,champion_id,champion_name,position,stats
class Summoner: puuid,game_name,tagline
class Stats: kill,death,assist,result,op_score,op_score_rank

LolListSummonerMatches(Data([GameHistory("KR_1","2026-08-18T01:00:00+09:00","SOLORANKED",[Participant(Summoner("puuid-1","테스트 유저","KR  1"),40,"잔나","SUPPORT",Stats(1,2,13,"WIN",7.8,2))]),GameHistory("KR_2","2026-08-17T23:00:00+09:00","SOLORANKED",[Participant(Summoner("puuid-1","테스트 유저","KR  1"),40,"잔나","SUPPORT",Stats(0,4,9,"LOSE",4.2,7))]),GameHistory("KR_3","2026-08-17T22:00:00+09:00","SOLORANKED",[Participant(Summoner("puuid-1","테스트 유저","KR  1"),89,"레오나","SUPPORT",Stats(2,1,0,"UNKNOWN",0.0,0))])]))
"""

SYNERGIES_TEXT = """class LolGetChampionSynergies: champion,my_position,synergy_position,data
class Data: synergies
class Synergie: champion_id,champion_name,position,synergy_champion_id,synergy_champion_name,synergy_position,score_rank,score,play,win,win_rate,synergy_tier_data
class SynergyTierData: tier,rank

LolGetChampionSynergies("징크스","adc","support",Data([Synergie(222,"징크스","ADC",412,"쓰레쉬","SUPPORT",1,0,3817,2122,0.56,SynergyTierData(1,1)),Synergie(222,"징크스","ADC",117,"룰루","SUPPORT",2,0,3531,1872,0.53,SynergyTierData(1,6))]))
"""


class OpggMcpTests(unittest.TestCase):
    def test_synergy_parser_reads_adc_support_win_rate_and_sample(self) -> None:
        names = {412: ("Thresh", "쓰레쉬"), 117: ("Lulu", "룰루")}
        snapshot = parse_champion_synergies_text(
            SYNERGIES_TEXT, requested_champion_id="Jinx",
            key_resolver=lambda key: names[key], fetched_at="2026-08-18T03:00:00",
        )
        self.assertEqual((snapshot.ally_champion_id, snapshot.ally_champion_key), (
            "Jinx", 222,
        ))
        self.assertEqual(len(snapshot.synergies), 2)
        thresh = snapshot.synergy_for("Thresh")
        self.assertIsNotNone(thresh)
        self.assertEqual((thresh.games, thresh.wins, thresh.win_rate), (
            3817, 2122, 56.0,
        ))
        self.assertEqual((thresh.synergy_rank, thresh.synergy_tier), (1, 1))
        self.assertEqual(mcp_champion_token("MissFortune"), "MISS_FORTUNE")
        self.assertEqual(mcp_champion_token("Kaisa"), "KAISA")

    def test_recent_match_parser_reads_form_fields(self) -> None:
        matches = parse_summoner_matches_text(MATCHES_TEXT)
        self.assertEqual(len(matches), 3)
        self.assertEqual((matches[0].match_id, matches[0].champion_key), ("KR_1", 40))
        self.assertEqual((matches[0].kills, matches[0].deaths, matches[0].assists), (
            1, 2, 13,
        ))
        self.assertEqual((matches[0].result, matches[0].op_score_rank), ("WIN", 2))
        self.assertEqual(matches[2].result, "UNKNOWN")

    def test_profile_parser_reads_rank_and_champion_season_record(self) -> None:
        profile = parse_summoner_profile_text(
            PROFILE_TEXT,
            requested_game_name="테스트 유저",
            requested_tag_line="KR1",
            fetched_at="2026-08-18T02:00:00",
        )
        self.assertEqual(profile.riot_id, "테스트 유저#KR1")
        self.assertEqual((profile.tier, profile.division, profile.league_points), (
            "EMERALD", "2", 64,
        ))
        self.assertEqual((profile.season_wins, profile.season_losses), (31, 22))
        janna = profile.champion_stat(40)
        self.assertIsNotNone(janna)
        self.assertEqual((janna.games, janna.wins, janna.losses), (12, 8, 4))
        self.assertIsNone(profile.champion_stat(412))

    def test_profile_cache_is_fresh_for_one_hour(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "advisor.db")
            profile = parse_summoner_profile_text(
                PROFILE_TEXT,
                requested_game_name="테스트 유저",
                requested_tag_line="KR1",
                fetched_at=datetime.now().isoformat(timespec="seconds"),
            )
            storage.save_opgg_player_profile(profile)
            self.assertIsNotNone(storage.load_opgg_player_profile("테스트 유저#kr1"))
            self.assertIsNone(storage.load_opgg_player_profile(
                "테스트 유저#KR1", max_age=timedelta(seconds=-1)
            ))
            self.assertIsNotNone(
                storage.load_opgg_player_profile_any_age("테스트 유저#KR1")
            )


if __name__ == "__main__":
    unittest.main()
