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
    ally_bans: list[str] = field(default_factory=list)
    enemy_bans: list[str] = field(default_factory=list)
    selected_enemy_support_id: str | None = None
    selected_enemy_support_name_ko: str | None = None
    selected_enemy_support_source: str = "UNKNOWN"
    local_player_cell_id: int | None = None
    connection_state: str = "DISCONNECTED"
    snapshot_id: str = ""

    def unavailable_champions(self) -> list[str]:
        values = [m.champion_id for m in self.ally_locked]
        values += [m.champion_id for m in self.enemy_locked]
        values += [m.champion_id for m in self.ally_hover]
        values += self.ally_bans + self.enemy_bans
        # The user's own hover remains a valid recommendation.
        return sorted({value for value in values if value})

    def payload(self) -> dict[str, Any]:
        return {
            "my_role": self.my_role,
            "my_pick_order": self.my_pick_order,
            "my_status": self.my_status,
            "ally_locked": [m.to_dict() for m in self.ally_locked],
            "ally_hover": [m.to_dict() for m in self.ally_hover],
            "my_hover": self.my_hover.to_dict() if self.my_hover else None,
            "enemy_locked": [m.to_dict() for m in self.enemy_locked],
            "selected_enemy_support": {
                "champion_id": self.selected_enemy_support_id,
                "champion_name_ko": self.selected_enemy_support_name_ko,
                "source": self.selected_enemy_support_source,
            },
            "ally_bans": self.ally_bans,
            "enemy_bans": self.enemy_bans,
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OpggSnapshot:
    enemy_support_id: str | None
    enemy_support_name_ko: str | None
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
