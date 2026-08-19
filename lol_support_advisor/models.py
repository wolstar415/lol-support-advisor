from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any


@dataclass(slots=True)
class DraftMember:
    champion_id: str
    champion_name_ko: str
    role: str = "UNKNOWN"
    state: str = "LOCKED"
    cell_id: int | None = None
    pick_order: int | None = None
    pick_turn: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DraftBan:
    champion_id: str = ""
    champion_name_ko: str = ""
    state: str = "EMPTY"
    actor_cell_id: int | None = None
    order: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DraftSnapshot:
    my_role: str = "SUPPORT"
    my_pick_order: int | None = None
    my_status: str = "WAITING"
    ally_locked: list[DraftMember] = field(default_factory=list)
    ally_hover: list[DraftMember] = field(default_factory=list)
    enemy_locked: list[DraftMember] = field(default_factory=list)
    my_hover: DraftMember | None = None
    ally_team_order: list[DraftMember] = field(default_factory=list)
    enemy_team_order: list[DraftMember] = field(default_factory=list)
    ally_bans: list[str] = field(default_factory=list)
    enemy_bans: list[str] = field(default_factory=list)
    ally_ban_actions: list[DraftBan] = field(default_factory=list)
    enemy_ban_actions: list[DraftBan] = field(default_factory=list)
    selected_enemy_support_id: str | None = None
    selected_enemy_support_name_ko: str | None = None
    selected_enemy_support_source: str = "UNKNOWN"
    local_player_cell_id: int | None = None
    connection_state: str = "DISCONNECTED"
    snapshot_id: str = ""
    pick_order_swap_state: str = ""
    pick_order_swap_target_cell_id: int | None = None

    def unavailable_champions(self) -> list[str]:
        values = [m.champion_id for m in self.ally_locked]
        values += [m.champion_id for m in self.enemy_locked]
        values += [m.champion_id for m in self.ally_hover]
        values += self.ally_bans + self.enemy_bans
        # The user's own hover remains a valid recommendation.
        return sorted({value for value in values if value})

    def payload(self) -> dict[str, Any]:
        lane_opponent = {
            "champion_id": self.selected_enemy_support_id,
            "champion_name_ko": self.selected_enemy_support_name_ko,
            "source": self.selected_enemy_support_source,
            "position": self.my_role,
        }
        return {
            "my_role": self.my_role,
            "my_pick_order": self.my_pick_order,
            "my_status": self.my_status,
            "local_player_cell_id": self.local_player_cell_id,
            "ally_locked": [m.to_dict() for m in self.ally_locked],
            "ally_hover": [m.to_dict() for m in self.ally_hover],
            "my_hover": self.my_hover.to_dict() if self.my_hover else None,
            "enemy_locked": [m.to_dict() for m in self.enemy_locked],
            "ally_team_order": [m.to_dict() for m in self.ally_team_order],
            "enemy_team_order": [m.to_dict() for m in self.enemy_team_order],
            "selected_enemy_support": {
                "champion_id": self.selected_enemy_support_id,
                "champion_name_ko": self.selected_enemy_support_name_ko,
                "source": self.selected_enemy_support_source,
            },
            "selected_lane_opponent": lane_opponent,
            "ally_bans": self.ally_bans,
            "enemy_bans": self.enemy_bans,
            "ally_ban_actions": [item.to_dict() for item in self.ally_ban_actions],
            "enemy_ban_actions": [item.to_dict() for item in self.enemy_ban_actions],
            "unavailable_champions": self.unavailable_champions(),
        }

    def refresh_snapshot_id(self) -> str:
        raw = json.dumps(self.payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = sha256(raw).hexdigest()[:10].upper()
        self.snapshot_id = f"DRAFT-{digest}"
        return self.snapshot_id


@dataclass(slots=True)
class OpggCounter:
    champion_id: str
    champion_name_ko: str
    versus_win_rate: float
    games: int
    overall_win_rate: float | None = None
    ally_adc_win_rate: float | None = None
    status: str = "AVAILABLE"
    position_rank: int | None = None
    pick_rate: float | None = None
    ban_rate: float | None = None
    # Some matchup providers expose a separate laning metric. OP.GG's current
    # table parser does not always receive it, so this must remain optional and
    # must never be substituted with the normal game win rate.
    laning_win_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OpggSnapshot:
    enemy_support_id: str | None
    enemy_support_name_ko: str | None
    position: str = "SUPPORT"
    region: str = "GLOBAL"
    tier: str = "EMERALD_PLUS"
    patch: str = "UNKNOWN"
    updated_at: str = ""
    source_url: str = ""
    counters: list[OpggCounter] = field(default_factory=list)
    weak_picks: list[OpggCounter] = field(default_factory=list)
    target_overall_win_rate: float | None = None
    target_pick_rate: float | None = None
    target_ban_rate: float | None = None
    raw_status: str = "NO_DATA"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "counters": [c.to_dict() for c in self.counters],
            "weak_picks": [c.to_dict() for c in self.weak_picks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpggSnapshot":
        payload = dict(data)
        payload["counters"] = [OpggCounter(**item) for item in data.get("counters", [])]
        payload["weak_picks"] = [OpggCounter(**item) for item in data.get("weak_picks", [])]
        return cls(**payload)


@dataclass(slots=True)
class OpggSynergyStat:
    champion_key: int
    champion_id: str
    champion_name_ko: str
    games: int = 0
    wins: int = 0
    win_rate: float | None = None
    synergy_rank: int = 0
    synergy_tier: int = 4
    tier_rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OpggSynergySnapshot:
    ally_champion_key: int
    ally_champion_id: str
    ally_champion_name_ko: str
    ally_position: str = "BOTTOM"
    candidate_position: str = "SUPPORT"
    fetched_at: str = ""
    synergies: list[OpggSynergyStat] = field(default_factory=list)
    status: str = "NO_DATA"

    def synergy_for(self, champion_id: str) -> OpggSynergyStat | None:
        return next(
            (item for item in self.synergies if item.champion_id == champion_id),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "synergies": [item.to_dict() for item in self.synergies],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpggSynergySnapshot":
        payload = dict(data)
        payload["synergies"] = [
            OpggSynergyStat(**item) for item in data.get("synergies", [])
        ]
        return cls(**payload)


@dataclass(slots=True)
class OpggPlayerChampionStat:
    champion_id: str
    champion_name_ko: str
    wins: int = 0
    losses: int = 0
    page_updated_text: str = ""
    fetched_at: str = ""
    source_url: str = ""
    status: str = "NO_DATA"

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games * 100 if self.games else None


@dataclass(slots=True)
class OpggMcpChampionStat:
    champion_key: int
    champion_name: str
    games: int = 0
    wins: int = 0
    losses: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OpggMcpRecentMatch:
    match_id: str
    created_at: str
    game_type: str
    champion_key: int
    champion_name: str
    position: str
    result: str
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    op_score: float = 0.0
    op_score_rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OpggMcpSummonerProfile:
    riot_id: str
    game_name: str
    tag_line: str
    region: str = "KR"
    tier: str = "UNRANKED"
    division: str = ""
    league_points: int = 0
    season_wins: int = 0
    season_losses: int = 0
    source_updated_at: str = ""
    fetched_at: str = ""
    champion_stats: list[OpggMcpChampionStat] = field(default_factory=list)
    recent_matches: list[OpggMcpRecentMatch] = field(default_factory=list)
    recent_matches_status: str = "NO_DATA"
    status: str = "NO_DATA"

    def champion_stat(self, champion_key: int) -> OpggMcpChampionStat | None:
        return next(
            (
                stat for stat in self.champion_stats
                if int(stat.champion_key) == int(champion_key)
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "champion_stats": [stat.to_dict() for stat in self.champion_stats],
            "recent_matches": [match.to_dict() for match in self.recent_matches],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpggMcpSummonerProfile":
        payload = dict(data)
        payload["champion_stats"] = [
            OpggMcpChampionStat(**item)
            for item in data.get("champion_stats", [])
        ]
        payload["recent_matches"] = [
            OpggMcpRecentMatch(**item)
            for item in data.get("recent_matches", [])
        ]
        return cls(**payload)


@dataclass(slots=True)
class BuildAsset:
    asset_id: int
    name: str
    icon_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RuneBuild:
    name: str
    primary_style_id: int
    sub_style_id: int
    perks: list[BuildAsset] = field(default_factory=list)
    games: int | None = None
    win_rate: float | None = None
    pick_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "perks": [perk.to_dict() for perk in self.perks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuneBuild":
        payload = dict(data)
        payload["perks"] = [BuildAsset(**item) for item in data.get("perks", [])]
        return cls(**payload)


@dataclass(slots=True)
class SummonerSpellBuild:
    name: str
    spells: list[BuildAsset] = field(default_factory=list)
    games: int | None = None
    win_rate: float | None = None
    pick_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "spells": [spell.to_dict() for spell in self.spells]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummonerSpellBuild":
        payload = dict(data)
        payload["spells"] = [BuildAsset(**item) for item in data.get("spells", [])]
        return cls(**payload)


@dataclass(slots=True)
class BuildItemGroup:
    title: str
    items: list[BuildAsset] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "items": [item.to_dict() for item in self.items]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BuildItemGroup":
        payload = dict(data)
        payload["items"] = [BuildAsset(**item) for item in data.get("items", [])]
        return cls(**payload)


@dataclass(slots=True)
class ChampionBuildGuide:
    champion_id: str
    champion_name_ko: str
    position: str
    patch: str = "UNKNOWN"
    tier: str = "EMERALD_PLUS"
    updated_at: str = ""
    source_url: str = ""
    rune_builds: list[RuneBuild] = field(default_factory=list)
    summoner_spells: list[BuildAsset] = field(default_factory=list)
    summoner_spell_builds: list[SummonerSpellBuild] = field(default_factory=list)
    skill_priority: list[str] = field(default_factory=list)
    skill_sequence: list[str] = field(default_factory=list)
    item_groups: list[BuildItemGroup] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "rune_builds": [build.to_dict() for build in self.rune_builds],
            "summoner_spells": [spell.to_dict() for spell in self.summoner_spells],
            "summoner_spell_builds": [
                build.to_dict() for build in self.summoner_spell_builds
            ],
            "item_groups": [group.to_dict() for group in self.item_groups],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChampionBuildGuide":
        payload = dict(data)
        payload["rune_builds"] = [
            RuneBuild.from_dict(item) for item in data.get("rune_builds", [])
        ]
        payload["summoner_spells"] = [
            BuildAsset(**item) for item in data.get("summoner_spells", [])
        ]
        payload["summoner_spell_builds"] = [
            SummonerSpellBuild.from_dict(item)
            for item in data.get("summoner_spell_builds", [])
        ]
        if not payload["summoner_spell_builds"] and payload["summoner_spells"]:
            payload["summoner_spell_builds"] = [SummonerSpellBuild(
                name="추천 스펠 1",
                spells=list(payload["summoner_spells"]),
            )]
        elif payload["summoner_spell_builds"] and not payload["summoner_spells"]:
            payload["summoner_spells"] = list(
                payload["summoner_spell_builds"][0].spells
            )
        payload["item_groups"] = [
            BuildItemGroup.from_dict(item) for item in data.get("item_groups", [])
        ]
        return cls(**payload)


@dataclass(slots=True)
class Recommendation:
    rank: int
    champion_id: str
    champion_name_ko: str
    style: str
    blind_safety: str
    reason: str
    team_synergy: str
    lane_plan: str
    watch_for: str


@dataclass(slots=True)
class PersonalStat:
    games: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    kda: float | None = None
    vision_score: float | None = None
    matchup_games: int = 0
    matchup_wins: int = 0
    matchup_losses: int = 0
    matchup_win_rate: float | None = None
    ally_adc_games: int = 0
    ally_adc_wins: int = 0
    ally_adc_losses: int = 0
    ally_adc_win_rate: float | None = None

    @property
    def matchup_confidence(self) -> str:
        if self.matchup_games == 0:
            return "데이터 없음"
        if self.matchup_games < 5:
            return "표본 매우 적음"
        if self.matchup_games < 10:
            return "표본 적음"
        if self.matchup_games < 20:
            return "표본 보통"
        return "표본 충분"


@dataclass(slots=True)
class LivePlayer:
    champion_id: str
    champion_name_ko: str
    riot_game_name: str
    riot_tag_line: str
    team: str
    position: str = "UNKNOWN"
    level: int = 1
    is_active_player: bool = False
    draft_pick_turn: int | None = None
    draft_team_pick_order: int | None = None

    @property
    def riot_id(self) -> str:
        if self.riot_tag_line:
            return f"{self.riot_game_name}#{self.riot_tag_line}"
        return self.riot_game_name


@dataclass(slots=True)
class LiveGameSnapshot:
    players: list[LivePlayer] = field(default_factory=list)
    active_riot_id: str = ""
    active_team: str = "ORDER"
    game_time: float = 0.0
    game_mode: str = ""

    @property
    def allies(self) -> list[LivePlayer]:
        return [player for player in self.players if player.team == self.active_team]

    @property
    def enemies(self) -> list[LivePlayer]:
        return [player for player in self.players if player.team != self.active_team]


@dataclass(slots=True)
class GamePrediction:
    prediction_key: str
    captured_at: str
    active_riot_id: str
    active_champion_id: str
    ally_champion_ids: tuple[str, ...]
    enemy_champion_ids: tuple[str, ...]
    ally_riot_ids: tuple[str, ...]
    enemy_riot_ids: tuple[str, ...]
    win_probability: float
    predicted_win: bool
    confidence: str
    evidence: tuple[str, ...] = ()
    evidence_score: float = 0.0
    match_id: str = ""
    actual_win: bool | None = None

    @property
    def correct(self) -> bool | None:
        if self.actual_win is None:
            return None
        return self.predicted_win == self.actual_win

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GamePrediction":
        payload = dict(data)
        for key in (
            "ally_champion_ids", "enemy_champion_ids",
            "ally_riot_ids", "enemy_riot_ids", "evidence",
        ):
            payload[key] = tuple(str(value) for value in payload.get(key, ()))
        return cls(**payload)


@dataclass(slots=True)
class LaneMatchupStat:
    position: str
    ally_champion_id: str
    ally_champion_name_ko: str
    enemy_champion_id: str
    enemy_champion_name_ko: str
    ally_win_rate: float | None = None
    ally_laning_win_rate: float | None = None
    games: int = 0
    patch: str = "UNKNOWN"
    updated_at: str = ""
    status: str = "LOADING"
    cached: bool = False
    message: str = ""

    @property
    def enemy_win_rate(self) -> float | None:
        if self.ally_win_rate is None:
            return None
        return round(100.0 - self.ally_win_rate, 2)

    @property
    def enemy_laning_win_rate(self) -> float | None:
        if self.ally_laning_win_rate is None:
            return None
        return round(100.0 - self.ally_laning_win_rate, 2)


@dataclass(slots=True)
class JungleTendencyStat:
    """Evidence-backed jungle tendencies computed from cached solo queue games."""

    puuid: str = ""
    champion_id: str = ""
    games: int = 0
    champion_specific: bool = False
    early_takedowns: float | None = None
    early_lane_kills: float | None = None
    jungle_cs_10: float | None = None
    enemy_jungle_cs: float | None = None
    spawn_objectives: float | None = None
    first_blood_kill_rate: float | None = None
    first_blood_assist_rate: float | None = None
    average_deaths: float | None = None
    wins: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    labels: list[str] = field(default_factory=list)
    status: str = "NO_DATA"
    message: str = ""

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games * 100.0 if self.games else None

    @property
    def kda(self) -> float | None:
        if not self.games:
            return None
        return (self.kills + self.assists) / max(self.deaths, 1)


@dataclass(slots=True)
class PlayerBehaviorStat:
    """Recent role behavior derived from Riot match participant evidence."""

    puuid: str = ""
    champion_id: str = ""
    position: str = "UNKNOWN"
    games: int = 0
    champion_specific: bool = False
    first_blood_kills: int = 0
    first_blood_assists: int = 0
    first_blood_rate: float | None = None
    early_advantage_rate: float | None = None
    early_takedowns: float | None = None
    kill_participation: float | None = None
    average_deaths: float | None = None
    vision_per_minute: float | None = None
    control_wards: float | None = None
    crowd_control_seconds: float | None = None
    tanking_per_minute: float | None = None
    champion_damage_per_minute: float | None = None
    ally_protection_per_minute: float | None = None
    objective_damage_per_minute: float | None = None
    turret_damage_per_minute: float | None = None
    labels: list[str] = field(default_factory=list)
    status: str = "NO_DATA"
    message: str = ""


@dataclass(slots=True)
class PlayerProfileStat:
    puuid: str = ""
    queue_type: str = "RANKED_SOLO_5x5"
    tier: str = "UNRANKED"
    rank: str = ""
    league_points: int = 0
    season_wins: int = 0
    season_losses: int = 0
    champion_games: int = 0
    champion_wins: int = 0
    local_sample_games: int = 0
    champion_data_source: str = "LOCAL"
    champion_sample_target: int = 0
    champion_source_detail: str = ""
    recent_games: int = 0
    recent_wins: int = 0
    recent_kills: int = 0
    recent_deaths: int = 0
    recent_assists: int = 0
    recent_op_score: float = 0.0
    last_op_score_rank: int = 0
    overall_streak: int = 0
    champion_recent_games: int = 0
    champion_recent_wins: int = 0
    champion_streak: int = 0
    recent_form_source: str = ""
    together_games: int = 0
    together_wins: int = 0
    against_games: int = 0
    against_my_wins: int = 0
    recent_10_together_games: int = 0
    recent_10_against_games: int = 0
    last_met_game_number: int = 0
    last_met_same_team: bool | None = None
    last_met_my_win: bool | None = None
    last_met_my_champion_id: str = ""
    last_met_other_champion_id: str = ""
    last_game_champion_id: str = ""
    last_game_position: str = "UNKNOWN"
    last_game_kills: int = 0
    last_game_deaths: int = 0
    last_game_assists: int = 0
    last_game_won: bool | None = None
    sample_scope: str = "최근 저장된 솔로랭크"
    updated_at: str = ""
    status: str = "NO_DATA"

    @staticmethod
    def rate(wins: int, games: int) -> float | None:
        return wins / games * 100 if games else None

    @property
    def season_win_rate(self) -> float | None:
        return self.rate(self.season_wins, self.season_wins + self.season_losses)

    @property
    def champion_win_rate(self) -> float | None:
        return self.rate(self.champion_wins, self.champion_games)

    @property
    def recent_win_rate(self) -> float | None:
        return self.rate(self.recent_wins, self.recent_games)

    @property
    def recent_kda(self) -> float | None:
        if not self.recent_games:
            return None
        return (self.recent_kills + self.recent_assists) / max(self.recent_deaths, 1)

    @property
    def together_win_rate(self) -> float | None:
        return self.rate(self.together_wins, self.together_games)

    @property
    def against_my_win_rate(self) -> float | None:
        return self.rate(self.against_my_wins, self.against_games)

    @property
    def last_game_kda(self) -> float | None:
        if not self.last_game_champion_id:
            return None
        return (self.last_game_kills + self.last_game_assists) / max(self.last_game_deaths, 1)
