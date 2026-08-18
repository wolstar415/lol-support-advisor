from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


PERFORMANCE_BADGE_CODES = (
    "CC", "VISION", "TANKING", "DAMAGE", "TEAMPLAY",
    "PERFECT_KDA", "KILL_CARRY", "ASSIST_MASTER", "PROTECTOR",
    "OBJECTIVE", "SIEGE", "WARD_CLEAR", "SURVIVOR", "KILLING_SPREE",
    "FIRST_BLOOD", "OBJECTIVE_STEAL", "FARM",
)


@dataclass(slots=True, frozen=True)
class RankSnapshot:
    """One observed solo-queue rank state.

    Match-v5 does not carry LP.  These observations deliberately live outside
    the raw match payload so callers can retain a before/after audit trail.
    """

    snapshot_id: int
    puuid: str
    observed_at: str
    stage: str
    session_key: str
    tier: str
    division: str
    league_points: int
    wins: int
    losses: int
    source: str = "RIOT_LEAGUE_V4"

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def rank_text(self) -> str:
        tier = self.tier.strip().upper() or "UNRANKED"
        division = self.division.strip().upper()
        suffix = f" {division}" if division else ""
        return f"{tier}{suffix} {self.league_points}LP"


@dataclass(slots=True, frozen=True)
class MatchLpChange:
    """A conservative rank transition linked to exactly one solo match."""

    match_id: str
    puuid: str
    before_snapshot_id: int
    after_snapshot_id: int
    before_tier: str
    before_division: str
    before_lp: int
    after_tier: str
    after_division: str
    after_lp: int
    lp_delta: int | None
    confidence: str
    resolved_at: str

    @staticmethod
    def _rank_text(tier: str, division: str, lp: int) -> str:
        normalized_tier = tier.strip().upper() or "UNRANKED"
        normalized_division = division.strip().upper()
        suffix = f" {normalized_division}" if normalized_division else ""
        return f"{normalized_tier}{suffix} {lp}LP"

    @property
    def before_rank_text(self) -> str:
        return self._rank_text(
            self.before_tier, self.before_division, self.before_lp,
        )

    @property
    def after_rank_text(self) -> str:
        return self._rank_text(
            self.after_tier, self.after_division, self.after_lp,
        )


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
    summoner_spell_ids: tuple[int, ...] = ()
    primary_rune_id: int = 0
    secondary_rune_style_id: int = 0
    ally_players: tuple[tuple[str, str], ...] = ()
    enemy_players: tuple[tuple[str, str], ...] = ()
    damage_self_mitigated: int = 0
    time_ccing_others: int = 0
    wards_placed: int = 0
    control_wards_placed: int = 0
    wards_killed: int = 0
    healing_on_teammates: int = 0
    shielding_on_teammates: int = 0
    damage_to_objectives: int = 0
    damage_to_turrets: int = 0
    turret_kills: int = 0
    objectives_stolen: int = 0
    largest_killing_spree: int = 0
    largest_multi_kill: int = 0
    first_blood_participation: bool = False
    performance_badges: tuple[str, ...] = ()
    predicted_win_rate: float | None = None
    predicted_win: bool | None = None
    prediction_confidence: str = ""
    prediction_correct: bool | None = None
    lp_delta: int | None = None
    lp_confidence: str = ""
    lp_before_rank: str = ""
    lp_after_rank: str = ""


@dataclass(slots=True)
class ChampionHistoryStat:
    champion_id: str
    position: str = "UNKNOWN"
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
    recent_20_lp_sum: int | None = None
    recent_20_lp_known_games: int = 0
    recent_20_lp_inferred_games: int = 0
    recent_20_champions: list[ChampionHistoryStat] = field(default_factory=list)
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


