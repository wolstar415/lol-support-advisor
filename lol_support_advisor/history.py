from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class MatchHistoryEntry:
    match_id: str
    game_creation: int
    duration_seconds: int
    queue_id: int
    champion_id: str
    position: str
    won: bool
    kills: int
    deaths: int
    assists: int
    kda: float
    cs: int
    cs_per_minute: float
    vision_score: int
    damage_to_champions: int
    damage_taken: int
    gold_earned: int
    kill_participation: float | None
    items: tuple[int, ...]
    ally_champions: tuple[str, ...]
    enemy_champions: tuple[str, ...]


@dataclass(slots=True)
class ChampionHistoryStat:
    champion_id: str
    games: int = 0
    wins: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    vision_score: int = 0

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games * 100 if self.games else None

    @property
    def kda(self) -> float | None:
        return (self.kills + self.assists) / max(self.deaths, 1) if self.games else None

    @property
    def average_vision(self) -> float | None:
        return self.vision_score / self.games if self.games else None


@dataclass(slots=True)
class HistoryOverview:
    entries: list[MatchHistoryEntry] = field(default_factory=list)
    champions: list[ChampionHistoryStat] = field(default_factory=list)
    games: int = 0
    wins: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    total_vision: int = 0
    recent_20_games: int = 0
    recent_20_wins: int = 0
    current_streak: int = 0

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games * 100 if self.games else None

    @property
    def kda(self) -> float | None:
        return (self.kills + self.assists) / max(self.deaths, 1) if self.games else None

    @property
    def average_vision(self) -> float | None:
        return self.total_vision / self.games if self.games else None

    @property
    def recent_20_win_rate(self) -> float | None:
        return (
            self.recent_20_wins / self.recent_20_games * 100
            if self.recent_20_games else None
        )


def _integer(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def analyze_history(
    matches: Iterable[dict[str, Any]], puuid: str, limit: int = 1000
) -> HistoryOverview:
    overview = HistoryOverview()
    champion_totals: dict[str, ChampionHistoryStat] = {}
    for match in matches:
        info = match.get("info") or {}
        participants = info.get("participants") or []
        mine = next((row for row in participants if row.get("puuid") == puuid), None)
        if not mine:
            continue
        team_id = mine.get("teamId")
        allies = [row for row in participants if row.get("teamId") == team_id]
        enemies = [row for row in participants if row.get("teamId") != team_id]
        duration = max(_integer(info, "gameDuration"), _integer(mine, "timePlayed"), 1)
        kills = _integer(mine, "kills")
        deaths = _integer(mine, "deaths")
        assists = _integer(mine, "assists")
        cs = _integer(mine, "totalMinionsKilled") + _integer(mine, "neutralMinionsKilled")
        team_kills = sum(_integer(row, "kills") for row in allies)
        participation = (
            (kills + assists) / team_kills * 100 if team_kills else None
        )
        champion_id = str(mine.get("championName") or "Unknown")
        won = bool(mine.get("win"))
        entry = MatchHistoryEntry(
            match_id=str((match.get("metadata") or {}).get("matchId") or info.get("gameId") or ""),
            game_creation=_integer(info, "gameCreation"),
            duration_seconds=duration,
            queue_id=_integer(info, "queueId"),
            champion_id=champion_id,
            position=str(
                mine.get("teamPosition") or mine.get("individualPosition") or "UNKNOWN"
            ).upper(),
            won=won,
            kills=kills,
            deaths=deaths,
            assists=assists,
            kda=(kills + assists) / max(deaths, 1),
            cs=cs,
            cs_per_minute=cs / max(duration / 60.0, 1.0),
            vision_score=_integer(mine, "visionScore"),
            damage_to_champions=_integer(mine, "totalDamageDealtToChampions"),
            damage_taken=_integer(mine, "totalDamageTaken"),
            gold_earned=_integer(mine, "goldEarned"),
            kill_participation=participation,
            items=tuple(
                item_id for item_id in (_integer(mine, f"item{index}") for index in range(7))
                if item_id
            ),
            ally_champions=tuple(str(row.get("championName") or "Unknown") for row in allies),
            enemy_champions=tuple(str(row.get("championName") or "Unknown") for row in enemies),
        )
        overview.entries.append(entry)
        overview.games += 1
        overview.wins += int(won)
        overview.kills += kills
        overview.deaths += deaths
        overview.assists += assists
        overview.total_vision += entry.vision_score
        champion = champion_totals.setdefault(
            champion_id, ChampionHistoryStat(champion_id=champion_id)
        )
        champion.games += 1
        champion.wins += int(won)
        champion.kills += kills
        champion.deaths += deaths
        champion.assists += assists
        champion.vision_score += entry.vision_score
        if overview.games >= limit:
            break

    recent = overview.entries[:20]
    overview.recent_20_games = len(recent)
    overview.recent_20_wins = sum(int(entry.won) for entry in recent)
    if overview.entries:
        first_result = overview.entries[0].won
        for entry in overview.entries:
            if entry.won != first_result:
                break
            overview.current_streak += 1 if first_result else -1
    overview.champions = sorted(
        champion_totals.values(),
        key=lambda stat: (stat.games, stat.wins, stat.kda or 0.0),
        reverse=True,
    )
    return overview