def refresh_recent_20_summary(overview: HistoryOverview) -> None:
    """Recompute recent summary fields after optional LP data is attached."""
    recent = overview.entries[:20]
    overview.recent_20_games = len(recent)
    overview.recent_20_wins = sum(int(entry.won) for entry in recent)
    known_lp = [entry.lp_delta for entry in recent if entry.lp_delta is not None]
    overview.recent_20_lp_known_games = len(known_lp)
    overview.recent_20_lp_sum = sum(known_lp) if known_lp else None
    overview.recent_20_lp_inferred_games = sum(
        1 for entry in recent
        if entry.lp_delta is not None and entry.lp_confidence == "INFERRED"
    )

    champion_totals: dict[str, ChampionHistoryStat] = {}
    for entry in recent:
        champion = champion_totals.setdefault(
            entry.champion_id,
            ChampionHistoryStat(champion_id=entry.champion_id, position="ALL"),
        )
        champion.games += 1
        champion.wins += int(entry.won)
        champion.kills += entry.kills
        champion.deaths += entry.deaths
        champion.assists += entry.assists
        champion.vision_score += entry.vision_score
    overview.recent_20_champions = sorted(
        champion_totals.values(),
        key=lambda stat: (stat.games, stat.wins, stat.kda or 0.0),
        reverse=True,
    )[:3]

    overview.current_streak = 0
    if overview.entries:
        first_result = overview.entries[0].won
        for entry in overview.entries:
            if entry.won != first_result:
                break
            overview.current_streak += 1 if first_result else -1


def attach_match_lp_changes(
    overview: HistoryOverview,
    changes: dict[str, MatchLpChange],
) -> HistoryOverview:
    """Attach one batch-loaded LP map without inventing zeroes for old games."""
    for entry in overview.entries:
        change = changes.get(entry.match_id)
        if change is None:
            continue
        entry.lp_delta = change.lp_delta
        entry.lp_confidence = change.confidence
        entry.lp_before_rank = change.before_rank_text
        entry.lp_after_rank = change.after_rank_text
    refresh_recent_20_summary(overview)
    return overview


def _integer(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _riot_id(payload: dict[str, Any]) -> str:
    game_name = str(
        payload.get("riotIdGameName") or payload.get("summonerName") or "이름 미상"
    ).strip()
    tag_line = str(
        payload.get("riotIdTagline") or payload.get("riotIdTagLine") or ""
    ).strip()
    return f"{game_name}#{tag_line}" if tag_line else game_name


def _position(value: Any) -> str:
    normalized = str(value or "UNKNOWN").upper()
    return {
        "UTILITY": "SUPPORT",
        "SUP": "SUPPORT",
        "ADC": "BOTTOM",
        "MID": "MIDDLE",
        "JGL": "JUNGLE",
    }.get(normalized, normalized)


def _rune_loadout(payload: dict[str, Any]) -> tuple[int, int]:
    styles = (payload.get("perks") or {}).get("styles") or []
    primary = next(
        (
            int((style.get("selections") or [{}])[0].get("perk") or 0)
            for style in styles
            if str(style.get("description") or "").casefold() == "primary"
            and style.get("selections")
        ),
        0,
    )
    secondary_style = next(
        (
            int(style.get("style") or 0)
            for style in styles
            if str(style.get("description") or "").casefold() == "substyle"
        ),
        int(payload.get("perkSubStyle") or 0),
    )
    return primary, secondary_style


def _team_rank(
    mine: dict[str, Any], allies: list[dict[str, Any]], key: str,
) -> int:
    value = _integer(mine, key)
    return 1 + sum(_integer(row, key) > value for row in allies if row is not mine)


def _team_combined_rank(
    mine: dict[str, Any], allies: list[dict[str, Any]], keys: tuple[str, ...],
) -> int:
    value = sum(_integer(mine, key) for key in keys)
    return 1 + sum(
        sum(_integer(row, key) for key in keys) > value
        for row in allies if row is not mine
    )


def _performance_badges(
    mine: dict[str, Any], allies: list[dict[str, Any]], duration: int,
    position: str, kill_participation: float | None,
) -> tuple[str, ...]:
    """Return conservative, explainable match-card achievement codes.

    These are not opaque ratings. A badge needs both an absolute per-minute
    floor and a top-two team rank so short games and inflated single metrics do
    not award everything at once.
    """
    minutes = max(duration / 60.0, 1.0)
    cc_time = _integer(mine, "timeCCingOthers")
    vision = _integer(mine, "visionScore")
    wards = _integer(mine, "wardsPlaced")
    mitigated = _integer(mine, "damageSelfMitigated")
    damage_taken = _integer(mine, "totalDamageTaken")
    damage = _integer(mine, "totalDamageDealtToChampions")
    kills = _integer(mine, "kills")
    deaths = _integer(mine, "deaths")
    assists = _integer(mine, "assists")
    takedowns = kills + assists
    wards_killed = _integer(mine, "wardsKilled")
    teammate_healing = _integer(mine, "totalHealsOnTeammates")
    teammate_shielding = _integer(mine, "totalDamageShieldedOnTeammates")
    protection = teammate_healing + teammate_shielding
    objective_damage = _integer(mine, "damageDealtToObjectives")
    turret_damage = _integer(mine, "damageDealtToTurrets")
    turret_kills = _integer(mine, "turretKills")
    objectives_stolen = _integer(mine, "objectivesStolen")
    largest_spree = _integer(mine, "largestKillingSpree")
    largest_multi = _integer(mine, "largestMultiKill")
    first_blood = bool(mine.get("firstBloodKill") or mine.get("firstBloodAssist"))
    cs = _integer(mine, "totalMinionsKilled") + _integer(
        mine, "neutralMinionsKilled"
    )
    badges: list[str] = []

    # Put genuinely rare achievements first so a steal or flawless KDA is not
    # hidden behind three common contribution badges.
    if objectives_stolen >= 1:
        badges.append("OBJECTIVE_STEAL")
    if deaths == 0 and takedowns >= 8:
        badges.append("PERFECT_KDA")
    if (
        cc_time >= max(8, int(minutes * 0.35))
        and _team_rank(mine, allies, "timeCCingOthers") <= 2
    ):
        badges.append("CC")
    vision_floor = 1.35 if position == "SUPPORT" else 0.65
    if (
        vision / minutes >= vision_floor
        and wards >= max(4, int(minutes * 0.35))
        and _team_rank(mine, allies, "visionScore") <= 2
    ):
        badges.append("VISION")
    if (
        damage_taken / minutes >= 350
        and mitigated / minutes >= 250
        and _team_rank(mine, allies, "damageSelfMitigated") <= 2
    ):
        badges.append("TANKING")
    if (
        damage / minutes >= 550
        and _team_rank(mine, allies, "totalDamageDealtToChampions") <= 2
    ):
        badges.append("DAMAGE")
    if (
        protection >= max(1500, int(minutes * 75))
        and _team_combined_rank(
            mine, allies,
            ("totalHealsOnTeammates", "totalDamageShieldedOnTeammates"),
        ) <= 2
    ):
        badges.append("PROTECTOR")
    if kill_participation is not None and kill_participation >= 70:
        badges.append("TEAMPLAY")
    if (
        objective_damage >= max(3000, int(minutes * 200))
        and _team_rank(mine, allies, "damageDealtToObjectives") <= 2
    ):
        badges.append("OBJECTIVE")
    if (
        turret_kills >= 2
        or (
            turret_damage >= max(2000, int(minutes * 100))
            and _team_rank(mine, allies, "damageDealtToTurrets") <= 2
        )
    ):
        badges.append("SIEGE")
    if (
        wards_killed >= max(4, int(minutes * 0.2))
        and _team_rank(mine, allies, "wardsKilled") <= 2
    ):
        badges.append("WARD_CLEAR")
    if kills >= 8 and _team_rank(mine, allies, "kills") <= 2:
        badges.append("KILL_CARRY")
    if assists >= 15 and _team_rank(mine, allies, "assists") <= 2:
        badges.append("ASSIST_MASTER")
    if minutes >= 18 and deaths <= 2 and takedowns >= 8:
        badges.append("SURVIVOR")
    if largest_spree >= 5 or largest_multi >= 2:
        badges.append("KILLING_SPREE")
    if first_blood:
        badges.append("FIRST_BLOOD")
    farm_floor = {
        "TOP": 6.5, "MIDDLE": 7.0, "BOTTOM": 7.0, "JUNGLE": 5.5,
    }.get(position)
    if (
        farm_floor is not None and cs / minutes >= farm_floor
        and _team_combined_rank(
            mine, allies, ("totalMinionsKilled", "neutralMinionsKilled")
        ) <= 2
    ):
        badges.append("FARM")
    return tuple(badges[:3])


def analyze_history(
    matches: Iterable[dict[str, Any]], puuid: str, limit: int = 1000
) -> HistoryOverview:
    overview = HistoryOverview()
    champion_totals: dict[tuple[str, str], ChampionHistoryStat] = {}
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
        position = _position(
            mine.get("teamPosition") or mine.get("individualPosition")
        )
        won = bool(mine.get("win"))
        primary_rune_id, secondary_rune_style_id = _rune_loadout(mine)
        performance_badges = _performance_badges(
            mine, allies, duration, position, participation,
        )
        entry = MatchHistoryEntry(
            match_id=str((match.get("metadata") or {}).get("matchId") or info.get("gameId") or ""),
            game_creation=_integer(info, "gameCreation"),
            duration_seconds=duration,
            queue_id=_integer(info, "queueId"),
            champion_id=champion_id,
            position=position,
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
            summoner_spell_ids=tuple(
                spell_id for spell_id in (
                    _integer(mine, "summoner1Id"), _integer(mine, "summoner2Id")
                ) if spell_id
            ),
            primary_rune_id=primary_rune_id,
            secondary_rune_style_id=secondary_rune_style_id,
            ally_players=tuple(
                (str(row.get("championName") or "Unknown"), _riot_id(row))
                for row in allies
            ),
            enemy_players=tuple(
                (str(row.get("championName") or "Unknown"), _riot_id(row))
                for row in enemies
            ),
            damage_self_mitigated=_integer(mine, "damageSelfMitigated"),
            time_ccing_others=_integer(mine, "timeCCingOthers"),
            wards_placed=_integer(mine, "wardsPlaced"),
            control_wards_placed=_integer(mine, "detectorWardsPlaced"),
            wards_killed=_integer(mine, "wardsKilled"),
            healing_on_teammates=_integer(mine, "totalHealsOnTeammates"),
            shielding_on_teammates=_integer(
                mine, "totalDamageShieldedOnTeammates"
            ),
            damage_to_objectives=_integer(mine, "damageDealtToObjectives"),
            damage_to_turrets=_integer(mine, "damageDealtToTurrets"),
            turret_kills=_integer(mine, "turretKills"),
            objectives_stolen=_integer(mine, "objectivesStolen"),
            largest_killing_spree=_integer(mine, "largestKillingSpree"),
            largest_multi_kill=_integer(mine, "largestMultiKill"),
            first_blood_participation=bool(
                mine.get("firstBloodKill") or mine.get("firstBloodAssist")
            ),
            performance_badges=performance_badges,
        )
        overview.entries.append(entry)
        overview.games += 1
        overview.wins += int(won)
        overview.kills += kills
        overview.deaths += deaths
        overview.assists += assists
        overview.total_vision += entry.vision_score
        champion = champion_totals.setdefault(
            (champion_id, position),
            ChampionHistoryStat(champion_id=champion_id, position=position),
        )
        champion.games += 1
        champion.wins += int(won)
        champion.kills += kills
        champion.deaths += deaths
        champion.assists += assists
        champion.vision_score += entry.vision_score
        if overview.games >= limit:
            break

    overview.champions = sorted(
        champion_totals.values(),
        key=lambda stat: (stat.games, stat.wins, stat.kda or 0.0),
        reverse=True,
    )
    refresh_recent_20_summary(overview)
    return overview
